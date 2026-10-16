"""jobs_ui 路由契约：任务列表 / 详情 / 取消 / 全局暂停开关 / SSE 单 job 进度。

SSE 端点是无限 async generator，TestClient stream 会挂死（见 test_web_overview.py
模块注释），复用其 ASGI 驱动 helper `_collect_sse_frames` 收帧后主动断开。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store import jobs as jobs_store
from app.store.settings import SettingsStore
from app.web.app import create_app
from tests.test_web_overview import _collect_sse_frames


@pytest.fixture()
def client(tmp_path: Path):
    data_dir = tmp_path / "data"
    c = TestClient(
        create_app(data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir)))
    )
    with c:
        c.app.state.scheduler.stop()
        SettingsStore(c.app.state.db).set("poll_enabled", False)
        with c.app.state.db.connect() as conn:
            conn.execute("DELETE FROM jobs")
        yield c


def _running_job(db) -> int:
    """造一个 running job：create 后手动 claim。"""
    jid = jobs_store.create_job(db, "scan", params={"source": "test"})
    jobs_store.claim_next(db)
    return jid


def _write_record(db, asset_id: str, job_id: int, *, status: str = "error") -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, job_id, source, predicted_tag, "
            "confidence, status) VALUES (?, ?, 'poll', 'landscape', 0.9, ?)",
            (asset_id, job_id, status),
        )


# ------------------------------------------------------------- list page

def test_list_empty_state(client):
    c = client
    html = c.get("/jobs").text
    assert "暂无任务" in html


def test_list_renders_rows_with_progress_and_badges(client):
    c = client
    db = c.app.state.db
    ok_id = jobs_store.create_job(db, "scan", params={"source": "test"})
    fail_id = jobs_store.create_job(db, "reclassify", params={})
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET total = 1 WHERE id = ?", (ok_id,))
    jobs_store.claim_next(db)
    jobs_store.incr_progress(db, ok_id)
    jobs_store.finish_job(db, ok_id)
    jobs_store.claim_next(db)
    jobs_store.finish_job(db, fail_id, error="unsupported job type")
    html = c.get("/jobs").text
    assert "/jobs/%d" % fail_id in html  # 行可点进详情
    assert "reclassify" in html
    assert "failed" in html
    assert "1/1" in html
    # 详情入口链接存在
    assert "/jobs/%d" % ok_id in html


def test_list_paginates_10_per_page(client):
    c = client
    db = c.app.state.db
    for i in range(25):
        jobs_store.create_job(db, "single", params={"asset_id": str(i)})
    resp = c.get("/jobs")
    assert resp.status_code == 200
    html = resp.text
    assert "共 25 条" in html
    assert "第 1 / 3 页" in html
    # 第一页：最新 10 条（id 最高的 10 个）
    with db.connect() as conn:
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'jobs'"
        ).fetchone()["seq"]
    assert '"/jobs/%d"' % seq in html
    assert '"/jobs/%d"' % (seq - 9) in html
    assert '"/jobs/%d"' % (seq - 10) not in html
    # 第二页有上一页链接，第一页没有
    assert "下一页" in html
    assert "上一页" not in html
    p2 = c.get("/jobs", params={"page": 2}).text
    assert '"/jobs/%d"' % (seq - 10) in p2
    assert "上一页" in p2


def test_list_filter_by_status(client):
    c = client
    db = c.app.state.db
    done_id = jobs_store.create_job(db, "scan", params={})
    jobs_store.claim_next(db)
    jobs_store.finish_job(db, done_id)
    running_id = _running_job(db)
    html = c.get("/jobs", params={"status": "running"}).text
    assert "/jobs/%d" % running_id in html
    assert "/jobs/%d" % done_id not in html


def test_list_shows_started_at_for_claimed_job(client):
    """claim_next 后 started_at 有值，列表页"时间"列直出该字符串。"""
    c = client
    db = c.app.state.db
    jid = _running_job(db)
    started_at = jobs_store.get_job(db, jid).started_at
    assert started_at is not None
    html = c.get("/jobs").text
    assert "时间" in html
    assert started_at in html


def test_list_shows_dash_for_queued_job(client):
    """排队中的 job started_at 为 NULL，时间列显示 —。"""
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "scan", params={"source": "test"})
    assert jobs_store.get_job(db, jid).started_at is None
    html = c.get("/jobs").text
    assert "—" in html


# ------------------------------------------------------------- pause switch

def test_pause_switch_shows_state(client):
    c = client
    html = c.get("/jobs").text
    assert "已暂停" in html  # fixture 里 poll_enabled=False
    assert "action=\"/jobs/pause\"" in html


def test_pause_toggle_writes_settings(client):
    c = client
    db = c.app.state.db
    resp = c.post("/jobs/pause", data={"pause": "1"})
    assert resp.status_code == 200
    assert SettingsStore(db).get("poll_enabled") is False
    c.post("/jobs/pause", data={})
    assert SettingsStore(db).get("poll_enabled") is True
    assert "运行中" in c.get("/jobs").text


# ------------------------------------------------------------- manual scan trigger

def test_post_scan_creates_manual_job(client):
    c = client
    db = c.app.state.db
    resp = c.post("/jobs/scan", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/jobs"
    rows = jobs_store.list_jobs(db, type_="scan")
    assert len(rows) == 1
    assert rows[0].type == "scan"
    assert rows[0].status == "queued"
    assert rows[0].params == {"source": "manual"}


def test_post_scan_dedups_active_scan(client):
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "scan", params={"source": "poll"})
    resp = c.post("/jobs/scan", follow_redirects=False)
    assert resp.status_code == 303
    rows = jobs_store.list_jobs(db, type_="scan")
    assert len(rows) == 1
    assert rows[0].id == jid


# ------------------------------------------------------------- detail page

def test_detail_not_found(client):
    c = client
    resp = c.get("/jobs/9999")
    assert resp.status_code == 200
    assert "未找到任务 9999" in resp.text


def test_detail_progress_rate_eta_skipped(client):
    c = client
    db = c.app.state.db
    jid = _running_job(db)
    jobs_store.incr_progress(db, jid)
    jobs_store.incr_progress(db, jid)
    jobs_store.incr_progress(db, jid, failed=True)
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET total = 10, "
            "started_at = datetime('now', '-10 seconds') WHERE id = ?", (jid,)
        )
    html = c.get(f"/jobs/{jid}").text
    assert "2/10" in html
    assert "0.2/s" in html              # 速率：done/耗时
    assert "将在当前图片完成后停止" in html  # running 取消文案
    # running 期间不展示「已完成跳过」：total-done-failed 还混着待处理项
    assert "已完成跳过" not in html


def test_detail_skipped_shown_on_done_scan(client):
    """终态 scan：total-done-failed 才真正等于已跳过数。"""
    c = client
    db = c.app.state.db
    jid = _running_job(db)
    jobs_store.incr_progress(db, jid)
    jobs_store.incr_progress(db, jid)
    jobs_store.incr_progress(db, jid, failed=True)
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET total = 10 WHERE id = ?", (jid,))
    jobs_store.finish_job(db, jid)
    html = c.get(f"/jobs/{jid}").text
    assert "已完成跳过" in html          # scan job 跳过数：10-2-1=7
    assert "1 小时延迟" in html          # Immich 索引延迟说明
    assert ">7</span>" in html


def test_detail_eta_and_invalid_rate(client):
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "scan", params={})
    jobs_store.claim_next(db)
    # done=0：速率无效显示 "-"，ETA "-"
    html = c.get(f"/jobs/{jid}").text
    assert ">-</" in html or "> - <" in html or ">-</span>" in html


def test_detail_failure_list_from_records(client):
    c = client
    db = c.app.state.db
    jid = _running_job(db)
    jobs_store.incr_progress(db, jid, failed=True)
    _write_record(db, "asset-err-1", jid, status="error")
    html = c.get(f"/jobs/{jid}").text
    assert "asset-err-1" in html
    assert "失败清单" in html
    assert "/assets/asset-err-1" in html


def test_detail_failure_list_includes_unapplied_ok_records(client):
    """apply 失败挂起重试的记录 status 仍是 'ok' 且 applied_at 为 NULL；
    失败清单必须走 include_unapplied=True 才能列出这些待重试项。"""
    c = client
    db = c.app.state.db
    jid = _running_job(db)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, job_id, source, predicted_tag, "
            "confidence, status, applied_at) VALUES "
            "('asset-retry-1', ?, 'poll', 'landscape', 0.9, 'ok', NULL)",
            (jid,),
        )
    html = c.get(f"/jobs/{jid}").text
    assert "asset-retry-1" in html


def test_detail_failure_list_excludes_dry_run_records(client):
    """沙盒记录 applied_at 恒为 NULL 且 status='ok'；失败清单必须限定
    dry_run=0，否则 dryrun 任务的成功记录会被全列成失败清单。"""
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "dryrun", params={})
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, job_id, source, predicted_tag, "
            "confidence, status, dry_run, applied_at) "
            "VALUES ('sandbox-leak', ?, 'manual', 'landscape', 0.9, 'ok', 1, NULL)",
            (jid,),
        )
    html = c.get(f"/jobs/{jid}").text
    assert "sandbox-leak" not in html


def test_detail_params_snapshot(client):
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "scan", params={"source": "poll", "limit": 5})
    html = c.get(f"/jobs/{jid}").text
    assert "&#34;limit&#34;: 5" in html
    assert "&#34;source&#34;: &#34;poll&#34;" in html


def test_detail_unknown_type_failed_shows_error(client):
    """未知 type 的 job：executor fail 落 failed，详情页展示 error 原文。"""
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "backfill", params={})
    jobs_store.claim_next(db)
    jobs_store.finish_job(db, jid, error="no executor for type 'backfill'")
    html = c.get(f"/jobs/{jid}").text
    assert "no executor for type &#39;backfill&#39;" in html
    # 终态：无取消按钮
    assert "/jobs/%d/cancel" % jid not in html


def test_detail_final_job_no_cancel_button(client):
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "scan", params={})
    jobs_store.claim_next(db)
    jobs_store.finish_job(db, jid)
    html = c.get(f"/jobs/{jid}").text
    assert "cancel" not in html


# ------------------------------------------------------------- cancel

def test_cancel_queued_becomes_canceled(client):
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "scan", params={})
    resp = c.post(f"/jobs/{jid}/cancel")
    assert resp.status_code == 200
    j = jobs_store.get_job(db, jid)
    assert j.status == "canceled"
    assert j.finished_at is not None


def test_cancel_running_only_sets_flag(client):
    c = client
    db = c.app.state.db
    jid = _running_job(db)
    resp = c.post(f"/jobs/{jid}/cancel")
    assert resp.status_code == 200
    j = jobs_store.get_job(db, jid)
    assert j.status == "running"          # status 不变
    with db.connect() as conn:
        flag = conn.execute(
            "SELECT cancel_requested FROM jobs WHERE id = ?", (jid,)
        ).fetchone()["cancel_requested"]
    assert flag == 1


def test_cancel_final_job_no_effect(client):
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "scan", params={})
    jobs_store.claim_next(db)
    jobs_store.finish_job(db, jid)
    resp = c.post(f"/jobs/{jid}/cancel")
    assert resp.status_code == 200
    assert jobs_store.get_job(db, jid).status == "done"


# ------------------------------------------------------------- SSE

def test_sse_job_frames(client):
    c = client
    db = c.app.state.db
    jid = _running_job(db)
    jobs_store.incr_progress(db, jid)
    msgs = _collect_sse_frames(c.app, f"/api/sse/job/{jid}", n_frames=1)
    start = [m for m in msgs if m["type"] == "http.response.start"]
    assert len(start) == 1
    assert start[0]["status"] == 200
    headers = dict(start[0]["headers"])
    assert headers[b"content-type"].startswith(b"text/event-stream")
    body = next(m["body"] for m in msgs if m["type"] == "http.response.body")
    assert body.startswith(b"data: ")
    payload = json.loads(body[len(b"data: "):])
    for key in ("id", "status", "done", "total", "failed"):
        assert key in payload, key
    assert payload["id"] == jid
    assert payload["status"] == "running"


def test_sse_unknown_job():
    """job 不存在时：仍 200 SSE，首帧 status=not_found（选简单方案：
    不做 404 短路，浏览器端 sse-swap 收到 not_found 也不更新数字）。"""
    from app.web.app import create_app as _ca
    tmp = tempfile.mkdtemp()
    c = TestClient(_ca(data_dir=Path(tmp), bootstrap=BootstrapConfig(data_dir=tmp)))
    with c:
        c.app.state.scheduler.stop()
        msgs = _collect_sse_frames(c.app, "/api/sse/job/424242", n_frames=1)
    body = next(m["body"] for m in msgs if m["type"] == "http.response.body")
    payload = json.loads(body[len(b"data: "):])
    assert payload["status"] == "not_found"


# ------------------------------------------------------------- M5 Task 4: 回滚 UI

def _write_changes(db, job_id: int, asset_ids: list[str]) -> None:
    """给 job 写 apply change_log（模拟 Applier 已执行）。"""
    with db.connect() as conn:
        for aid in asset_ids:
            conn.execute(
                "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
                "VALUES (?, ?, 'tag_add', 'apply', '{}')",
                (job_id, aid),
            )


def _done_apply_job(db, n_assets: int = 2) -> int:
    jid = jobs_store.create_job(db, "reclassify", params={})
    jobs_store.claim_next(db)
    jobs_store.finish_job(db, jid)
    _write_changes(db, jid, [f"a{i}" for i in range(n_assets)])
    return jid


def test_undo_button_on_done_job_with_apply_log(client):
    c = client
    db = c.app.state.db
    jid = _done_apply_job(db, n_assets=3)
    html = c.get(f"/jobs/{jid}").text
    assert f'/jobs/{jid}/undo' in html
    assert "回滚此任务" in html
    assert "将撤销 3 条变更（3 张图片）" in html


def test_no_undo_button_on_running_job(client):
    c = client
    db = c.app.state.db
    jid = _running_job(db)
    _write_changes(db, jid, ["a0"])
    assert "/jobs/%d/undo" % jid not in c.get(f"/jobs/{jid}").text


def test_no_undo_button_without_apply_log(client):
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "scan", params={})
    jobs_store.claim_next(db)
    jobs_store.finish_job(db, jid)
    _write_changes(db, jid, ["a0"])
    with db.connect() as conn:
        conn.execute("UPDATE change_log SET direction='undo' WHERE job_id=?", (jid,))
    assert "/jobs/%d/undo" % jid not in c.get(f"/jobs/{jid}").text


def test_no_undo_button_on_undo_job_itself(client):
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "undo", params={"target_job_id": 1})
    jobs_store.claim_next(db)
    jobs_store.finish_job(db, jid)
    _write_changes(db, jid, ["a0"])
    assert "/jobs/%d/undo" % jid not in c.get(f"/jobs/{jid}").text


def test_undo_button_disabled_when_active_undo_exists(client):
    c = client
    db = c.app.state.db
    jid = _done_apply_job(db)
    jobs_store.create_job(db, "undo", params={"target_job_id": jid})
    html = c.get(f"/jobs/{jid}").text
    assert "回滚此任务" in html
    assert "已有回滚任务在进行中" in html
    # 置灰：无 hx-post 确认提交
    assert f'hx-post="/jobs/{jid}/undo"' not in html


def test_post_undo_creates_new_job(client):
    c = client
    db = c.app.state.db
    jid = _done_apply_job(db)
    resp = c.post(f"/jobs/{jid}/undo", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] != f"/jobs/{jid}"
    new_id = int(resp.headers["location"].rstrip("/").split("/")[-1])
    j = jobs_store.get_job(db, new_id)
    assert j.type == "undo"
    assert j.params == {"target_job_id": jid}


def test_post_undo_rejects_nonexistent_job(client):
    c = client
    resp = c.post("/jobs/99999/undo", follow_redirects=False)
    assert resp.status_code == 404


def test_post_undo_rejects_running_job(client):
    c = client
    db = c.app.state.db
    jid = _running_job(db)
    _write_changes(db, jid, ["a0"])
    resp = c.post(f"/jobs/{jid}/undo", follow_redirects=False)
    assert resp.status_code == 404


def test_post_undo_rejects_job_without_apply_log(client):
    c = client
    db = c.app.state.db
    jid = jobs_store.create_job(db, "scan", params={})
    jobs_store.claim_next(db)
    jobs_store.finish_job(db, jid)
    resp = c.post(f"/jobs/{jid}/undo", follow_redirects=False)
    assert resp.status_code == 404


def test_post_undo_rejects_undo_job_itself(client):
    c = client
    db = c.app.state.db
    target = _done_apply_job(db)
    jid = jobs_store.create_job(db, "undo", params={"target_job_id": target})
    jobs_store.claim_next(db)
    jobs_store.finish_job(db, jid)
    _write_changes(db, jid, ["a0"])
    resp = c.post(f"/jobs/{jid}/undo", follow_redirects=False)
    assert resp.status_code == 404


def test_undo_detail_shows_origin_and_changes(client):
    c = client
    db = c.app.state.db
    target = _done_apply_job(db, n_assets=2)
    uid = jobs_store.create_job(db, "undo", params={"target_job_id": target})
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (?, 'a0', 'tag_add', 'undo', '{\"tag\": \"landscape\"}')",
            (uid,),
        )
    html = c.get(f"/jobs/{uid}").text
    assert f"回滚自 #{target}" in html
    assert "撤销明细" in html
    assert "tag_add" in html
    assert "a0" in html
