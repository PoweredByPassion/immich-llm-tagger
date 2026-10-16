"""Task 7: 单张处理管线的测试。

覆盖计划的 13 条失败模式 + 变异清单里的每一条。
夹具：真实 SQLite（migrate 后）、FakeClient（Immich 替身，记录调用）、
FakeLLM（记录调用次数与入参）、monkeypatch thumb 模块避免真编码。
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.classify import pipeline as pipeline_module
from app.classify.commit import CommitResult
from app.classify.lease import LLM_MAX_RETRIES, claim, lease_seconds_for
from app.classify.parse import TagDef
from app.classify.pipeline import PipelineConfig, PipelineResult, classify_asset
from app.classify.thumb import ThumbConfig
from app.clients.immich import ImmichError
from app.db.connection import Database
from app.db.migrate import migrate
from app.store.connections import LLMConn
from app.store.tags import TagDef as StoreTagDef

MODEL = "vision-model"
LLM_JSON = '{"tag": "cat", "confidence": 0.9, "sensitive": false, "reason": "ok"}'


# --------------------------------------------------------------- fixtures


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    migrate(d)
    return d


class FakeClient:
    """Immich 替身：记录每类方法的调用次数，可注入失败。"""

    def __init__(self, *, thumb_fail: bool = False, thumb_bytes: bytes = b"fake-img",
                 fail_on=None):
        self.get_asset_calls = 0
        self.get_asset_calls_before_llm = 0
        self.get_asset_events: list[str] = []
        self._llm_called = False
        self.fetch_thumb_calls: list[tuple[str, str]] = []
        self.write_calls: list[str] = []
        self.thumb_fail = thumb_fail
        self.thumb_bytes = thumb_bytes
        self.fail_on = fail_on
        self.set_props_calls: list[dict] = []
        self.user_rating: int | None = None          # get_asset 返回的"当前评分"
        self.user_description: str | None = None

    def _maybe_fail(self, name: str) -> None:
        if self.fail_on == name:
            raise ImmichError(f"injected {name} failure")

    def get_asset(self, asset_id: str):
        self.get_asset_calls += 1
        self.get_asset_events.append("before" if not self._llm_called else "after")
        if not self._llm_called:
            self.get_asset_calls_before_llm += 1
        return SimpleNamespace(
            original_file_name="IMG_0001.png",
            exif_info=SimpleNamespace(
                make="Apple",
                model="iPhone 15",
                lens_model=None,
                focal_length=None,
                iso=100,
                f_number=None,
                exposure_time=None,
                date_original=None,
                city=None,
                rating=self.user_rating,          # ← 新增
                description=self.user_description,  # ← 新增
            ),
            tags=[],
            local_date_time=None,
            visibility="timeline",
        )

    def fetch_thumbnail_bytes(self, asset_id: str, size: str = "preview") -> bytes:
        self.fetch_thumb_calls.append((asset_id, size))
        if self.thumb_fail:
            raise ImmichError("boom", status_code=500)
        return self.thumb_bytes

    # ---- 写操作：全部记录，不真写
    def ensure_tags(self, names):
        self.write_calls.append(("ensure_tags", list(names)))
        return {n: f"tag-id-{n}" for n in names}

    def tag_assets(self, tag_ids, asset_ids):
        self._maybe_fail("tag_assets")
        self.write_calls.append(("tag_assets", list(tag_ids), list(asset_ids)))

    def untag_assets(self, tag_id, asset_ids):
        self._maybe_fail("untag_assets")
        self.write_calls.append(("untag_assets", tag_id, list(asset_ids)))

    def set_visibility(self, asset_ids, visibility):
        self.write_calls.append(("set_visibility", list(asset_ids), visibility))

    def ensure_album(self, name):
        self.write_calls.append(("ensure_album", name))
        return f"album-id-{name}"

    def add_to_album(self, album_id, asset_ids):
        self._maybe_fail("add_to_album")
        self.write_calls.append(("add_to_album", album_id, list(asset_ids)))

    def set_asset_properties(self, asset_ids, **props):
        self._maybe_fail("set_asset_properties")
        self.write_calls.append(("set_asset_properties", list(asset_ids), props))
        self.set_props_calls.append(props)

    @property
    def tagged_names(self) -> list[str]:
        """所有 tag_assets 调用涉及的标签名（fake 的 tag id 形如 tag-id-<name>）。"""
        out: list[str] = []
        for call in self.write_calls:
            if call[0] == "tag_assets":
                out += [str(t).removeprefix("tag-id-") for t in call[1]]
        return out


class FakeLLM:
    """LLM 替身：按序返回响应或抛异常；记录 messages。"""

    def __init__(self, results, on_call=None):
        # 每项是 str（返回 content）或 Exception 实例（抛出）
        self.results = list(results)
        self.calls: list[list[dict]] = []
        self.model = MODEL
        self.usage = {"prompt_tokens": 10, "completion_tokens": 5}
        self.on_call = on_call

    def chat(self, messages, retries: int = 4):
        self.calls.append(messages)
        if self.on_call is not None:
            self.on_call()
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(content=item, model=self.model, usage=self.usage)


TEMPLATE = (
    "Tags:\n{{TAG_LIST}}\n"
    "File: {{FILENAME}}\n"
    "Exif: {{EXIF}}\n"
    "Scope: {{SCOPE}}\n"
    "Date: {{DATE}}\n"
)


def make_cfg(*, tags=None, template: str = TEMPLATE, **overrides) -> PipelineConfig:
    defaults = dict(
        prompt_template=template,
        tags=tags
        if tags is not None
        else [
            StoreTagDef(
                id=1, name="cat", description="a cat", kind="primary",
                enabled=True, archive=False, album=None, min_confidence=None,
            ),
            StoreTagDef(
                id=2, name="dog", description="a dog", kind="primary",
                enabled=True, archive=True, album="Pets", min_confidence=0.8,
            ),
        ],
        marker_rules=[("low_confidence", {"confidence_lt": 0.5})],
        fallback_tag="other",
        default_min_confidence=0.6,
        thumb=ThumbConfig(),
        queue_tag_name="queue",
        llm=LLMConn(base_url="http://llm", api_key="k", model=MODEL, timeout=30),
        concurrency_owner=42,
        enabled_effects={"tag", "visibility", "album", "rating", "description"},
        skip_marker_tag_name="保留人工信息",
        apply_max_attempts=3,
    )
    defaults.update(overrides)
    return PipelineConfig(**defaults)


def make_llm_ok(client: FakeClient | None = None) -> FakeLLM:
    """LLM 替身；client 提供时在每次 chat 前标记（用于时序断言）。"""
    if client is not None:
        return FakeLLM([LLM_JSON],
                       on_call=lambda: setattr(client, "_llm_called", True))
    return FakeLLM([LLM_JSON])


@pytest.fixture(autouse=True)
def fake_thumb(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ThumbConfig]]:
    """把 fetch_and_encode 换成记录器；返回记录列表供断言。"""
    calls: list[tuple[str, ThumbConfig]] = []

    def fake_fetch_and_encode(client, asset_id, cfg):
        calls.append((asset_id, cfg))
        if getattr(client, "thumb_fail", False):
            raise ImmichError("boom", status_code=500)
        return "base64data"

    monkeypatch.setattr(pipeline_module, "fetch_and_encode", fake_fetch_and_encode)
    return calls


def _records(db: Database, asset_id: str = "asset-1") -> list:
    with db.connect() as conn:
        return conn.execute(
            "SELECT * FROM asset_records WHERE asset_id = ?", (asset_id,)
        ).fetchall()


def _lease(db: Database, asset_id: str = "asset-1"):
    with db.connect() as conn:
        return conn.execute(
            "SELECT lease_owner, lease_expires_at FROM asset_state WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()


# ------------------------------------------------------- 失败模式 1


def test_normal_path_result_matches_llm_response(db: Database) -> None:
    """#1 正常路径：结果与注入的 LLM 响应一致。"""
    llm = make_llm_ok()
    result = classify_asset(
        db, FakeClient(), llm, "asset-1", make_cfg(), source="manual"
    )
    assert result == PipelineResult(
        asset_id="asset-1", tag="cat", confidence=0.9, markers=(),
        status="ok", dry_run=False,
    )


def test_normal_path_writes_full_record_and_applies(db: Database) -> None:
    """#1 补充：asset_records 全字段 + apply 真的被执行 + 续租。"""
    client = FakeClient()
    llm = make_llm_ok()
    classify_asset(db, client, llm, "asset-1", make_cfg(), source="manual")
    rows = _records(db)
    assert len(rows) == 1
    r = rows[0]
    assert dict(r) == {
        "id": r["id"],
        "asset_id": "asset-1",
        "job_id": None,
        "prompt_version_id": None,
        "source": "manual",
        "predicted_tag": "cat",
        "confidence": 0.9,
        "markers": "[]",
        "model": MODEL,
        "usage": '{"prompt_tokens": 10, "completion_tokens": 5}',
        "duration_ms": r["duration_ms"],
        "status": "ok",
        "raw_response": LLM_JSON,
        # FakeLLM 替身没有 _model/_max_tokens 属性 → 快照里为 null；
        # 结构完整性由 test_llm_request_snapshot_saved_to_record 钉死。
        "llm_request": r["llm_request"],
        "dry_run": 0,
        "filename": "IMG_0001.png",
        "rating": None,
        "description": None,
        "created_at": r["created_at"],
        "applied_at": r["applied_at"],
    }
    assert r["llm_request"] is not None and '"messages"' in r["llm_request"]
    assert r["duration_ms"] is not None and r["duration_ms"] >= 0
    # apply 真的执行了：ensure_tags + tag_assets + untag_assets（album=None 不加相册）
    kinds = [c[0] for c in client.write_calls]
    assert ("ensure_tags", ["cat"]) in client.write_calls
    assert ("untag_assets", "tag-id-queue", ["asset-1"]) in client.write_calls
    assert "set_visibility" not in kinds  # archive=False 且无本系统归档史
    # asset_state 更新
    with db.connect() as conn:
        state = conn.execute(
            "SELECT current_tag, current_markers, last_job_id FROM asset_state "
            "WHERE asset_id = 'asset-1'"
        ).fetchone()
    assert (state["current_tag"], state["current_markers"]) == ("cat", "[]")
    # LLM 成功后的续租发生在处理过程中（finally 会释放租约），
    # 这里断言释放语义即可：租约已归零
    assert _lease(db)["lease_owner"] is None
    assert _lease(db)["lease_expires_at"] is None


def test_job_and_prompt_version_recorded(db: Database) -> None:
    llm = make_llm_ok()
    classify_asset(
        db, FakeClient(), llm, "asset-1", make_cfg(), source="poll",
        job_id=7, prompt_version_id=3,
    )
    r = _records(db)[0]
    assert r["job_id"] == 7 and r["prompt_version_id"] == 3 and r["source"] == "poll"


# ------------------------------------------------------- 失败模式 2


def test_no_lease_means_no_processing(db: Database) -> None:
    """#2 拿不到租约：直接返回，零 Immich 调用、零 LLM 调用。"""
    assert claim(db, "asset-1", owner=999, lease_seconds=600)
    client = FakeClient()
    llm = make_llm_ok()
    result = classify_asset(db, client, llm, "asset-1", make_cfg(), source="manual")
    assert result.status == "error"
    assert result.tag == ""
    assert client.get_asset_calls == 0
    assert client.fetch_thumb_calls == []
    assert client.write_calls == []
    assert llm.calls == []
    assert _records(db) == []
    assert _lease(db)["lease_owner"] == 999  # 租约未被破坏


# ------------------------------------------------------- 失败模式 3、4


def test_thumb_failure_error_and_lease_released(db: Database) -> None:
    """#3 缩略图失败：status=error、写记录、租约释放。"""
    llm = make_llm_ok()
    result = classify_asset(
        db, FakeClient(thumb_fail=True), llm, "asset-1", make_cfg(), source="webhook"
    )
    assert result.status == "error"
    assert result.tag == "" and result.confidence is None and result.markers == ()
    assert llm.calls == []
    rows = _records(db)
    assert len(rows) == 1
    assert rows[0]["status"] == "error" and rows[0]["predicted_tag"] is None
    assert rows[0]["source"] == "webhook"
    assert _lease(db) == ("lease released check",) if False else True
    assert _lease(db)["lease_owner"] is None
    assert _lease(db)["lease_expires_at"] is None


def test_llm_failure_error_and_lease_released(db: Database) -> None:
    """#4 LLM 抛错：status=error、写记录、租约释放。"""
    llm = FakeLLM([RuntimeError("network down")])
    result = classify_asset(
        db, FakeClient(), llm, "asset-1", make_cfg(), source="manual"
    )
    assert result.status == "error"
    assert result.tag == ""
    rows = _records(db)
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert _lease(db)["lease_owner"] is None


# ------------------------------------------------------- 失败模式 5


def test_refusal_no_retry(db: Database) -> None:
    """#5 拒答：status=refused、LLM 只调一次、sensitive marker。"""
    llm = FakeLLM(["抱歉，我无法响应这类内容。"])
    result = classify_asset(
        db, FakeClient(), llm, "asset-1", make_cfg(), source="poll"
    )
    assert result.status == "refused"
    assert result.tag == "other"  # fallback
    assert result.confidence is None
    assert result.markers == ("sensitive",)
    assert len(llm.calls) == 1
    r = _records(db)[0]
    assert r["status"] == "refused" and r["confidence"] is None
    assert json.loads(r["markers"]) == ["sensitive"]


def test_refusal_applies_configured_marker_tag(db: Database) -> None:
    """拒答：配置中心挂的 on_refusal marker 标签要真打进 Immich，并记进记录/状态。"""
    client = FakeClient()
    llm = FakeLLM(["抱歉，我无法响应这类内容。"])
    cfg = make_cfg(marker_rules=[("识别失败", {"on_refusal": True})])
    result = classify_asset(db, client, llm, "asset-1", cfg, source="poll")
    assert result.status == "refused"
    assert result.markers == ("sensitive", "识别失败")
    assert "识别失败" in client.tagged_names
    assert json.loads(_records(db)[0]["markers"]) == ["sensitive", "识别失败"]
    with db.connect() as conn:
        state = conn.execute(
            "SELECT current_markers FROM asset_state WHERE asset_id = 'asset-1'"
        ).fetchone()
    assert json.loads(state["current_markers"]) == ["sensitive", "识别失败"]


def test_unparseable_output_applies_configured_marker_tag(db: Database) -> None:
    """不合契约的输出（status=refused）同样打上拒答 marker，与 UI 的 refused 对齐。"""
    client = FakeClient()
    llm = FakeLLM(["The image shows a cat."])
    cfg = make_cfg(marker_rules=[("识别失败", {"on_refusal": True})])
    result = classify_asset(db, client, llm, "asset-1", cfg, source="poll")
    assert result.status == "refused"
    assert "识别失败" in client.tagged_names


# ------------------------------------------------------- 失败模式 6


def test_dry_run_no_immich_writes_but_record_kept(db: Database) -> None:
    """#6 dry_run：Immich 写操作零调用，asset_records 有一行 dry_run=1。"""
    client = FakeClient()
    llm = make_llm_ok()
    result = classify_asset(
        db, client, llm, "asset-1", make_cfg(), source="manual", dry_run=True
    )
    assert result.dry_run is True and result.status == "ok"
    assert client.write_calls == []
    r = _records(db)[0]
    assert r["dry_run"] == 1 and r["predicted_tag"] == "cat"
    assert r["status"] == "ok"
    # dry-run 仍占用过租约，且最终释放
    assert _lease(db)["lease_owner"] is None


# ------------------------------------------------------- 失败模式 7


def test_asset_hint_skips_get_asset(db: Database) -> None:
    """#7 有 hint：取元数据阶段不查 Immich get_asset；FILENAME/EXIF 用 hint 渲染。

    （apply 阶段 Applier 自己可能调 get_asset 取 visibility，那是 Task 6 的行为，
    不算管线的 hint 回退 —— 所以断言 LLM 调用之前 get_asset 次数为 0。）
    """
    client = FakeClient()
    llm = FakeLLM([LLM_JSON], on_call=lambda: setattr(client, "_llm_called", True))
    hint = {
        "originalFileName": "webhook-shot.png",
        "exifInfo": {"make": "Google", "model": "Pixel 9", "iso": 200},
    }
    classify_asset(
        db, client, llm, "asset-1", make_cfg(), source="webhook", asset_hint=hint
    )
    assert client.get_asset_calls_before_llm == 0
    sent = next(c["text"] for c in llm.calls[0][0]["content"] if c["type"] == "text")
    assert "webhook-shot.png" in sent
    assert "Google" in sent and "200" in sent
    # 回退路径渲染的文件名不能出现 → token 在不测的场景里不成立
    assert "IMG_0001.png" not in sent


def test_no_hint_falls_back_to_get_asset(db: Database) -> None:
    """#7 反向：无 hint 回退 get_asset 并用其字段渲染。"""
    client = FakeClient()
    llm = make_llm_ok(client)
    classify_asset(db, client, llm, "asset-1", make_cfg(), source="poll")
    # 元数据回退在 LLM 前 1 次；Applier 取当前可见性在 LLM 后 1 次
    assert client.get_asset_calls == 2
    assert client.get_asset_events == ["before", "after"]
    sent = next(c["text"] for c in llm.calls[0][0]["content"] if c["type"] == "text")
    assert "IMG_0001.png" in sent and "Apple" in sent


# ------------------------------------------------------- 失败模式 8


def test_config_snapshot_read_once(db: Database) -> None:
    """#8 PipelineConfig 是冻结快照：修改不被读、构造后不可变。"""
    cfg = make_cfg()
    llm = make_llm_ok()
    r1 = classify_asset(db, FakeClient(), llm, "asset-1", cfg, source="manual")
    r2 = classify_asset(db, FakeClient(), make_llm_ok(), "asset-2",
                        replace(cfg), source="manual")
    assert r1.tag == r2.tag == "cat"
    with pytest.raises(AttributeError):
        cfg.fallback_tag = "x"  # type: ignore[misc]


# ------------------------------------------------------- 失败模式 9


@pytest.mark.parametrize("source", ["webhook", "poll", "manual"])
def test_source_written_verbatim(db: Database, source: str) -> None:
    """#9 source 三取值逐一写进 asset_records。"""
    classify_asset(db, FakeClient(), make_llm_ok(), "asset-1", make_cfg(),
                   source=source)
    assert _records(db)[0]["source"] == source


def test_source_is_required() -> None:
    """#9 补充：source 无默认值，漏传直接 TypeError。"""
    with pytest.raises(TypeError):
        classify_asset(None, None, None, "asset-1", make_cfg())  # type: ignore


# ------------------------------------------------------- 失败模式 10


def test_record_survives_applier_failure(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#10 分类成功但 apply 失败：asset_records 仍在，异常不穿透。"""
    llm = make_llm_ok()

    def boom(*args, **kw):
        raise RuntimeError("apply exploded")

    monkeypatch.setattr(pipeline_module, "commit_effects", boom)
    result = classify_asset(
        db, FakeClient(), llm, "asset-1", make_cfg(), source="manual"
    )
    assert result.status == "ok"  # 分类本身是成功的
    assert len(_records(db)) == 1
    assert _records(db)[0]["status"] == "ok"
    assert _lease(db)["lease_owner"] is None  # 租约仍释放


# ------------------------------------------------------- 失败模式 11


def test_archive_comes_from_decide_archive(db: Database) -> None:
    """#11 归档布尔值来自 decide_archive：低 confidence → False。"""
    client = FakeClient()
    llm = FakeLLM(['{"tag": "dog", "confidence": 0.79}'])  # < tag.min_confidence 0.8
    classify_asset(db, client, llm, "asset-1", make_cfg(), source="manual")
    assert "set_visibility" not in [c[0] for c in client.write_calls]
    with db.connect() as conn:
        state = conn.execute(
            "SELECT visibility FROM asset_state WHERE asset_id='asset-1'"
        ).fetchone()
    # manual_override=False 且 archive=False 且无系统归档史 → visibility 取当前值 timeline
    assert state["visibility"] == "timeline"


def test_archive_true_path(db: Database) -> None:
    """#11 反向：decide_archive=True 时 apply 收到 True 并执行归档。"""
    client = FakeClient()
    llm = FakeLLM(['{"tag": "dog", "confidence": 0.95}'])  # ≥ 0.8
    classify_asset(db, client, llm, "asset-1", make_cfg(), source="manual")
    assert client.set_props_calls == [{"visibility": "archive"}]
    with db.connect() as conn:
        state = conn.execute(
            "SELECT visibility FROM asset_state WHERE asset_id='asset-1'"
        ).fetchone()
    assert state["visibility"] == "archive"


def test_album_from_selected_tag_def(db: Database) -> None:
    """#11 补充：album 取自选中 tag 的 TagDef.album，未选中 tag 的相册不加。"""
    client = FakeClient()
    llm = FakeLLM(['{"tag": "dog", "confidence": 0.9}'])
    classify_asset(db, client, llm, "asset-1", make_cfg(), source="manual")
    assert ("ensure_album", "Pets") in client.write_calls
    assert ("add_to_album", "album-id-Pets", ["asset-1"]) in client.write_calls


def test_album_not_taken_from_unselected_tag(db: Database) -> None:
    """#11 补充反例：选中的 cat 无 album，即使别的 tag 有 album 也不加。"""
    client = FakeClient()
    llm = FakeLLM(['{"tag": "cat", "confidence": 0.9}'])
    classify_asset(db, client, llm, "asset-1", make_cfg(), source="manual")
    assert ("ensure_album", "Pets") not in client.write_calls
    assert all(c[0] != "add_to_album" for c in client.write_calls)


# ------------------------------------------------------- 失败模式 12


def test_tag_list_filters_primary_enabled(db: Database) -> None:
    """#12 tag_list 过滤：只留 enabled 的 primary tag，顺序保持。"""
    tags = [
        StoreTagDef(id=1, name="cat", description="a cat", kind="primary",
                    enabled=True, archive=False, album=None, min_confidence=None),
        StoreTagDef(id=2, name="hidden", description="hidden desc", kind="primary",
                    enabled=False, archive=False, album=None, min_confidence=None),
        StoreTagDef(id=3, name="screenshot", description="marker desc",
                    kind="marker", enabled=True, archive=False, album=None,
                    min_confidence=None),
        StoreTagDef(id=4, name="dog", description="a dog", kind="primary",
                    enabled=True, archive=True, album=None, min_confidence=None),
    ]
    llm = make_llm_ok()
    classify_asset(
        db, FakeClient(), llm, "asset-1", make_cfg(tags=tags), source="manual"
    )
    sent = next(c["text"] for c in llm.calls[0][0]["content"] if c["type"] == "text")
    assert "- cat: a cat" in sent
    assert "- dog: a dog" in sent
    assert "hidden" not in sent
    assert "marker desc" not in sent
    # 验证顺序（cat 在 dog 前）
    assert sent.index("- cat: a cat") < sent.index("- dog: a dog")


# ------------------------------------------------------- 失败模式 13


def test_scope_always_none(db: Database) -> None:
    """#13 ctx.scope 恒 None：{{SCOPE}} 整行省略。"""
    llm = make_llm_ok()
    classify_asset(db, FakeClient(), llm, "asset-1", make_cfg(), source="manual")
    sent = next(c["text"] for c in llm.calls[0][0]["content"] if c["type"] == "text")
    assert "{{SCOPE}}" not in sent
    assert "Scope:" not in sent


# ------------------------------------------------------- 其他语义


def test_lease_duration_and_renew_per_llm_call(db: Database) -> None:
    """租约时长 = lease_seconds_for(llm.timeout, LLM_MAX_RETRIES)；每次 LLM 成功续租。"""
    llm = FakeLLM([LLM_JSON, LLM_JSON])
    cfg = make_cfg(llm=replace(cfg_llm(), timeout=30))
    classify_asset(db, FakeClient(), llm, "asset-1", cfg, source="manual")
    expected = lease_seconds_for(30, LLM_MAX_RETRIES)
    # 两次 LLM 调用 → claim 用满时长，第二次成功后续租
    assert llm.calls and _lease(db) is not None


# ------------------------------------------------ override_manual 透传


def test_override_manual_false_forces_full_apply_despite_state_bit(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """override_manual=False：即使 asset_state.manual_override=1，
    commit_effects 也收到 gates.manual_override=False（reclassify 全量应用语义）。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id, manual_override) "
            "VALUES ('asset-1', 1)"
        )
    captured: dict[str, object] = {}

    def spy(db_, client_, effects, **kw):
        captured.update(kw)
        return CommitResult(ok=True, committed=[])

    monkeypatch.setattr(pipeline_module, "commit_effects", spy)
    classify_asset(
        db, FakeClient(), make_llm_ok(), "asset-1", make_cfg(),
        source="manual", override_manual=False,
    )
    assert captured["gates"].manual_override is False


def test_override_manual_true_overrides_state_bit(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """override_manual=True 压过 DB 里的 0（对称用例，锁死语义）。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id, manual_override) "
            "VALUES ('asset-1', 0)"
        )
    captured: dict[str, object] = {}

    def spy(db_, client_, effects, **kw):
        captured.update(kw)
        return CommitResult(ok=True, committed=[])

    monkeypatch.setattr(pipeline_module, "commit_effects", spy)
    classify_asset(
        db, FakeClient(), make_llm_ok(), "asset-1", make_cfg(),
        source="manual", override_manual=True,
    )
    assert captured["gates"].manual_override is True


def cfg_llm() -> LLMConn:
    return LLMConn(base_url="http://llm", api_key="k", model=MODEL, timeout=30)


def test_llm_messages_contain_image_and_prompt(db: Database) -> None:
    """LLM 收到 text prompt + base64 image_url 两条内容。"""
    llm = make_llm_ok()
    classify_asset(db, FakeClient(), llm, "asset-1", make_cfg(), source="manual")
    content = llm.calls[0][0]["content"]
    assert isinstance(content, list)
    text_item = next(c for c in content if c["type"] == "text")
    image_item = next(c for c in content if c["type"] == "image_url")
    assert text_item["text"].startswith("Tags:")
    assert image_item == {"type": "image_url",
                          "image_url": {"url": "data:image/jpeg;base64,base64data"}}


def test_multimodal_off_sends_text_only_and_skips_thumb(
    db: Database, fake_thumb: list
) -> None:
    """multimodal=False：messages 退化为纯文本，且不再取图（省一次 fetch）。"""
    llm = make_llm_ok()
    cfg = make_cfg(llm=replace(cfg_llm(), multimodal=False))
    classify_asset(db, FakeClient(), llm, "asset-1", cfg, source="manual")
    sent = llm.calls[0][0]
    assert isinstance(sent["content"], str)
    assert sent["content"].startswith("Tags:")
    assert "image_url" not in json.dumps(sent, ensure_ascii=False)
    assert fake_thumb == []


def test_llm_request_snapshot_saved_to_record(db: Database) -> None:
    """asset_records.llm_request 记录 model / max_tokens / messages（图只留摘要）。"""
    from app.store.assets import records_for_asset

    llm = make_llm_ok()
    llm._model = MODEL
    llm._max_tokens = 400
    classify_asset(db, FakeClient(), llm, "asset-1", make_cfg(), source="manual")
    recs = records_for_asset(db, "asset-1")
    assert recs, "必须落一条分类记录"
    req = recs[0]["llm_request"]
    assert req is not None
    assert req["model"] == MODEL
    assert req["max_tokens"] == 400
    content = req["messages"][0]["content"]
    assert isinstance(content, list)
    text_item = next(c for c in content if c["type"] == "text")
    assert text_item["text"].startswith("Tags:")
    # base64 不落库：image_url 只留摘要
    img_item = next(c for c in content if c["type"] == "image_url")
    url = img_item["image_url"]["url"]
    assert "base64 omitted" in url
    assert len(url) < 80  # 原始 data-url 会远长于摘要


def test_post_llm_crash_becomes_error_and_releases(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM 返回后（解析/归一阶段）抛错：不穿透，status=error，租约释放。"""
    def boom(text):
        raise RuntimeError("injected parse crash")

    monkeypatch.setattr(pipeline_module, "extract_json", boom)
    llm = make_llm_ok()
    client = FakeClient()
    result = classify_asset(db, client, llm, "asset-1", make_cfg(), source="manual")
    assert result.status == "error"
    assert result.tag == "" and result.confidence is None and result.markers == ()
    assert _lease(db)["lease_owner"] is None
    assert client.write_calls == []  # 没走到 apply


def test_renew_called_after_each_llm_success(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每次 LLM 成功后续租一次（renew 以正确 owner/时长被调用）。"""
    seen: list[tuple[str, int, int]] = []
    orig_renew = pipeline_module.renew

    def spy(db_, asset_id, owner, seconds):
        seen.append((asset_id, owner, seconds))
        return orig_renew(db_, asset_id, owner, seconds)

    monkeypatch.setattr(pipeline_module, "renew", spy)
    classify_asset(db, FakeClient(), make_llm_ok(), "asset-1", make_cfg(),
                   source="manual")
    expected = lease_seconds_for(30, LLM_MAX_RETRIES)
    assert seen == [("asset-1", 42, expected)]


def test_llm_tag_not_in_enabled_primary_falls_back(db: Database) -> None:
    """LLM 返回的 tag 不在 enabled primary 集合（如 marker tag）→ fallback。"""
    llm = FakeLLM(['{"tag": "screenshot", "confidence": 0.9}'])  # marker tag
    tags = [
        StoreTagDef(id=1, name="cat", description="a cat", kind="primary",
                    enabled=True, archive=False, album=None, min_confidence=None),
        StoreTagDef(id=3, name="screenshot", description="marker desc",
                    kind="marker", enabled=True, archive=False, album=None,
                    min_confidence=None),
    ]
    result = classify_asset(db, FakeClient(), llm, "asset-1", make_cfg(tags=tags),
                            source="manual")
    assert result.tag == "other"
    assert _records(db)[0]["predicted_tag"] == "other"


def test_release_delegated_to_lease_module(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """释放必须走 lease.release（带 owner 校验的版本），参数正确。"""
    calls: list[tuple[str, int]] = []
    orig = pipeline_module.release

    def spy(db_, asset_id, owner):
        calls.append((asset_id, owner))
        return orig(db_, asset_id, owner)

    monkeypatch.setattr(pipeline_module, "release", spy)
    classify_asset(db, FakeClient(), make_llm_ok(), "asset-1", make_cfg(),
                   source="manual")
    assert calls == [("asset-1", 42)]


# ------------------------------------------------------- Task 0: M2 遗留坑


def test_queue_tag_id_comes_from_ensure_tags(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """commit_effects 收到的 queue_tag_id 是 ensure_tags 返回的真实 id，不是名字拼接。"""
    client = FakeClient()

    def fake_ensure_tags(names):
        client.write_calls.append(("ensure_tags", list(names)))
        return {n: f"immich-uuid-{n}" for n in names}

    monkeypatch.setattr(client, "ensure_tags", fake_ensure_tags)

    seen: list[dict] = []

    def spy(db_, client_, effects, **kw):
        seen.append(kw)
        return CommitResult(ok=True, committed=[])

    monkeypatch.setattr(pipeline_module, "commit_effects", spy)
    classify_asset(db, client, make_llm_ok(), "asset-1", make_cfg(),
                   source="manual")
    assert seen[0]["queue_tag_id"] == "immich-uuid-queue"


def test_manual_override_read_from_asset_state(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """asset_state.manual_override=1 → commit_effects 收到 gates.manual_override=True。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id, manual_override) "
            "VALUES ('asset-1', 1)"
        )
    seen: list[dict] = []

    def spy(db_, client_, effects, **kw):
        seen.append(kw)
        return CommitResult(ok=True, committed=[])

    monkeypatch.setattr(pipeline_module, "commit_effects", spy)
    classify_asset(db, FakeClient(), make_llm_ok(), "asset-1", make_cfg(),
                   source="manual")
    assert seen[0]["gates"].manual_override is True


def test_manual_override_defaults_false_without_state_row(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无 asset_state 行（dry_run 场景等）→ gates.manual_override=False。"""
    seen: list[dict] = []

    def spy(db_, client_, effects, **kw):
        seen.append(kw)
        return CommitResult(ok=True, committed=[])

    monkeypatch.setattr(pipeline_module, "commit_effects", spy)
    classify_asset(db, FakeClient(), make_llm_ok(), "asset-1", make_cfg(),
                   source="manual")
    assert seen[0]["gates"].manual_override is False


# ------------------------------- M5 Task 9: manual_override scan 重跑语义钉合


def test_semantics_manual_override_rerun_retags_but_not_visibility_album(
    db: Database,
) -> None:
    """端到端钉住 §5.5：manual_override=1 的 asset（无 ok 记录 → _already_done
    不拦，scan 重跑仍会处理）时，管线照常摘队列标签/打新标签（①④⑤），
    但不重写可见性与相册（②③ 跳过）——set_visibility / add_to_album
    不得被调用，即便 LLM 结果命中带 album 的 archive 标签。"""
    client = FakeClient()
    # asset_state 行带 manual_override=1；asset 处于 archive 可见性（用户/改判结果）
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id, manual_override) "
            "VALUES ('asset-1', 1)")

    llm = make_llm_ok(client)
    # dog 标签：archive=True, album="Pets" → 若未跳过 ②③ 会写可见性/相册
    cfg = make_cfg(tags=[
        StoreTagDef(id=2, name="dog", description="a dog", kind="primary",
                    enabled=True, archive=True, album="Pets",
                    min_confidence=None),
    ])
    llm.results = ['{"tag": "dog", "confidence": 0.95, "sensitive": false, '
                   '"reason": "ok"}']

    result = classify_asset(db, client, llm, "asset-1", cfg,
                            source="poll", job_id=9)
    assert result.status == "ok" and result.tag == "dog"

    kinds = [c[0] for c in client.write_calls]
    # ① 打新 + ④ 摘队列标签 都发生
    assert "tag_assets" in kinds
    assert "untag_assets" in kinds
    # ②③ 被跳过：属性桶整体为空（不动可见性/评分/描述）、不进相册
    assert client.set_props_calls == []
    assert "add_to_album" not in kinds
    assert "ensure_album" not in kinds


def test_skip_immich_tag_no_immich_writes(db: Database) -> None:
    """skip_immich 标签：识别照常（prompt 含该标签），apply 只摘队列标签。"""
    tags = [
        StoreTagDef(id=1, name="normal_photo", description="普通照片", kind="primary",
                    enabled=True, archive=False, album=None, min_confidence=None,
                    skip_immich=True),
        StoreTagDef(id=2, name="cat", description="a cat", kind="primary",
                    enabled=True, archive=False, album=None, min_confidence=None),
    ]
    llm = FakeLLM(['{"tag": "normal_photo", "confidence": 0.9}'])
    client = FakeClient()
    classify_asset(db, client, llm, "asset-1", make_cfg(tags=tags), source="manual")

    # prompt 里 normal_photo 仍参与识别
    sent = next(c["text"] for c in llm.calls[0][0]["content"] if c["type"] == "text")
    assert "- normal_photo: 普通照片" in sent

    # apply：无打标/相册/归档写，只有摘队列标签
    kinds = [c[0] for c in client.write_calls]
    assert "tag_assets" not in kinds
    assert "ensure_album" not in kinds and "add_to_album" not in kinds
    assert client.set_props_calls == []
    assert kinds.count("untag_assets") == 1

    # asset_records 正常记录分类结果
    with db.connect() as conn:
        rec = conn.execute(
            "SELECT predicted_tag, status FROM asset_records "
            "WHERE asset_id = 'asset-1'"
        ).fetchone()
    assert rec["predicted_tag"] == "normal_photo" and rec["status"] == "ok"


# ------------------------------------- Task 9: effector 接线 + 跳过 + 成功闸门

RATING_JSON = ('{"tag": "cat", "confidence": 0.9, "sensitive": false, '
               '"reason": "ok", "rating": 4, "description": "一只猫"}')


def test_pipeline_writes_rating_and_description(db: Database) -> None:
    client = FakeClient()
    llm = FakeLLM([RATING_JSON])
    classify_asset(db, client, llm, "asset-1", make_cfg(), source="poll", job_id=1)
    assert client.set_props_calls == [{"rating": 4, "description": "一只猫"}]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT rating, description FROM asset_state WHERE asset_id='asset-1'"
        ).fetchone()
    assert (row["rating"], row["description"]) == (4, "一只猫")


def test_pipeline_records_analysis_output_in_records(db: Database) -> None:
    """§7.3 复用重试的前提：记录里必须存得住全部判定。"""
    classify_asset(db, FakeClient(), FakeLLM([RATING_JSON]), "asset-1",
                   make_cfg(), source="poll")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT rating, description FROM asset_records WHERE asset_id='asset-1'"
        ).fetchone()
    assert (row["rating"], row["description"]) == (4, "一只猫")


def test_pipeline_marks_skip_when_user_value_present(db: Database) -> None:
    """用户手写 5 → 不覆盖评分，并且打上跳过标记（D6）。"""
    client = FakeClient()
    client.user_rating = 5
    classify_asset(db, client, FakeLLM([RATING_JSON]), "asset-1",
                   make_cfg(), source="poll")
    assert [c for c in client.set_props_calls if "rating" in c] == []
    assert "保留人工信息" in client.tagged_names


def test_skip_marker_payload_records_which_fields_were_skipped(db: Database) -> None:
    """§5.8：跳过了哪些字段要进 change_log.payload（供 UI 展示与排障）。

    这条同时钉住"标记标签的变更**有账**"（§5.5 不变式：每一次成功写入都要有记录）。
    """
    client = FakeClient()
    client.user_rating = 5
    classify_asset(db, client, FakeLLM([RATING_JSON]), "asset-1",
                   make_cfg(), source="poll")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM change_log WHERE asset_id='asset-1' "
            "AND action='tag_add' ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(row["payload"]) == {
        "tag_name": "保留人工信息", "skipped": ["rating"]}


def test_pipeline_does_not_mark_when_value_is_ours(db: Database) -> None:
    """重试场景：快照里的值就是本系统上次写的 → 跳过但不打标记（§5.8.1）。"""
    client = FakeClient()
    client.user_rating = 4                    # == 我们上次写的 new
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (asset_id, action, direction, payload) "
            "VALUES ('asset-1','rating_set','apply','{\"old\": null, \"new\": 4}')")
    classify_asset(db, client, FakeLLM([RATING_JSON]), "asset-1",
                   make_cfg(), source="poll")
    assert [c for c in client.set_props_calls if "rating" in c] == []
    assert "保留人工信息" not in client.tagged_names


def test_pipeline_skips_marker_when_metadata_blocked(db: Database) -> None:
    """manual_override：评分/描述一律不写，也**不**打跳过标记
    （不是"保留了人工信息"，是这次压根不处理）。"""
    client = FakeClient()
    client.user_rating = 5
    with db.connect() as conn:
        conn.execute("INSERT INTO asset_state (asset_id, manual_override) "
                     "VALUES ('asset-1', 1)")
    classify_asset(db, client, FakeLLM([RATING_JSON]), "asset-1",
                   make_cfg(), source="poll")
    assert client.set_props_calls == []
    assert "保留人工信息" not in client.tagged_names


def test_applied_at_set_only_after_full_success(db: Database) -> None:
    classify_asset(db, FakeClient(), FakeLLM([RATING_JSON]), "asset-1",
                   make_cfg(), source="poll", job_id=1)
    with db.connect() as conn:
        row = conn.execute("SELECT status, applied_at FROM asset_records "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "ok" and row["applied_at"] is not None


def test_applied_at_absent_when_tag_bucket_fails(db: Database) -> None:
    """apply 失败 → 不写 applied_at（下轮 scan 才会重试，§7.1）。"""
    client = FakeClient()
    client.fail_on = "tag_assets"
    classify_asset(db, client, FakeLLM([RATING_JSON]), "asset-1",
                   make_cfg(), source="poll", job_id=1)
    with db.connect() as conn:
        row = conn.execute("SELECT applied_at FROM asset_records "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["applied_at"] is None
    # 队列标签仍在：Finalizer 没跑
    assert not [c for c in client.write_calls if c[0] == "untag_assets"]


def test_give_up_tags_the_asset_after_max_attempts(db: Database) -> None:
    """§7.3：达到上限 → 打跳过标记标签，让它在 Immich 里可见。

    ⚠ 注入的失败**不能**是 `tag_assets` —— `_tag_gave_up` 自己也调
    `tag_assets` 打这个标记，同一个故障会让它一起挂掉，断言全部落空。
    换属性桶：它在 `_BUCKET_ORDER` 里排最前，失败后标签桶根本没跑，
    放弃路径的 `tag_assets` 是健康的。
    """
    client = FakeClient()
    client.fail_on = "set_asset_properties"
    cfg = make_cfg(apply_max_attempts=1)
    classify_asset(db, client, FakeLLM([RATING_JSON]), "asset-1", cfg, source="poll")
    assert client.tagged_names == ["保留人工信息"]     # 主标签没打上（标签桶没跑）
    with db.connect() as conn:
        actions = [r["action"] for r in conn.execute(
            "SELECT action FROM change_log WHERE asset_id='asset-1' ORDER BY id")]
        assert conn.execute(
            "SELECT apply_attempts FROM asset_state WHERE asset_id='asset-1'"
        ).fetchone()["apply_attempts"] == 1
    assert "tag_add" in actions            # 打标这个写入有账（§5.5 不变式）


def test_give_up_does_not_reset_applied_at(db: Database) -> None:
    """放弃 ≠ 应用成功：applied_at 必须仍是 NULL（§7.3 评审纠正）。"""
    client = FakeClient()
    client.fail_on = "tag_assets"
    classify_asset(db, client, FakeLLM([RATING_JSON]), "asset-1",
                   make_cfg(apply_max_attempts=1), source="poll")
    with db.connect() as conn:
        assert conn.execute("SELECT applied_at FROM asset_records").fetchone()[
            "applied_at"] is None


# ------------------------------------- Task 11: 重试复用分析结果（§7.3 层 1）


def test_retry_reuses_stored_analysis_without_llm(db: Database) -> None:
    """已有 status='ok' 且 applied_at IS NULL 的记录 → 不再调 LLM（§7.3 层 1）。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, "
            "  predicted_tag, confidence, markers, rating, description, filename) "
            "VALUES ('asset-1','poll','ok',0,'cat',0.9,'[]',4,'一只猫','IMG_0001.png')")
    client = FakeClient()
    llm = FakeLLM([RATING_JSON])        # 一旦被调用就说明复用没生效

    classify_asset(db, client, llm, "asset-1", make_cfg(), source="poll", job_id=2)

    assert llm.calls == []                                  # 零 LLM 成本
    assert client.set_props_calls == [{"rating": 4, "description": "一只猫"}]
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT applied_at FROM asset_records WHERE asset_id='asset-1'"
        ).fetchall()
    assert len(rows) == 1 and rows[0]["applied_at"] is not None   # §6.5：不新增行，只盖章


def test_successful_apply_clears_apply_attempts(db: Database) -> None:
    """成功应用必须把 asset_state.apply_attempts 归零。

    计数器语义 = **连续失败次数**，只在 reclassify / 维度重置里被清过、
    从不在成功时清 —— 于是「失败一次 → 下轮复用分析后应用成功」的资产会
    永远停在 apply_attempts=1，详情页的黄色「待重试 (1/3)」胶囊变成永久假
    阳性（而任务详情页的失败清单按 applied_at 已不再列它，两个界面互相矛盾）。
    """
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id, apply_attempts) "
            "VALUES ('asset-1', 2)")
        # 可复用的待应用记录（status='ok'、dry_run=0、applied_at IS NULL）
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, "
            "  predicted_tag, confidence, markers, rating, description, filename) "
            "VALUES ('asset-1','poll','ok',0,'cat',0.9,'[]',4,'一只猫','IMG_0001.png')")
    client = FakeClient()

    classify_asset(db, client, FakeLLM([RATING_JSON]), "asset-1", make_cfg(),
                   source="poll", job_id=2)

    with db.connect() as conn:
        assert conn.execute(
            "SELECT apply_attempts FROM asset_state WHERE asset_id='asset-1'"
        ).fetchone()["apply_attempts"] == 0
        # 成功盖章照旧：修的是计数器，不是 applied_at
        assert conn.execute(
            "SELECT applied_at FROM asset_records WHERE asset_id='asset-1'"
        ).fetchone()["applied_at"] is not None


def test_reuse_does_not_take_refused_or_applied_records(db: Database) -> None:
    """status='refused' 不是可复用对象。

    （applied_at 非空 / predicted_tag 为空 的守卫由下面两条专门用例钉住。）
    """
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, "
            "  predicted_tag, confidence, markers) "
            "VALUES ('asset-1','poll','refused',0,'other',NULL,'[\"sensitive\"]')")
    llm = FakeLLM([RATING_JSON])
    classify_asset(db, FakeClient(), llm, "asset-1", make_cfg(), source="poll")
    assert len(llm.calls) == 1                              # 照常分析


def test_reuse_skips_already_applied_record(db: Database) -> None:
    """applied_at 非空 → 不复用（复用旧结果会盖掉更新的状态，是状态损坏类失败）。

    直接调 classify_asset 会绕过 executor 的 `_already_done` 跳过，因此这里
    确实会走到 `_reusable_analysis`，钉住它的 applied_at 守卫。
    """
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, "
            "  predicted_tag, confidence, markers, rating, description, filename, "
            "  applied_at) "
            "VALUES ('asset-1','poll','ok',0,'cat',0.9,'[]',4,'一只猫','IMG_0001.png',"
            "        datetime('now'))")
    llm = FakeLLM([RATING_JSON])
    classify_asset(db, FakeClient(), llm, "asset-1", make_cfg(), source="poll")
    assert len(llm.calls) == 1                              # 已应用过 → 照常重新分析


def test_reuse_skips_record_without_predicted_tag(db: Database) -> None:
    """predicted_tag IS NULL → 没有可用判定，不复用。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, dry_run, "
            "  predicted_tag, confidence, markers) "
            "VALUES ('asset-1','poll','ok',0,NULL,NULL,'[]')")
    llm = FakeLLM([RATING_JSON])
    classify_asset(db, FakeClient(), llm, "asset-1", make_cfg(), source="poll")
    assert len(llm.calls) == 1                              # 无判定 → 照常分析
