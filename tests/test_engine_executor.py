"""M3 Task 5: Job 执行器的测试。

纪律：取消、total 语义、去重全部单线程确定性测试（不起真线程）。
classify_asset / scan_queue_assets / is_canceled 通过 monkeypatch 注入替身，
聚焦 executor 的编排逻辑（进度、事件、取消、cfg 快照）。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.engine.executor as executor_module
from app.classify.pipeline import PipelineConfig, PipelineResult
from app.db.connection import Database
from app.db.migrate import migrate
from app.engine.executor import (
    DEFAULT_PROMPT_TEMPLATE,
    _already_done,
    build_pipeline_config,
    run_scan_job,
    run_single_job,
)
from app.store import events, jobs
from app.store.settings import SettingsStore
from app.store.tags import TagDef

# --------------------------------------------------------------- fixtures


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    migrate(d)
    return d


def make_cfg() -> PipelineConfig:
    return PipelineConfig(
        prompt_template="Tags:\n{{TAG_LIST}}\n",
        tags=[TagDef(id=1, name="cat", description="a cat", kind="primary",
                     enabled=True, archive=False, album=None,
                     min_confidence=None)],
        marker_rules=[],
        fallback_tag="other",
        default_min_confidence=0.6,
        thumb=__import__("app.classify.thumb", fromlist=["ThumbConfig"]).ThumbConfig(),
        queue_tag_name="未分类",
        llm=None,               # executor 不直接用 llm（传给 classify_asset）
        concurrency_owner=1,
    )


class Recorder:
    """classify_asset 替身：记录调用，按脚本吐结果/抛异常。

    results 为空时默认返回一条 status='ok' 结果——子任务路径不再像旧 scan
    那样一次喂满脚本，空脚本 pop(0) 会 IndexError（被 executor 收敛成
    failed，done 断言全崩）。"""

    def __init__(self, results=None, exc=None):
        self.results = list(results or [])
        self.exc = exc
        self.calls: list[str] = []
        self.kwargs: list[dict] = []

    def __call__(self, db, client, llm, asset_id, cfg, **kwargs):
        self.calls.append(asset_id)
        self.kwargs.append(kwargs)
        if self.exc:
            raise self.exc
        if self.results:
            return self.results.pop(0)
        return self._ok_result(asset_id)

    def _ok_result(self, asset_id: str) -> PipelineResult:
        return PipelineResult(asset_id=asset_id, tag="cat", confidence=0.9,
                              markers=(), status="ok", dry_run=False)

    @property
    def last_kwargs(self) -> dict:
        return self.kwargs[-1] if self.kwargs else {}


def ok(asset_id: str) -> PipelineResult:
    return PipelineResult(asset_id=asset_id, tag="cat", confidence=0.9,
                          markers=(), status="ok", dry_run=False)


def err(asset_id: str) -> PipelineResult:
    return PipelineResult(asset_id=asset_id, tag="", confidence=None,
                          markers=(), status="error", dry_run=False)


def refused(asset_id: str) -> PipelineResult:
    return PipelineResult(asset_id=asset_id, tag="other", confidence=None,
                          markers=("sensitive",), status="refused", dry_run=False)


def event_types(db: Database) -> list[str]:
    return [e["event_type"] for e in events.tail_events(db, limit=50)]


def get_job(db: Database, job_id: int) -> jobs.JobRow:
    j = jobs.get_job(db, job_id)
    assert j is not None
    return j


def drain_children(db: Database, client=None) -> list[str]:
    """单线程模拟 worker 池：claim + 跑完所有排队的 classify_item 子任务。
    返回被处理的 asset_id 列表（按认领顺序）。"""
    handled: list[str] = []
    while True:
        job = jobs.claim_next(db)
        if job is None or job.type != "classify_item":
            break
        handled.append(job.params["asset_id"])
        executor_module.run_classify_item(db, client, None, job,
                                          cfg_factory=make_cfg)
    return handled


def finish_parent(db: Database, parent_id: int) -> None:
    executor_module.finish_scan_parent(db, parent_id)


def wait_until(pred, timeout: float = 3.0) -> bool:
    """轮询等待（协调器线程是异步的，需有界等待）。"""
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if pred():
            return True
        _time.sleep(0.01)
    return False


def make_running_parent(db: Database) -> int:
    parent = jobs.create_job(db, "scan", {})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (parent,))
    return parent


def make_child(db: Database, asset_id: str, parent_id: int) -> jobs.JobRow:
    """建一条 classify_item 子任务并 claim（父必须已 running，否则会被 claim）。"""
    child = jobs.create_job(db, "classify_item",
                            {"asset_id": asset_id, "parent_job_id": parent_id})
    claimed = jobs.claim_next(db)
    assert claimed is not None and claimed.id == child
    return claimed


# --------------------------------------------------------------- single job


def test_single_job_ok_done_and_events(db: Database, monkeypatch) -> None:
    """失败模式 1：single 正常跑完 → done=1、事件齐全。"""
    rec = Recorder(results=[ok("a1")])
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    job_id = jobs.create_job(db, "single",
                             {"asset_id": "a1", "source": "webhook"})
    job = get_job(db, job_id)

    run_single_job(db, client=None, llm=None, job=job, cfg=make_cfg())

    row = get_job(db, job_id)
    assert row.status == "done" and row.done == 1 and row.failed == 0
    types = event_types(db)
    assert types.count("job_started") == 1
    assert types.count("job_complete") == 1
    assert "asset_ingested" in types


def test_single_job_error_result_counts_failed_but_done(db: Database,
                                                        monkeypatch) -> None:
    """失败模式 2：classify 返回 error → failed+1，job 仍 done。"""
    rec = Recorder(results=[err("a1")])
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    job_id = jobs.create_job(db, "single", {"asset_id": "a1", "source": "poll"})
    job = get_job(db, job_id)

    run_single_job(db, None, None, job, cfg=make_cfg())

    row = get_job(db, job_id)
    assert row.status == "done" and row.failed == 1 and row.done == 0
    assert "job_error" in event_types(db)


def test_single_job_refused_is_skipped_event(db: Database, monkeypatch) -> None:
    rec = Recorder(results=[refused("a1")])
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    job_id = jobs.create_job(db, "single", {"asset_id": "a1", "source": "poll"})
    job = get_job(db, job_id)

    run_single_job(db, None, None, job, cfg=make_cfg())

    row = get_job(db, job_id)
    assert row.status == "done" and row.done == 1 and row.failed == 0
    assert "asset_skipped" in event_types(db)


def test_single_job_unexpected_exception_fails_job(db: Database,
                                                   monkeypatch) -> None:
    """失败模式 5：执行抛未预期异常 → finish_job(error) → failed + job_error。"""
    rec = Recorder(exc=RuntimeError("boom"))
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    job_id = jobs.create_job(db, "single", {"asset_id": "a1", "source": "poll"})
    job = get_job(db, job_id)

    run_single_job(db, None, None, job, cfg=make_cfg())  # 不应向外抛

    row = get_job(db, job_id)
    assert row.status == "failed" and row.error is not None
    assert "job_error" in event_types(db)


def test_single_job_dryrun_type_forces_dry_run(db: Database,
                                               monkeypatch) -> None:
    seen: dict = {}

    def fake_classify(db, client, llm, asset_id, cfg, **kwargs):
        seen.update(kwargs)
        return ok(asset_id)

    monkeypatch.setattr(executor_module, "classify_asset", fake_classify)
    job_id = jobs.create_job(db, "dryrun", {"asset_id": "a1", "source": "manual"})
    job = get_job(db, job_id)

    run_single_job(db, None, None, job, cfg=make_cfg())
    assert seen["dry_run"] is True


def test_single_job_dryrun_uses_sandbox_prompt_body(db: Database,
                                                    monkeypatch) -> None:
    """沙盒试跑：job params.prompt_body 携带编辑框草稿，必须覆盖 cfg 里的
    已保存模板（未保存版本也能试跑）。"""
    seen: dict = {}

    def fake_classify(db, client, llm, asset_id, cfg, **kwargs):
        seen["cfg"] = cfg
        return ok(asset_id)

    monkeypatch.setattr(executor_module, "classify_asset", fake_classify)
    job_id = jobs.create_job(db, "dryrun", {
        "asset_id": "a1", "source": "manual",
        "prompt_body": "DRAFT\n{{TAG_LIST}}\n",
    })
    job = get_job(db, job_id)

    run_single_job(db, None, None, job, cfg=make_cfg())
    assert seen["cfg"].prompt_template == "DRAFT\n{{TAG_LIST}}\n"
    # 其他 cfg 字段不受影响
    assert seen["cfg"].queue_tag_name == make_cfg().queue_tag_name


def test_single_job_non_dryrun_ignores_prompt_body(db: Database,
                                                   monkeypatch) -> None:
    """非 dryrun 的 single job 不允许被 params 里的 prompt_body 改写模板
    （否则一条 job 就能绕过已保存的激活版本）。"""
    seen: dict = {}

    def fake_classify(db, client, llm, asset_id, cfg, **kwargs):
        seen["cfg"] = cfg
        return ok(asset_id)

    monkeypatch.setattr(executor_module, "classify_asset", fake_classify)
    job_id = jobs.create_job(db, "single", {
        "asset_id": "a1", "source": "manual", "prompt_body": "DRAFT\n",
    })
    job = get_job(db, job_id)

    run_single_job(db, None, None, job, cfg=make_cfg())
    assert seen["cfg"].prompt_template == make_cfg().prompt_template


# --------------------------------------------------------------- scan job


def test_scan_splits_into_child_jobs(db: Database, monkeypatch) -> None:
    """scan 只拆分：3 张候选 → 3 条 classify_item 子任务，父仍 running，
    拆分阶段不调 classify_asset。"""
    monkeypatch.setattr(executor_module, "scan_queue_assets",
                        lambda *a, **k: ["a1", "a2", "a3"])
    parent = jobs.create_job(db, "scan", {"source": "poll"})
    job = jobs.claim_next(db)
    assert job.id == parent and job.type == "scan"
    recorder = Recorder()
    monkeypatch.setattr(executor_module, "classify_asset", recorder)

    executor_module.run_scan_job(db, None, None, job, cfg_factory=make_cfg)

    assert recorder.calls == []          # 拆分阶段不分类
    children = jobs.list_jobs(db, include_internal=True, type_="classify_item")
    assert len(children) == 3
    assert all(c.params["parent_job_id"] == parent for c in children)
    assert jobs.get_job(db, parent).status == "running"


def test_classify_item_increments_parent_and_writes_parent_job_id(
        db: Database, monkeypatch) -> None:
    """子任务：进度记父；传给 classify_asset 的 job_id 是**父 id**。"""
    parent = jobs.create_job(db, "scan", {})
    recorder = Recorder()
    monkeypatch.setattr(executor_module, "classify_asset", recorder)
    child_id = jobs.create_job(db, "classify_item",
                               {"asset_id": "a1", "parent_job_id": parent})
    parent_job = jobs.claim_next(db)
    assert parent_job.type == "scan"
    job = jobs.claim_next(db)
    assert job.type == "classify_item" and job.id == child_id

    executor_module.run_classify_item(db, None, None, job, cfg_factory=make_cfg)

    assert recorder.last_kwargs["job_id"] == parent
    assert jobs.get_job(db, parent).done == 1
    assert jobs.get_job(db, child_id).status == "done"


def test_classify_item_already_done_counts_as_done(db: Database,
                                                   monkeypatch) -> None:
    """幂等二次确认：资产已 ok → 计 done，不调 LLM。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, applied_at) "
            "VALUES ('done-asset', 'poll', 'ok', 0, datetime('now'))")
    parent = jobs.create_job(db, "scan", {})
    jobs.claim_next(db)                    # 父 → running
    child_id = jobs.create_job(db, "classify_item",
                               {"asset_id": "done-asset",
                                "parent_job_id": parent})
    jobs.claim_next(db)                    # 子 → running
    job = jobs.get_job(db, child_id)
    recorder = Recorder()
    monkeypatch.setattr(executor_module, "classify_asset", recorder)

    executor_module.run_classify_item(db, None, None, job, cfg_factory=make_cfg)

    assert recorder.calls == []
    assert jobs.get_job(db, parent).done == 1


def test_classify_item_already_done_heals_queue_tag(db: Database,
                                                    monkeypatch) -> None:
    """幂等二次确认命中（早退）时补摘队列标签，避免残留「未分类」。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, applied_at) "
            "VALUES ('done-asset', 'poll', 'ok', 0, datetime('now'))")
    parent = jobs.create_job(db, "scan", {})
    jobs.claim_next(db)                    # 父 → running
    child_id = jobs.create_job(db, "classify_item",
                               {"asset_id": "done-asset",
                                "parent_job_id": parent})
    jobs.claim_next(db)                    # 子 → running
    job = jobs.get_job(db, child_id)
    monkeypatch.setattr(executor_module, "classify_asset", Recorder())

    untagged: list[tuple[str, list[str]]] = []

    class FakeClient:
        def ensure_tags(self, names):
            return {n: f"tag-{n}" for n in names}

        def untag_assets(self, tag_id, asset_ids):
            untagged.append((tag_id, list(asset_ids)))

    executor_module.run_classify_item(db, FakeClient(), None, job,
                                      cfg_factory=make_cfg)

    assert untagged == [("tag-未分类", ["done-asset"])]  # 早退也补摘标签
    assert "queue_tag_healed" in event_types(db)
    assert jobs.get_job(db, parent).done == 1


def test_classify_item_lease_conflict_retries_not_failed(db: Database,
                                                         monkeypatch) -> None:
    """并发副本持有租约：不计 failed；等待后复查发现已完成 → 计 done。
    必须真正走到租约分支：入口 _already_done=False，调用期间经替身副作用
    变为 True。patch time.sleep 避免真睡。"""
    parent = jobs.create_job(db, "scan", {})
    jobs.claim_next(db)                    # 父 → running
    child_id = jobs.create_job(db, "classify_item",
                               {"asset_id": "lease-asset",
                                "parent_job_id": parent})
    jobs.claim_next(db)                    # 子 → running
    job = jobs.get_job(db, child_id)

    def _side_effect_recorder(db_, client_, llm_, asset_id, cfg_, **kwargs):
        with db_.connect() as conn:
            conn.execute(
                "INSERT INTO asset_records (asset_id, job_id, source, status, "
                "dry_run, applied_at) VALUES (?, ?, 'poll', 'ok', 0, "
                "datetime('now'))", (asset_id, parent))
        return err(asset_id)               # status="error"

    monkeypatch.setattr(executor_module, "classify_asset", _side_effect_recorder)
    monkeypatch.setattr(executor_module.time, "sleep", lambda *_: None)

    executor_module.run_classify_item(db, None, None, job, cfg_factory=make_cfg)

    assert jobs.get_job(db, parent).failed == 0
    assert jobs.get_job(db, parent).done == 1
    assert jobs.get_job(db, child_id).status == "done"


def test_finish_scan_parent_after_children_drain(db: Database) -> None:
    """协调器收尾：pending 0 → 父 done；pending > 0 → 不动。"""
    parent = jobs.create_job(db, "scan", {})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (parent,))
    jobs.create_job(db, "classify_item",
                    {"asset_id": "a1", "parent_job_id": parent})

    executor_module.finish_scan_parent(db, parent)
    assert jobs.get_job(db, parent).status == "running"   # 还有排队子任务

    jobs.cancel_children_of(db, parent)
    executor_module.finish_scan_parent(db, parent)
    assert jobs.get_job(db, parent).status == "done"


# ------------------------------------------- 优化批次 Part B: 子任务/协调器

def test_classify_item_genuine_error_counts_failed(db: Database,
                                                   monkeypatch) -> None:
    """真实失败路径（LLM/Immich 挂）：error 且无并发 ok 记录 → 父 failed+1，
    子任务 done，_record_result 的 else 分支记 job_error。sleep 已 patch。"""
    parent = make_running_parent(db)
    child = make_child(db, "a1", parent)
    monkeypatch.setattr(executor_module, "classify_asset",
                        Recorder(results=[err("a1")]))
    monkeypatch.setattr(executor_module.time, "sleep", lambda *_: None)

    executor_module.run_classify_item(db, None, None, child, cfg_factory=make_cfg)

    p = jobs.get_job(db, parent)
    assert p.failed == 1 and p.done == 0
    assert "job_error" in event_types(db)
    assert jobs.get_job(db, child.id).status == "done"


def test_classify_item_record_failure_does_not_double_count(
        db: Database, monkeypatch) -> None:
    """I4：_record_result（写事件）抛错时，except 不能重复 incr_progress
    —— 计数恰好一次，done+failed 不超过 total。"""
    parent = make_running_parent(db)
    child = make_child(db, "a1", parent)
    monkeypatch.setattr(executor_module, "classify_asset", Recorder())  # ok

    def boom(*a, **k):
        raise RuntimeError("event write boom")

    monkeypatch.setattr(executor_module, "_record_result", boom)

    executor_module.run_classify_item(db, None, None, child, cfg_factory=make_cfg)

    p = jobs.get_job(db, parent)
    assert p.done + p.failed == 1        # 只计一次
    assert jobs.get_job(db, child.id).status == "failed"


def test_classify_item_malformed_params_fails_without_escaping(db: Database,
                                                               monkeypatch) -> None:
    """M2：子任务 params 畸形（缺 asset_id）→ 计父 failed、子 failed，
    不把异常抛回 worker 池。"""
    parent = make_running_parent(db)
    child_id = jobs.create_job(db, "classify_item", {"parent_job_id": parent})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (child_id,))
    job = jobs.get_job(db, child_id)
    monkeypatch.setattr(executor_module, "classify_asset", Recorder())

    executor_module.run_classify_item(db, None, None, job, cfg_factory=make_cfg)

    assert jobs.get_job(db, parent).failed == 1
    assert jobs.get_job(db, child_id).status == "failed"


def test_start_scan_coordinator_finishes_parent_when_children_done(
        db: Database, monkeypatch) -> None:
    """I5：协调器线程 smoke——父 running + 子 running，子终态后父转 done。"""
    monkeypatch.setattr(executor_module, "SCAN_COORD_POLL_SECONDS", 0.01)
    parent = make_running_parent(db)
    child = make_child(db, "a1", parent)   # 子 running → pending_children=1

    t = executor_module.start_scan_coordinator(db.path, parent)
    try:
        jobs.finish_job(db, child.id)
        assert wait_until(lambda: jobs.get_job(db, parent).status == "done")
    finally:
        t.join(timeout=2.0)
    assert jobs.get_job(db, parent).status == "done"


def test_start_scan_coordinator_survives_transient_error(db: Database,
                                                         monkeypatch) -> None:
    """I1：循环体内一次瞬态异常不能杀死协调器线程；记 scan_coordinator_error
    后继续，父仍能收尾。"""
    monkeypatch.setattr(executor_module, "SCAN_COORD_POLL_SECONDS", 0.01)
    parent = make_running_parent(db)
    calls = {"n": 0}
    real = executor_module.finish_scan_parent

    def flaky(db_, jid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient lock")
        real(db_, jid)

    monkeypatch.setattr(executor_module, "finish_scan_parent", flaky)

    t = executor_module.start_scan_coordinator(db.path, parent)
    t.join(timeout=2.0)

    assert not t.is_alive()
    assert jobs.get_job(db, parent).status == "done"
    assert "scan_coordinator_error" in event_types(db)


def test_start_scan_coordinator_logs_timeout_and_fails_parent(
        db: Database, monkeypatch) -> None:
    """I1/H1：超时不再静默放弃——记 scan_coordinator_timeout 并把父 job 置
    失败，让轮询调度无需重启即可恢复。"""
    monkeypatch.setattr(executor_module, "SCAN_COORDINATOR_TIMEOUT", 0)
    parent = make_running_parent(db)

    t = executor_module.start_scan_coordinator(db.path, parent)
    t.join(timeout=2.0)

    assert not t.is_alive()
    assert "scan_coordinator_timeout" in event_types(db)
    row = jobs.get_job(db, parent)
    assert row.status == "failed"
    assert "timeout" in (row.error or "")


def test_start_scan_coordinator_throttles_error_log(db: Database,
                                                    monkeypatch) -> None:
    """H2：协调器错误日志按 SCAN_COORD_ERROR_LOG_INTERVAL 节流——持续失败时
    不会每 0.5s 记一条。"""
    monkeypatch.setattr(executor_module, "SCAN_COORD_POLL_SECONDS", 0.001)
    monkeypatch.setattr(executor_module, "SCAN_COORDINATOR_TIMEOUT", 0.03)
    monkeypatch.setattr(executor_module, "SCAN_COORD_ERROR_LOG_INTERVAL", 60.0)
    parent = make_running_parent(db)

    def always_boom(db_, jid):
        raise RuntimeError("persistent failure")

    monkeypatch.setattr(executor_module, "finish_scan_parent", always_boom)

    t = executor_module.start_scan_coordinator(db.path, parent)
    t.join(timeout=2.0)

    assert not t.is_alive()
    assert event_types(db).count("scan_coordinator_error") == 1


def test_run_job_scan_without_callback_still_finishes(db: Database,
                                                      monkeypatch) -> None:
    """I2：未注入 on_scan_split 时 run_job 直接起协调器，父不会永远卡 running。"""
    from app.engine.executor import run_job

    monkeypatch.setattr(executor_module, "SCAN_COORD_POLL_SECONDS", 0.01)
    monkeypatch.setattr(executor_module, "scan_queue_assets", lambda *a, **k: [])
    monkeypatch.setattr(executor_module, "classify_asset", Recorder())
    jid = jobs.create_job(db, "scan", {"source": "poll"})
    job = jobs.claim_next(db)
    assert job is not None and job.id == jid

    run_job(db, None, None, job)          # 无 on_scan_split

    assert wait_until(lambda: jobs.get_job(db, jid).status == "done")


def test_run_job_scan_with_callback_does_not_double_start_coordinator(
        db: Database, monkeypatch) -> None:
    """I2 补充：提供了 on_scan_split 时 run_job 只回调，**不**再直接起协调器。"""
    from app.engine.executor import run_job

    monkeypatch.setattr(executor_module, "scan_queue_assets", lambda *a, **k: [])
    monkeypatch.setattr(executor_module, "classify_asset", Recorder())
    started: list = []
    monkeypatch.setattr(executor_module, "start_scan_coordinator",
                        lambda *a, **k: started.append(a))
    seen: list[int] = []

    jid = jobs.create_job(db, "scan", {"source": "poll"})
    job = jobs.claim_next(db)
    assert job is not None and job.id == jid

    run_job(db, None, None, job, on_scan_split=seen.append)

    assert seen == [jid]        # 回调被调用一次
    assert started == []        # 未直接起协调器


# ------------------------------------------------ 优化批次 Part B: prompt 版本

def test_active_prompt_version_id_dangling_active_matches_build_cfg(
        db: Database) -> None:
    """I3：active id 悬空（行缺失）→ build_pipeline_config 回退内置默认模板
    （无版本）；_active_prompt_version_id 必须同样返回 None，否则会记录一个
    body 并未被渲染的版本。"""
    from app.store.settings import SettingsStore
    with db.connect() as conn:
        conn.execute("INSERT INTO prompt_versions (body) VALUES ('v1 {{TAG_LIST}}')")
    SettingsStore(db).set_active_prompt_version(99999)   # 悬空 id

    assert build_pipeline_config(db).prompt_template == DEFAULT_PROMPT_TEMPLATE
    assert executor_module._active_prompt_version_id(db) is None


def test_active_prompt_version_id_none_falls_back_to_latest(db: Database) -> None:
    """active 为 None → 回退最新版本（与 build_pipeline_config 一致）。"""
    with db.connect() as conn:
        conn.execute("INSERT INTO prompt_versions (body) VALUES ('v1 {{TAG_LIST}}')")
        cur = conn.execute(
            "INSERT INTO prompt_versions (body) VALUES ('v2 {{TAG_LIST}}')")
        latest = cur.lastrowid
    assert executor_module._active_prompt_version_id(db) == latest


def test_scan_total_is_candidate_count_and_skips_done(db: Database,
                                                      monkeypatch) -> None:
    """失败模式 3：3 张候选、1 张已完成 → total=3、拆 2 条子任务，
    跳过的不算 done 也不算 failed。total=发现的候选数（定义见 executor 注释）。"""
    rec = Recorder()
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    monkeypatch.setattr(executor_module, "scan_queue_assets",
                        lambda client, db, tag, rules: ["a1", "a2", "a3"])
    # a1 已有非 dry_run 的 ok 记录 → "已完成"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, applied_at) "
            "VALUES ('a1', 'poll', 'ok', 0, datetime('now'))")
    job_id = jobs.create_job(db, "scan", {"source": "poll"})
    job = jobs.claim_next(db)
    assert job is not None and job.id == job_id

    run_scan_job(db, None, None, job, cfg_factory=make_cfg)

    # 拆分阶段：total=3，只给 a2/a3 建子任务，父仍 running
    row = get_job(db, job_id)
    assert row.total == 3
    assert row.status == "running"
    assert rec.calls == []
    children = jobs.list_jobs(db, include_internal=True, type_="classify_item")
    assert sorted(c.params["asset_id"] for c in children) == ["a2", "a3"]

    # 子任务跑完后协调器收尾
    drain_children(db)
    finish_parent(db, job_id)
    row = get_job(db, job_id)
    assert row.done == 2 and row.failed == 0
    assert row.status == "done"
    assert sorted(rec.calls) == ["a2", "a3"]
    types = list(reversed(event_types(db)))  # tail 新在前，恢复时间序
    assert types[0] == "job_started" and types[-1] == "job_complete"


def test_scan_dry_run_record_does_not_skip(db: Database, monkeypatch) -> None:
    """dry_run=1 的记录不算"已完成"→ 仍为该资产建子任务。"""
    rec = Recorder()
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    monkeypatch.setattr(executor_module, "scan_queue_assets",
                        lambda client, db, tag, rules: ["a1"])
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run) "
            "VALUES ('a1', 'poll', 'ok', 1)")
    job_id = jobs.create_job(db, "scan", {})
    job = get_job(db, job_id)

    run_scan_job(db, None, None, job, cfg_factory=make_cfg)

    children = jobs.list_jobs(db, include_internal=True, type_="classify_item")
    assert len(children) == 1
    assert children[0].params["asset_id"] == "a1"


def test_resumed_scan_only_processes_remaining(db: Database,
                                               monkeypatch) -> None:
    """重启续跑闭环（本次线上 685/722 卡死的回归测试）：job 被
    requeue_orphan_running_jobs 重新入队后，done=685 保留，重跑只碰
    未完成的候选——已完成的被 _already_done 跳过，不会重复分类。

    真实场景里已处理图的队列标签也已被摘除，scan 扫到的就是剩余部分；
    这里直接给出全量候选，验证"已完成过滤"这道兜底。"""
    rec = Recorder()
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    monkeypatch.setattr(executor_module, "scan_queue_assets",
                        lambda client, db, tag, rules:
                            [f"a{i}" for i in range(1, 5)])
    # 前两张在"上一个进程"里已完成（ok 记录 + job 已有进度）
    with db.connect() as conn:
        for aid in ("a1", "a2"):
            conn.execute(
                "INSERT INTO asset_records (asset_id, source, status, dry_run, "
                "applied_at) VALUES (?, 'poll', 'ok', 0, datetime('now'))",
                (aid,))
    job_id = jobs.create_job(db, "scan", {})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running', done=2, total=4 "
                     "WHERE id=?", (job_id,))
    # —— 模拟进程重启：恢复入队 → 重新 claim ——
    assert jobs.requeue_orphan_running_jobs(db) == [job_id]
    job = jobs.claim_next(db)
    assert job is not None and job.id == job_id
    assert job.done == 2  # 进度跨重启保留

    run_scan_job(db, None, None, job, cfg_factory=make_cfg)

    # 只为未完成的 a3/a4 建子任务
    children = jobs.list_jobs(db, include_internal=True, type_="classify_item")
    assert sorted(c.params["asset_id"] for c in children) == ["a3", "a4"]

    drain_children(db)
    finish_parent(db, job_id)
    assert sorted(rec.calls) == ["a3", "a4"]        # 只补剩余两张
    row = get_job(db, job_id)
    assert row.status == "done"
    assert row.done == 4
    # total = 本轮候选 4 + 续跑前已完成 2 = 6（不覆盖成 4）
    assert row.total == 6
    assert row.failed == 0


def test_resumed_scan_keeps_total_when_fewer_candidates(db: Database,
                                                        monkeypatch) -> None:
    """线上形态：续跑时只扫到剩余 37 张（已完成的 685 张队列标签已摘除），
    total 必须保持 722 而不是被覆盖成 37——否则进度显示 "722/37"。"""
    rec = Recorder()
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    monkeypatch.setattr(executor_module, "scan_queue_assets",
                        lambda client, db, tag, rules: ["t0", "t1", "t2"])
    job_id = jobs.create_job(db, "scan", {})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running', done=685, total=722 "
                     "WHERE id=?", (job_id,))
    assert jobs.requeue_orphan_running_jobs(db) == [job_id]
    job = jobs.claim_next(db)
    assert job.done == 685

    run_scan_job(db, None, None, job, cfg_factory=make_cfg)
    drain_children(db)
    finish_parent(db, job_id)

    row = get_job(db, job_id)
    assert row.done == 688
    assert row.total == 688  # 685 + 3，不是 3
    assert row.status == "done"


def test_fresh_scan_total_is_candidate_count(db: Database,
                                             monkeypatch) -> None:
    """首轮（done=0）total 仍是纯候选数，不被续跑逻辑影响。"""
    rec = Recorder()
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    monkeypatch.setattr(executor_module, "scan_queue_assets",
                        lambda client, db, tag, rules: ["a0", "a1", "a2"])
    job_id = jobs.create_job(db, "scan", {})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
    job = get_job(db, job_id)
    assert job.done == 0

    run_scan_job(db, None, None, job, cfg_factory=make_cfg)
    drain_children(db)
    finish_parent(db, job_id)

    row = get_job(db, job_id)
    assert row.total == 3 and row.done == 3


def test_scan_cancel_stops_splitting_new_assets(db: Database,
                                                monkeypatch) -> None:
    """失败模式 4：拆分循环内取消 → 不再建新子任务；已建的排队子任务由
    协调器清掉，父最终 done（不是 canceled —— 取消只是标志）。"""
    monkeypatch.setattr(executor_module, "classify_asset", Recorder())
    monkeypatch.setattr(executor_module, "scan_queue_assets",
                        lambda client, db, tag, rules: ["a1", "a2", "a3"])
    job_id = jobs.create_job(db, "scan", {})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?",
                     (job_id,))
    job = get_job(db, job_id)

    real_create = jobs.create_job

    def create_then_cancel(db_, type_, params, **kw):
        cid = real_create(db_, type_, params, **kw)
        if type_ == "classify_item":
            jobs.cancel_job(db_, job_id)   # running → 只置 cancel_requested
        return cid

    monkeypatch.setattr(executor_module.jobs, "create_job", create_then_cancel)

    run_scan_job(db, None, None, job, cfg_factory=make_cfg)

    children = jobs.list_jobs(db, include_internal=True, type_="classify_item")
    assert len(children) == 1              # 建了第 1 条后循环发现取消 → break
    assert get_job(db, job_id).status == "running"

    # 协调器：清掉排队子任务 → 父收尾
    finish_parent(db, job_id)
    assert get_job(db, job_id).status == "done"
    assert jobs.get_job(db, children[0].id).status == "canceled"


def test_scan_cfg_factory_called_once(db: Database, monkeypatch) -> None:
    """失败模式 7：PipelineConfig 在 scan 内只构建一次（M1 §3.3）。"""
    calls = []

    def cfg_factory():
        calls.append(1)
        return make_cfg()

    monkeypatch.setattr(executor_module, "classify_asset", Recorder(
        results=[ok("a1"), ok("a2"), ok("a3")]))
    monkeypatch.setattr(executor_module, "scan_queue_assets",
                        lambda client, db, tag, rules: ["a1", "a2", "a3"])
    job_id = jobs.create_job(db, "scan", {})
    job = get_job(db, job_id)

    run_scan_job(db, None, None, job, cfg_factory=cfg_factory)
    assert len(calls) == 1


def test_scan_unexpected_exception_fails_job(db: Database,
                                             monkeypatch) -> None:
    monkeypatch.setattr(executor_module, "load_scope_rules",
                        lambda db: (_ for _ in ()).throw(RuntimeError("boom")))
    job_id = jobs.create_job(db, "scan", {})
    job = get_job(db, job_id)

    run_scan_job(db, None, None, job, cfg_factory=make_cfg)

    row = get_job(db, job_id)
    assert row.status == "failed" and row.error is not None
    assert "job_error" in event_types(db)


# --------------------------------------------------------------- build cfg


def test_build_pipeline_config_default_template_when_no_active_version(
        db: Database) -> None:
    """active_prompt_version_id 为 None → 内置默认模板常量。"""
    cfg = build_pipeline_config(db)
    assert cfg.prompt_template == DEFAULT_PROMPT_TEMPLATE
    assert "{{TAG_LIST}}" in cfg.prompt_template  # 必须含必备占位符


def test_build_pipeline_config_reads_prompt_version(db: Database) -> None:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO prompt_versions (body) VALUES ('custom {{TAG_LIST}}')")
        pv_id = cur.lastrowid
    from app.store.settings import SettingsStore
    SettingsStore(db).set_active_prompt_version(pv_id)

    cfg = build_pipeline_config(db)
    assert cfg.prompt_template == "custom {{TAG_LIST}}"


def test_build_pipeline_config_empty_tags_allowed(db: Database) -> None:
    """tags 为空 → tag_list 空（可接受的降级：LLM 拒答/fallback）。"""
    cfg = build_pipeline_config(db)
    assert cfg.tags == []
    assert cfg.queue_tag_name == "未分类"     # settings 默认
    assert cfg.default_min_confidence == 0.8  # settings 默认


# ------------------------------------------------- dispatch（真机回归：Task 8 抓到的坑）


def test_run_job_dispatch_scan_builds_cfg_with_db(db, monkeypatch):
    """run_job 的 scan 分支必须以零参回调包住 build_pipeline_config。

    真机（M3 Task 8）曾直接传函数对象，cfg_factory() 调用时缺 db 参数
    而 job 立即 failed。pool 测试全部 mock run_job，从未走通 dispatch。

    新语义下 scan 只拆分：run_job 返回后父仍 running、已建子任务。
    """
    from app.engine.executor import run_job

    seen_cfg = {}

    def fake_build(d):
        seen_cfg["db_is_same"] = d is db
        return make_cfg()

    monkeypatch.setattr(executor_module, "build_pipeline_config", fake_build)
    monkeypatch.setattr(
        executor_module, "classify_asset",
        lambda d, c, l, a, cfg, **kw: ok(a),
    )
    monkeypatch.setattr(
        executor_module, "scan_queue_assets",
        lambda client, d, q, rules: ["a-1"],
    )

    jid = jobs.create_job(db, "scan", {"source": "poll"})
    job = jobs.claim_next(db)               # 真实流程里 worker 先 claim（running）
    assert job is not None and job.id == jid
    # 注入空回调：本用例只验 dispatch，不真起协调器（否则子任务不终态 →
    # 协调器线程会一直轮询已删除的 DB 路径）
    run_job(db, None, None, job, on_scan_split=lambda *_: None)

    row = jobs.get_job(db, jid)
    assert seen_cfg["db_is_same"] is True
    assert row.status == "running"          # 父等子任务，未收尾
    children = jobs.list_jobs(db, include_internal=True, type_="classify_item")
    assert len(children) == 1
    assert children[0].params["asset_id"] == "a-1"


def test_run_job_dispatch_single_builds_cfg(db, monkeypatch):
    from app.engine.executor import run_job

    monkeypatch.setattr(
        executor_module, "build_pipeline_config", lambda d: make_cfg(),
    )
    monkeypatch.setattr(
        executor_module, "classify_asset",
        lambda d, c, l, a, cfg, **kw: ok(a),
    )
    jid = jobs.create_job(db, "single", {"asset_id": "a-1", "source": "manual"})
    run_job(db, None, None, jobs.get_job(db, jid))
    assert jobs.get_job(db, jid).status == "done"


# ------------------------------------------------- M5 Task 6: prompt fallback


def test_prompt_fallback_null_uses_latest_version(db: Database) -> None:
    """active_prompt_version_id 为 null 且表里有版本 → 取最新版本（设计 §6.3），
    而非内置 DEFAULT_PROMPT_TEMPLATE。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO prompt_versions (body, note) VALUES ('old {{TAG_LIST}}', 'v1')")
        conn.execute(
            "INSERT INTO prompt_versions (body, note) VALUES ('newest {{TAG_LIST}}', 'v2')")

    cfg = build_pipeline_config(db)
    assert cfg.prompt_template == "newest {{TAG_LIST}}"


def test_prompt_fallback_null_and_empty_table_uses_default(db: Database) -> None:
    """表完全为空（尚未创建任何版本）→ 才回退内置默认模板。"""
    cfg = build_pipeline_config(db)
    assert cfg.prompt_template == DEFAULT_PROMPT_TEMPLATE


# ------------------------------------------------- M5 Task 3: undo dispatch


def _seed_undo_log(db: Database, job_id: int,
                   rows: list[tuple[str, str, dict]]) -> None:
    import json as _json
    with db.connect() as conn:
        for action, asset, payload in rows:
            conn.execute(
                "INSERT INTO change_log (job_id, asset_id, action, direction, "
                "payload) VALUES (?, ?, ?, 'apply', ?)",
                (job_id, asset, action, _json.dumps(payload)),
            )


def test_undo_dispatch_executes_and_finishes(db, monkeypatch):
    """run_job 分发 undo：调 UndoSession、按条数计进度、正常 finish。"""
    from app.engine.executor import run_job

    _seed_undo_log(db, 5, [
        ("tag_add", "a-1", {"tag_name": "cat"}),
        ("queue_remove", "a-1", {"queue_tag_name": "未分类"}),
    ])
    jid = jobs.create_job(db, "undo", {"target_job_id": 5})

    calls: list[tuple[str, list[str]]] = []

    class FakeClient:
        def list_tags(self):
            from types import SimpleNamespace
            return [SimpleNamespace(name="cat", id="tag-1"),
                    SimpleNamespace(name="未分类", id="tag-q")]

        def untag_assets(self, tag_id, asset_ids):
            calls.append(("untag", [tag_id, *asset_ids]))

        def tag_assets(self, tag_ids, asset_ids):
            calls.append(("tag", [*tag_ids, *asset_ids]))

        def ensure_tags(self, names):
            return {"cat": "tag-1", "未分类": "tag-q"}

    run_job(db, FakeClient(), None, jobs.get_job(db, jid))

    row = jobs.get_job(db, jid)
    assert row.status == "done"
    assert row.total == 2
    assert row.done == 2
    # 逆序：queue_remove 先恢复（tag），tag_add 后摘（untag）
    assert calls == [("tag", ["tag-q", "a-1"]), ("untag", ["tag-1", "a-1"])]
    types = event_types(db)
    assert "job_started" in types and "job_complete" in types
    # tail_events 是 DESC（新在前）：complete 先出现于 started
    assert types.index("job_complete") < types.index("job_started")


def test_undo_dispatch_404_skip_counts_as_progress_not_failed_done(db, monkeypatch):
    """404 跳过的条也 incr（done=条数），但 undone=0 的条不写 undo 记录。"""
    from app.engine.executor import run_job

    _seed_undo_log(db, 5, [("tag_add", "a-1", {"tag_name": "gone"})])
    jid = jobs.create_job(db, "undo", {"target_job_id": 5})

    class FakeClient:
        def list_tags(self):
            return []  # "gone" 不存在 → 404 路径

        def untag_assets(self, tag_id, asset_ids):  # pragma: no cover
            raise AssertionError("should not be called")

    run_job(db, FakeClient(), None, jobs.get_job(db, jid))
    row = jobs.get_job(db, jid)
    assert row.status == "done"
    assert row.done == 1  # 跳过条仍计数（不计 failed）
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM change_log WHERE direction='undo' "
            "AND job_id = ?", (jid,)).fetchone()["n"]
    assert n == 0  # 跳过条无 undo 记录


def test_undo_dispatch_unexpected_exception_fails_job(db, monkeypatch):
    from app.engine.executor import run_job

    _seed_undo_log(db, 5, [("tag_add", "a-1", {"tag_name": "cat"})])
    jid = jobs.create_job(db, "undo", {"target_job_id": 5})

    class FakeClient:
        def list_tags(self):
            raise RuntimeError("boom")

    run_job(db, FakeClient(), None, jobs.get_job(db, jid))
    row = jobs.get_job(db, jid)
    assert row.status == "failed"
    assert "boom" in row.error
    assert "job_error" in event_types(db)


def test_undo_dispatch_missing_target_param_fails_job(db):
    from app.engine.executor import run_job

    jid = jobs.create_job(db, "undo", {})
    run_job(db, None, None, jobs.get_job(db, jid))
    row = jobs.get_job(db, jid)
    assert row.status == "failed"


# ------------------------------------------- M5 Task 9: 语义钉合（reviewed / manual_override）


class _ScopeFakeClient:
    """scan_queue_assets 需要的最小 Immich 替身（ensure_tags + iter_assets）。"""

    def __init__(self, assets):
        self.assets = assets

    def ensure_tags(self, names):
        return {n: f"tag-id-{n}" for n in names}

    def iter_assets(self, dto, size=1000):
        yield from self.assets


def _scope_asset(aid: int):
    import uuid as _uuid
    return SimpleNamespace(id=_uuid.UUID(int=aid),
                           library_id="lib-1", original_path="/foo/a.jpg")


def test_semantics_reviewed_excluded_not_in_total_and_not_reprocessed(
    db: Database, monkeypatch
) -> None:
    """asset_state.reviewed=1 + enabled reviewed 排除规则 → scan_queue_assets
    在候选层排除（不计入 job.total，与"已完成"计入 total 的语义不同），
    executor 因此不再处理它。钉住现状：排除在 _already_done 之前、被排除者
    不计 total（计划文字与实现有出入，以代码现状为准）。"""
    from app.classify.scope import scan_queue_assets as real_scan

    rec = Recorder()
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    monkeypatch.setattr(executor_module, "scan_queue_assets", real_scan)
    monkeypatch.setattr(executor_module, "load_scope_rules",
                        lambda _db: [])

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO exclusions (type, value, enabled) "
            "VALUES ('reviewed', '', 1)")
        conn.execute(
            "INSERT INTO asset_state (asset_id, reviewed) VALUES (?, 1)",
            (str(_scope_asset(1).id),))

    class _Client(_ScopeFakeClient):
        def iter_assets(self, dto, size=1000):
            yield from [_scope_asset(1), _scope_asset(2)]

    job_id = jobs.create_job(db, "scan", {"source": "poll"})
    job = jobs.claim_next(db)
    assert job is not None and job.id == job_id
    run_scan_job(db, _Client([_scope_asset(1), _scope_asset(2)]), None,
                 job, cfg_factory=make_cfg)

    row = get_job(db, job_id)
    # a1 被 reviewed 排除 → 不在候选里 → total 只含 a2，且只为 a2 建子任务
    assert row.total == 1
    children = jobs.list_jobs(db, include_internal=True, type_="classify_item")
    assert [c.params["asset_id"] for c in children] == [str(_scope_asset(2).id)]
    assert "asset_out_of_scope" in event_types(db)

    drain_children(db)
    finish_parent(db, job_id)
    row = get_job(db, job_id)
    assert row.status == "done"
    assert rec.calls == [str(_scope_asset(2).id)]
    # 资产未被重复处理：a1 无任何处理记录
    assert not any("a1" in c for c in rec.calls)


def test_semantics_manual_override_with_ok_record_skipped_by_already_done(
    db: Database, monkeypatch
) -> None:
    """manual_override=1 且已有 dry_run=false ok 记录 → _already_done 跳过，
    正常"已完成"语义不被 override 破坏。"""
    rec = Recorder()
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    monkeypatch.setattr(executor_module, "scan_queue_assets",
                        lambda client, db, tag, rules: ["a1"])
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, applied_at) "
            "VALUES ('a1', 'poll', 'ok', 0, datetime('now'))")
        conn.execute(
            "INSERT INTO asset_state (asset_id, manual_override) "
            "VALUES ('a1', 1)")
    job_id = jobs.create_job(db, "scan", {"source": "poll"})
    job = jobs.claim_next(db)
    assert job is not None and job.id == job_id

    run_scan_job(db, None, None, job, cfg_factory=make_cfg)
    finish_parent(db, job_id)

    assert rec.calls == []          # 完全跳过，不重跑
    assert jobs.list_jobs(db, include_internal=True,
                          type_="classify_item") == []
    row = get_job(db, job_id)
    assert row.total == 1 and row.done == 0 and row.failed == 0
    assert row.status == "done"


# ------------------------------------------- M6 Task 1: backfill 执行器

def test_backfill_dispatch_enqueues_and_finishes(db, monkeypatch):
    """backfill：范围内资产逐批打队列标签，写 asset_enqueued 事件，
    不写 change_log、不改 asset_state，正常 finish。"""
    from app.engine.executor import run_job

    monkeypatch.setattr(executor_module, "iter_scope_assets",
                        lambda client, d, rules: iter(["a-1", "a-2"]))
    tagged: list[tuple[str, list[str]]] = []

    class FakeClient:
        def ensure_tags(self, names):
            return {n: f"id-{n}" for n in names}

        def tag_assets(self, tag_ids, asset_ids):
            tagged.append((tag_ids[0], list(asset_ids)))

    jid = jobs.create_job(db, "backfill", {})
    run_job(db, FakeClient(), None, jobs.get_job(db, jid))

    row = jobs.get_job(db, jid)
    assert row.status == "done"
    assert row.total == 2 and row.done == 2
    assert tagged == [("id-未分类", ["a-1", "a-2"])]
    types = event_types(db)
    assert types.count("asset_enqueued") == 2
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"]
        s = conn.execute(
            "SELECT COUNT(*) c FROM asset_state").fetchone()["c"]
    assert n == 0 and s == 0


def test_backfill_skips_already_done_not_in_total(db, monkeypatch):
    """有 dry_run=false ok 记录的资产跳过且不计 total。"""
    from app.engine.executor import run_job

    monkeypatch.setattr(executor_module, "iter_scope_assets",
                        lambda client, d, rules: iter(["a-1", "a-2"]))
    tagged: list[list[str]] = []

    class FakeClient:
        def ensure_tags(self, names):
            return {n: f"id-{n}" for n in names}

        def tag_assets(self, tag_ids, asset_ids):
            tagged.append(list(asset_ids))

    with db.connect() as conn:
        conn.execute("INSERT INTO asset_records (asset_id, source, status, "
                     "dry_run, applied_at) "
                     "VALUES ('a-1', 'poll', 'ok', 0, datetime('now'))")
    jid = jobs.create_job(db, "backfill", {})
    run_job(db, FakeClient(), None, jobs.get_job(db, jid))

    row = jobs.get_job(db, jid)
    assert row.status == "done"
    assert row.total == 1 and row.done == 1
    assert tagged == [["a-2"]]


def test_backfill_cancel_between_batches(db, monkeypatch):
    """取消在批之间生效：第 1 批后取消 → 第 2 批不执行，job 最终 done。"""
    from app.engine.executor import run_job

    jid_holder: list[int] = []
    tagged: list[list[str]] = []

    class FakeClient:
        def ensure_tags(self, names):
            return {n: f"id-{n}" for n in names}

        def tag_assets(self, tag_ids, asset_ids):
            tagged.append(list(asset_ids))
            jobs.cancel_job(db, jid_holder[0])

    monkeypatch.setattr(executor_module, "iter_scope_assets",
                        lambda client, d, rules: iter(["a-1", "a-2", "a-3"]))
    monkeypatch.setattr(executor_module, "BACKFILL_BATCH_SIZE", 1)
    jid_holder.append(jobs.create_job(db, "backfill", {}))
    jid = jid_holder[0]
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    run_job(db, FakeClient(), None, jobs.get_job(db, jid))

    row = jobs.get_job(db, jid)
    assert row.status == "done"
    assert tagged == [["a-1"]]
    assert row.done == 1 and row.total == 3


def test_backfill_unexpected_exception_fails_job(db, monkeypatch):
    from app.engine.executor import run_job

    def boom(client, d, rules):
        raise RuntimeError("boom")

    monkeypatch.setattr(executor_module, "iter_scope_assets", boom)
    jid = jobs.create_job(db, "backfill", {})
    run_job(db, None, None, jobs.get_job(db, jid))
    row = jobs.get_job(db, jid)
    assert row.status == "failed"
    assert "boom" in row.error


# ------------------------------------------- M6 Task 2: reclassify 执行器

def test_reclassify_dispatch_runs_each_asset(db, monkeypatch):
    from app.engine.executor import run_job

    calls: list[str] = []

    def fake_reclassify(db, client, llm, cfg, asset_id, **kw):
        calls.append(asset_id)
        return ok(asset_id)

    monkeypatch.setattr(executor_module, "reclassify_asset", fake_reclassify)
    jid = jobs.create_job(db, "reclassify", {"asset_ids": ["a-1", "a-2"]})
    run_job(db, None, None, jobs.get_job(db, jid))

    row = jobs.get_job(db, jid)
    assert row.status == "done"
    assert row.total == 2 and row.done == 2
    assert calls == ["a-1", "a-2"]


def test_reclassify_dispatch_error_counts_failed(db, monkeypatch):
    from app.engine.executor import run_job

    monkeypatch.setattr(
        executor_module, "reclassify_asset",
        lambda db, c, l, cfg, aid, **kw: err(aid))
    jid = jobs.create_job(db, "reclassify", {"asset_ids": ["a-1"]})
    run_job(db, None, None, jobs.get_job(db, jid))
    row = jobs.get_job(db, jid)
    assert row.status == "done" and row.failed == 1 and row.done == 0


def test_reclassify_dispatch_cancel_between_assets(db, monkeypatch):
    from app.engine.executor import run_job

    jid = jobs.create_job(db, "reclassify", {"asset_ids": ["a-1", "a-2"]})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))

    calls: list[str] = []

    def fake_reclassify(db, client, llm, cfg, asset_id, **kw):
        calls.append(asset_id)
        jobs.cancel_job(db, jid)
        return ok(asset_id)

    monkeypatch.setattr(executor_module, "reclassify_asset", fake_reclassify)
    run_job(db, None, None, jobs.get_job(db, jid))
    row = jobs.get_job(db, jid)
    assert row.status == "done"
    assert calls == ["a-1"]


def test_reclassify_dispatch_unexpected_exception_fails_job(db, monkeypatch):
    from app.engine.executor import run_job

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(executor_module, "reclassify_asset", boom)
    jid = jobs.create_job(db, "reclassify", {"asset_ids": ["a-1"]})
    run_job(db, None, None, jobs.get_job(db, jid))
    row = jobs.get_job(db, jid)
    assert row.status == "failed" and "boom" in row.error


# ------------------------------------------- M6 Task 3: reset 执行器

def test_reset_dispatch_runs_steps_and_finishes(db, monkeypatch):
    from app.engine.executor import run_job

    steps = {"unarchived": 3, "untagged_tags": 2}
    seen: dict = {}
    monkeypatch.setattr(
        executor_module, "run_reset_steps",
        lambda db, client, *, job_id, is_canceled, dimensions=None: (
            seen.update(job_id=job_id, cancel_fn=is_canceled,
                        dimensions=dimensions) or steps))
    jid = jobs.create_job(db, "reset", {})
    run_job(db, object(), None, jobs.get_job(db, jid))

    row = jobs.get_job(db, jid)
    assert row.status == "done"
    assert seen["job_id"] == jid
    assert callable(seen["cancel_fn"])
    assert seen["dimensions"] is None      # params={} → None（向后兼容，等价全集）
    types = event_types(db)
    assert "job_started" in types and "job_complete" in types

    # params 带 dimensions → 解析成集合后原样下传
    jid2 = jobs.create_job(db, "reset", {"dimensions": ["rating"]})
    run_job(db, object(), None, jobs.get_job(db, jid2))
    assert jobs.get_job(db, jid2).status == "done"
    assert seen["dimensions"] == {"rating"}


def test_reset_dispatch_unexpected_exception_fails_job(db, monkeypatch):
    from app.engine.executor import run_job

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(executor_module, "run_reset_steps", boom)
    jid = jobs.create_job(db, "reset", {})
    run_job(db, None, None, jobs.get_job(db, jid))
    row = jobs.get_job(db, jid)
    assert row.status == "failed" and "boom" in row.error


# ------------------------------------------------ lease owner 唯一性


def test_owner_id_unique_per_thread() -> None:
    """同一进程内不同线程必须拿到不同 owner（BUG-2 回归）。"""
    import threading
    from app.engine.executor import _owner_id

    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(_owner_id())

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert len(results) == 2
    assert results[0] != results[1], "不同线程的 lease owner 必须不同"


def test_owner_id_varies_across_processes(monkeypatch) -> None:
    """同 hostname 不同 pid 的 owner 必须不同（多容器共享 DB 场景）。"""
    import os
    import app.engine.executor as ex

    monkeypatch.setattr(os, "getpid", lambda: 111)
    a = ex._owner_id()
    monkeypatch.setattr(os, "getpid", lambda: 222)
    b = ex._owner_id()
    assert a != b


# ------------------------------------------------------- queue tag 自愈


def test_scan_heals_stuck_queue_tag(db: Database, monkeypatch) -> None:
    """已完成但仍挂队列标签的资产：补一次 untag，不进 classify。"""
    rec = Recorder(results=[])
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    monkeypatch.setattr(executor_module, "scan_queue_assets",
                        lambda client, db, tag, rules: ["a1"])

    untagged: list[tuple[str, list[str]]] = []

    class FakeClient:
        def ensure_tags(self, names):
            return {n: f"tag-{n}" for n in names}

        def untag_assets(self, tag_id, asset_ids):
            untagged.append((tag_id, list(asset_ids)))

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, applied_at) "
            "VALUES ('a1', 'poll', 'ok', 0, datetime('now'))")
    job_id = jobs.create_job(db, "scan", {"source": "poll"})
    job = jobs.get_job(db, job_id)

    run_scan_job(db, FakeClient(), None, job, cfg_factory=make_cfg)

    assert rec.calls == []  # 已完成 → 不重新分类
    assert untagged == [("tag-未分类", ["a1"])]  # 补摘队列标签
    types = event_types(db)
    assert "queue_tag_healed" in types
    with db.connect() as conn:
        row = conn.execute(
            "SELECT action, payload FROM change_log WHERE asset_id='a1'"
        ).fetchone()
    assert row["action"] == "queue_remove"
    assert '"healed": true' in row["payload"]


def test_scan_heal_failure_does_not_break_scan(db: Database, monkeypatch) -> None:
    """自愈 untag 失败：记事件，scan 正常拆分并收尾。"""
    rec = Recorder()
    monkeypatch.setattr(executor_module, "classify_asset", rec)
    monkeypatch.setattr(executor_module, "scan_queue_assets",
                        lambda client, db, tag, rules: ["a1", "a2"])

    class FakeClient:
        def ensure_tags(self, names):
            return {n: f"tag-{n}" for n in names}

        def untag_assets(self, tag_id, asset_ids):
            raise executor_module.ImmichError("Immich 批量操作部分失败: a1: not_found")

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, applied_at) "
            "VALUES ('a1', 'poll', 'ok', 0, datetime('now'))")
    job_id = jobs.create_job(db, "scan", {"source": "poll"})
    job = jobs.claim_next(db)
    assert job is not None and job.id == job_id

    run_scan_job(db, FakeClient(), None, job, cfg_factory=make_cfg)

    assert "queue_tag_heal_failed" in event_types(db)
    # a2 照常拆成子任务（自愈失败不影响拆分）
    children = jobs.list_jobs(db, include_internal=True, type_="classify_item")
    assert [c.params["asset_id"] for c in children] == ["a2"]

    drain_children(db)
    finish_parent(db, job_id)
    assert rec.calls == ["a2"]
    row = jobs.get_job(db, job_id)
    assert row.status == "done"


def test_default_prompt_template_declares_new_contract():
    from app.engine.executor import DEFAULT_PROMPT_TEMPLATE
    assert "description" in DEFAULT_PROMPT_TEMPLATE
    assert "rating" in DEFAULT_PROMPT_TEMPLATE
    assert "{{TAG_LIST}}" in DEFAULT_PROMPT_TEMPLATE        # 强制占位符仍在
    # description 必须排在 tag 之前（Visual CoT，D3）
    assert DEFAULT_PROMPT_TEMPLATE.index("description") < DEFAULT_PROMPT_TEMPLATE.index('"tag"')


# -------------------------------------------「已完成」口径（Task 10）


def test_already_done_uses_latest_record(db: Database) -> None:
    """旧的已应用记录 + 新的失败记录 → 必须判"未完成"，否则失败那次永不重试。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, "
            "  created_at, applied_at) VALUES "
            "('a1','poll','ok',0,'2026-01-01 00:00:00','2026-01-01 00:00:01'),"
            "('a1','manual','ok',0,'2026-02-01 00:00:00',NULL)")
    assert _already_done(db, "a1") is False


def test_already_done_true_when_applied(db: Database) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, "
            "  created_at, applied_at) VALUES "
            "('a1','poll','ok',0,'2026-01-01 00:00:00','2026-01-01 00:00:01')")
    assert _already_done(db, "a1") is True


def test_already_done_false_when_applied_at_missing(db: Database) -> None:
    """status='ok' 但没 applied_at = apply 失败挂起 → 必须重试。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run) "
            "VALUES ('a1','poll','ok',0)")
    assert _already_done(db, "a1") is False


def test_already_done_true_when_retries_exhausted(db: Database) -> None:
    """§7.3：放弃重试也是终态，否则永久失败的资产会无限重跑。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run) "
            "VALUES ('a1','poll','ok',0)")
        conn.execute("INSERT INTO asset_state (asset_id, apply_attempts) "
                     "VALUES ('a1', 3)")
    assert _already_done(db, "a1") is True


# --------------------------------------- Effectors: pipeline config（Task 15）


def test_pipeline_config_carries_effect_settings(db: Database) -> None:
    """非默认值：只有 build_pipeline_config 真的读了 settings 才可能通过。"""
    settings = SettingsStore(db)
    settings.set("enabled_effects", ["tag", "rating"])
    settings.set("apply_max_attempts", 7)
    settings.set("skip_marker_tag_name", "我改过的名字")

    cfg = build_pipeline_config(db)
    assert cfg.enabled_effects == {"tag", "rating"}
    assert cfg.apply_max_attempts == 7
    assert cfg.skip_marker_tag_name == "我改过的名字"


def test_pipeline_config_effect_defaults(db: Database) -> None:
    """未配置时的兜底：全开 + 上限 3 + 按 zh 解析标记名。"""
    cfg = build_pipeline_config(db)
    assert cfg.enabled_effects == {"tag", "visibility", "album",
                                   "rating", "description"}
    assert cfg.apply_max_attempts == 3
    assert cfg.skip_marker_tag_name == "保留人工信息"
