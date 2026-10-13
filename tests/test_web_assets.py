"""assets 路由契约：列表筛选/分页/empty、详情（历史+raw 折叠+404）、
从 Immich 同步（mock ImmichClient，单测不联网；成功/失败/未配置三态）。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store.connections import ConnectionStore, ImmichConn
from app.web.routes import assets_ui as assets_ui_module
from app.web.app import create_app

ASSET_ID = "asset-abc-123"


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _build_client(data_dir: Path) -> TestClient:
    app = create_app(
        data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir))
    )
    # app.py 的 include_router 由主线统一加；测试内本地挂载
    app.include_router(assets_ui_module.router)
    return TestClient(app)


@pytest.fixture()
def client(data_dir: Path):
    c = _build_client(data_dir)
    with c:
        yield c, c.app.state.db


def _insert_record(db, asset_id: str, **kw) -> int:
    row = {
        "asset_id": asset_id,
        "job_id": 1,
        "source": "webhook",
        "predicted_tag": "风景",
        "confidence": 0.92,
        "markers": "[]",
        "model": "qwen-vl",
        "usage": json.dumps({"prompt_tokens": 10, "completion_tokens": 5}),
        "duration_ms": 1234,
        "status": "ok",
        "raw_response": "RAW-RESPONSE-CONTENT",
        "created_at": "2026-10-12 08:00:00",
    }
    row.update(kw)
    cols = list(row.keys())
    with db.connect() as conn:
        cur = conn.execute(
            f"INSERT INTO asset_records ({', '.join(cols)}) "
            f"VALUES ({', '.join(':' + c for c in cols)})",
            row,
        )
        return cur.lastrowid


def _insert_state(db, asset_id: str, **kw) -> None:
    row = {
        "asset_id": asset_id,
        "current_tag": "风景",
        "current_markers": "[]",
        "visibility": "timeline",
        "manual_override": 0,
        "reviewed": 0,
    }
    row.update(kw)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id, current_tag, current_markers, "
            "visibility, manual_override, reviewed, last_job_id) VALUES (:asset_id, "
            ":current_tag, :current_markers, :visibility, :manual_override, "
            ":reviewed, :last_job_id)",
            {**row, "last_job_id": row.get("last_job_id")},
        )


def _insert_tag(db, name: str, kind: str = "primary", enabled: int = 1) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO tags (name, description, kind, enabled, archive, album) "
            "VALUES (?, '', ?, ?, 0, NULL)",
            (name, kind, enabled),
        )


def _fake_asset(tags, visibility="timeline"):
    class _T:
        def __init__(self, name):
            self.name = name

    class _A:
        def __init__(self):
            self.tags = [_T(n) for n in tags]

            class _V:
                value = visibility

            self.visibility = _V()

    return _A()


# ------------------------------------------------------------------- 列表

def test_list_empty_state(client):
    c, _ = client
    resp = c.get("/assets")
    assert resp.status_code == 200
    assert "暂无图片记录" in resp.text


def test_list_shows_rows_and_uncategorized(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    _insert_state(db, ASSET_ID)
    html = c.get("/assets").text
    assert ASSET_ID in html
    assert "风景" in html
    assert "未分类" not in html  # 有 JOIN current_tag 时不出现


def test_list_uncategorized_when_no_state(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    _insert_record(db, "asset-other", predicted_tag=None)
    html = c.get("/assets").text
    assert "未分类" in html


def test_list_filter_status(client):
    c, db = client
    _insert_record(db, "a1", status="ok")
    _insert_record(db, "a2", status="error")
    html = c.get("/assets", params={"status": "error"}).text
    assert "a2" in html
    assert "a1" not in html


def test_list_filter_current_tag(client):
    c, db = client
    _insert_record(db, "a1")
    _insert_record(db, "a2", predicted_tag="人像")
    _insert_state(db, "a2", current_tag="人像")
    html = c.get("/assets", params={"current_tag": "人像"}).text
    assert "a2" in html
    assert "a1" not in html


def test_list_filter_source(client):
    c, db = client
    _insert_record(db, "a1", source="webhook")
    _insert_record(db, "a2", source="poll")
    html = c.get("/assets", params={"source": "poll"}).text
    assert "a2" in html
    assert "a1" not in html


def test_list_filter_job_id(client):
    c, db = client
    _insert_record(db, "a1", job_id=1)
    _insert_record(db, "a2", job_id=2)
    html = c.get("/assets", params={"job_id": "2"}).text
    assert "a2" in html
    assert "a1" not in html


def test_list_pagination_boundaries(client):
    c, db = client
    # id DESC：id 越大越靠前。asset-000..asset-054 → 第 1 页是 054..005
    for i in range(55):
        _insert_record(db, f"asset-{i:03d}")
    page1 = c.get("/assets").text
    page2 = c.get("/assets", params={"page": "2"}).text
    assert "asset-054" in page1
    assert "asset-004" not in page1
    assert "asset-004" in page2
    assert "asset-000" in page2
    assert "asset-005" not in page2


def test_list_pagination_links(client):
    c, db = client
    for i in range(55):
        _insert_record(db, f"asset-{i:03d}")
    p1 = c.get("/assets").text
    assert "下一页" in p1
    assert "上一页" not in p1
    p2 = c.get("/assets", params={"page": "2"}).text
    assert "上一页" in p2
    assert "下一页" not in p2


def test_list_page_beyond_range_clamps_to_last_page(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    resp = c.get("/assets", params={"page": "99"})
    assert resp.status_code == 200
    # 越界页钳制到最后一页，仍能看到记录且不空
    assert ASSET_ID in resp.text


# ------------------------------------------------------------------- 详情

def test_detail_shows_state_history_and_raw(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    _insert_state(db, ASSET_ID, current_markers=json.dumps(["天空", "夜景"]))
    html = c.get(f"/assets/{ASSET_ID}").text
    assert "风景" in html
    assert "timeline" in html
    assert ASSET_ID in html
    assert "RAW-RESPONSE-CONTENT" in html  # details 折叠里也要在 DOM
    assert "qwen-vl" in html
    assert "1234" in html
    assert "webhook" in html


def test_detail_badges_readonly(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    _insert_state(db, ASSET_ID, manual_override=1, reviewed=1)
    html = c.get(f"/assets/{ASSET_ID}").text
    assert "人工改判" in html
    assert "已复核" in html


def test_detail_404_when_no_records(client):
    c, _ = client
    resp = c.get("/assets/no-such-asset")
    assert resp.status_code == 404


# ------------------------------------------------------------------- 同步

class _FakeClient:
    instances = []
    asset = None
    thumbnail = b"\xff\xd8\xff\xe0THUMB-JPEG"
    thumb_error = None  # None 或 status_code

    def __init__(self, *, base_url, api_key, verify_ssl, timeout, **kw):
        assert timeout == 30.0 or timeout > 0
        self.base_url = base_url
        assets_ui_module._fake_instances.append(self)

    def get_asset(self, asset_id):
        if _FakeClient.asset is None:
            raise assets_ui_module.ImmichError("boom")
        return _FakeClient.asset

    def fetch_thumbnail_bytes(self, asset_id, size="preview"):
        if _FakeClient.thumb_error is not None:
            raise assets_ui_module.ImmichError("thumb fail", _FakeClient.thumb_error)
        return _FakeClient.thumbnail

    @classmethod
    def reset(cls):
        cls.asset = None
        cls.thumb_error = None
        cls.thumbnail = b"\xff\xd8\xff\xe0THUMB-JPEG"
        assets_ui_module._fake_instances = []


@pytest.fixture()
def mock_client(monkeypatch):
    _FakeClient.reset()
    monkeypatch.setattr(assets_ui_module, "ImmichClient", _FakeClient)
    return _FakeClient


def test_sync_success_updates_state(client, mock_client):
    c, db = client
    ConnectionStore(db).set_immich(
        ImmichConn(url="http://immich.example", api_key="k")
    )
    _insert_state(db, ASSET_ID, current_tag="旧标签")
    _insert_tag(db, "风景", "primary")
    _insert_tag(db, "天空", "marker")
    _insert_tag(db, "人像", "primary")
    _insert_tag(db, "无关标签", "primary")
    _insert_tag(db, "未分类", "primary")
    mock_client.asset = _fake_asset(
        ["风景", "天空", "无关标签"], visibility="archive"
    )
    resp = c.post(f"/assets/{ASSET_ID}/sync")
    assert resp.status_code == 200
    assert "已同步" in resp.text
    # 系统标签口径：只保留 tags 表内 primary/marker；"无关标签" 被写回过所以也在表里…
    with db.connect() as conn:
        row = conn.execute(
            "SELECT current_tag, current_markers, visibility FROM asset_state "
            "WHERE asset_id = ?",
            (ASSET_ID,),
        ).fetchone()
    assert row["current_tag"] == "风景"
    assert json.loads(row["current_markers"]) == ["天空"]
    assert row["visibility"] == "archive"
    assert "archive" in resp.text
    assert "旧标签" not in resp.text


def test_sync_kind_split_primary_vs_marker(client, mock_client):
    """标签口径变异专项：marker 标签不能成为 current_tag（即使排在最前），
    primary 标签不能进 current_markers。"""
    c, db = client
    ConnectionStore(db).set_immich(
        ImmichConn(url="http://immich.example", api_key="k")
    )
    _insert_state(db, ASSET_ID)
    _insert_tag(db, "天空", "marker")
    _insert_tag(db, "风景", "primary")
    mock_client.asset = _fake_asset(["天空", "风景"], visibility="timeline")
    c.post(f"/assets/{ASSET_ID}/sync")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT current_tag, current_markers FROM asset_state WHERE asset_id = ?",
            (ASSET_ID,),
        ).fetchone()
    assert row["current_tag"] == "风景"
    assert json.loads(row["current_markers"]) == ["天空"]


def test_sync_failure_renders_error_not_500(client, mock_client):
    c, db = client
    ConnectionStore(db).set_immich(
        ImmichConn(url="http://immich.example", api_key="k")
    )
    _insert_state(db, ASSET_ID)
    mock_client.asset = None  # get_asset 抛 ImmichError
    resp = c.post(f"/assets/{ASSET_ID}/sync")
    assert resp.status_code == 200
    assert "失败" in resp.text or "错误" in resp.text


def test_sync_no_connection_config(client, mock_client):
    c, db = client
    _insert_state(db, ASSET_ID)
    resp = c.post(f"/assets/{ASSET_ID}/sync")
    assert resp.status_code == 200
    assert "尚未配置" in resp.text


def test_sync_no_asset_state(client, mock_client):
    c, db = client
    ConnectionStore(db).set_immich(
        ImmichConn(url="http://immich.example", api_key="k")
    )
    resp = c.post("/assets/ghost/sync")
    assert resp.status_code == 200
    assert "暂无记录" in resp.text
    # 未发起 Immich 调用
    assert assets_ui_module._fake_instances == []


def test_sync_timeout_propagates_as_error(client, mock_client, monkeypatch):
    c, db = client
    ConnectionStore(db).set_immich(
        ImmichConn(url="http://immich.example", api_key="k")
    )
    _insert_state(db, ASSET_ID)

    class _TimeoutClient(_FakeClient):
        def get_asset(self, asset_id):
            raise assets_ui_module.ImmichError("timed out")

    monkeypatch.setattr(assets_ui_module, "ImmichClient", _TimeoutClient)
    resp = c.post(f"/assets/{ASSET_ID}/sync")
    assert resp.status_code == 200
    assert "timed out" in resp.text


# ------------------------------------------------ M5 Task 6: dry_run 筛选


def test_list_dry_run_filter(client):
    c, db = client
    _insert_record(db, "a-real", dry_run=0)
    _insert_record(db, "a-sandbox", dry_run=1, source="manual")
    html = c.get("/assets?dry_run=1").text
    assert "a-sandbox" in html
    assert "a-real" not in html
    # 沙盒标注
    assert "沙盒结果基于当前激活 prompt 版本" in html
    html_all = c.get("/assets").text
    assert "a-real" in html_all and "a-sandbox" in html_all
    # 筛选下拉有三态
    assert "全部" in html and "沙盒" in html and "正式" in html


def test_list_dry_run_filter_false(client):
    c, db = client
    _insert_record(db, "a-real", dry_run=0)
    _insert_record(db, "a-sandbox", dry_run=1)
    html = c.get("/assets?dry_run=0").text
    assert "a-real" in html
    assert "a-sandbox" not in html


# ------------------------------------------------ M5 Task 8: 复核 / 人工改判


class _OverrideClient:
    """manual.py 的 Immich 替身（web 层）：记录 untag/tag 调用即可。"""

    def __init__(self, *, base_url, api_key, verify_ssl, timeout, **kw):
        assets_ui_module._fake_instances.append(self)

    def get_asset(self, asset_id):
        class _T:
            def __init__(self, name):
                self.name = name

        class _A:
            tags = [_T("风景")]

        return _A()

    def ensure_tags(self, names):
        return {n: f"id-{n}" for n in names}

    def tag_assets(self, tag_ids, asset_ids):
        pass

    def untag_assets(self, tag_id, asset_ids):
        pass


@pytest.fixture()
def override_client(monkeypatch):
    monkeypatch.setattr(
        "app.classify.manual.ImmichClient", _OverrideClient
    )
    monkeypatch.setattr(
        "app.web.routes.assets_ui.ImmichClient", _OverrideClient
    )
    return _OverrideClient


def test_detail_review_override_controls_render(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    _insert_state(db, ASSET_ID)
    _insert_tag(db, "风景", "primary")
    _insert_tag(db, "天空", "marker")
    _insert_tag(db, "停用标签", "primary", enabled=0)
    html = c.get(f"/assets/{ASSET_ID}").text
    # 复核按钮（POST /review）
    assert 'hx-post="/assets/' + ASSET_ID + '/review"' in html
    assert "标记已复核" in html
    # 改判表单：primary 下拉只含 enabled primary，marker 多选只含 enabled marker
    assert "/assets/" + ASSET_ID + "/override" in html
    assert "<option" in html and "风景" in html
    assert "天空" in html
    assert "停用标签" not in html.split("分类历史")[0]


def test_detail_reviewed_toggle_labels_and_badges(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    _insert_state(db, ASSET_ID, reviewed=1, manual_override=1)
    html = c.get(f"/assets/{ASSET_ID}").text
    assert "已复核" in html
    assert "取消复核" in html
    assert "人工改判" in html
    assert "取消改判" in html
    assert "/override/clear" in html
    # 覆盖确认文案分支
    assert "此操作将覆盖你的手工判定" in html


def test_detail_not_reviewed_no_override_confirm_text(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    _insert_state(db, ASSET_ID)
    html = c.get(f"/assets/{ASSET_ID}").text
    assert "取消复核" not in html
    assert "此操作将覆盖你的手工判定" not in html


def test_post_review_marks(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    resp = c.post(f"/assets/{ASSET_ID}/review")
    assert resp.status_code == 200
    assert "已复核" in resp.text
    with db.connect() as conn:
        row = conn.execute(
            "SELECT reviewed FROM asset_state WHERE asset_id = ?", (ASSET_ID,)
        ).fetchone()
    assert row["reviewed"] == 1
    # 不写 change_log
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM change_log").fetchone()["c"]
    assert n == 0


def test_post_review_unmarks(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    _insert_state(db, ASSET_ID, reviewed=1)
    resp = c.post(f"/assets/{ASSET_ID}/review")
    assert "取消复核" in resp.text or "已取消" in resp.text
    with db.connect() as conn:
        row = conn.execute(
            "SELECT reviewed FROM asset_state WHERE asset_id = ?", (ASSET_ID,)
        ).fetchone()
    assert row["reviewed"] == 0


def test_post_override_applies(client, override_client):
    c, db = client
    ConnectionStore(db).set_immich(
        ImmichConn(url="http://immich.example", api_key="k")
    )
    _insert_record(db, ASSET_ID)
    _insert_tag(db, "人像", "primary")
    _insert_tag(db, "天空", "marker")
    _insert_tag(db, "风景", "primary")  # _OverrideClient 的 asset 上有"风景"
    resp = c.post(
        f"/assets/{ASSET_ID}/override",
        data={"tag": "人像", "markers": ["天空"]},
    )
    assert resp.status_code == 200
    assert "改判" in resp.text
    with db.connect() as conn:
        st = conn.execute(
            "SELECT * FROM asset_state WHERE asset_id = ?", (ASSET_ID,)
        ).fetchone()
        logs = conn.execute(
            "SELECT * FROM change_log WHERE asset_id = ?", (ASSET_ID,)
        ).fetchall()
    assert st["manual_override"] == 1
    assert st["current_tag"] == "人像"
    assert all(l["job_id"] is None for l in logs)
    assert any(l["action"] == "tag_remove" for l in logs)
    assert any(l["action"] == "tag_add" for l in logs)


def test_post_override_missing_tag_rejected(client, override_client):
    c, db = client
    _insert_record(db, ASSET_ID)
    resp = c.post(f"/assets/{ASSET_ID}/override", data={"tag": ""})
    assert resp.status_code == 200
    assert "请选择" in resp.text or "失败" in resp.text


def test_post_override_clear(client, override_client):
    c, db = client
    _insert_record(db, ASSET_ID)
    _insert_state(db, ASSET_ID, manual_override=1, current_tag="人像")
    resp = c.post(f"/assets/{ASSET_ID}/override/clear")
    assert resp.status_code == 200
    with db.connect() as conn:
        row = conn.execute(
            "SELECT manual_override, current_tag FROM asset_state "
            "WHERE asset_id = ?",
            (ASSET_ID,),
        ).fetchone()
    assert row["manual_override"] == 0
    assert row["current_tag"] == "人像"  # 不回滚标签


# ------------------------------------------------- M6 Task 2: 重新分类按钮

def test_detail_has_reclassify_button(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    html = c.get(f"/assets/{ASSET_ID}").text
    assert 'action="/assets/' + ASSET_ID + '/reclassify"' in html
    assert "重新分类" in html
    assert "将摘掉本系统管理的标签并重新分类" in html


def test_detail_reclassify_override_warning(client):
    c, db = client
    _insert_record(db, ASSET_ID)
    _insert_state(db, ASSET_ID, manual_override=1)
    html = c.get(f"/assets/{ASSET_ID}").text
    assert "此操作将覆盖你的手工判定" in html


def test_post_reclassify_creates_job(client, monkeypatch):
    c, db = client
    _insert_record(db, ASSET_ID)
    monkeypatch.setattr(
        "app.engine.executor.run_job",
        lambda *a, **k: None, raising=False)
    resp = c.post(f"/assets/{ASSET_ID}/reclassify", follow_redirects=False)
    assert resp.status_code == 303
    from app.store import jobs as jobs_store
    job = [j for j in jobs_store.list_jobs(db) if j.type == "reclassify"][0]
    assert job.params == {"asset_ids": [ASSET_ID]}
    assert resp.headers["location"] == f"/jobs/{job.id}"


def test_detail_expired_source_job_shows_stale_text(client):
    """last_job_id 指向的 job 不存在时显示"来源 Job 已过期"，不渲染跳转链接。"""
    c, db = client
    _insert_record(db, ASSET_ID)
    _insert_state(db, ASSET_ID, last_job_id=999)
    html = c.get(f"/assets/{ASSET_ID}").text
    assert "来源 Job 已过期" in html
    assert 'href="/jobs/999"' not in html


def test_detail_existing_source_job_renders_link(client):
    c, db = client
    _insert_record(db, ASSET_ID, job_id=7)
    _insert_state(db, ASSET_ID, last_job_id=7)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO jobs (id, type, status) VALUES (7, 'scan', 'done')"
        )
    html = c.get(f"/assets/{ASSET_ID}").text
    assert 'href="/jobs/7"' in html


# ------------------------------------------- 任务 B: 缩略图代理 + 时间显示

def test_thumb_returns_jpeg_bytes(client, mock_client):
    c, db = client
    ConnectionStore(db).set_immich(
        ImmichConn(url="http://immich.example", api_key="k")
    )
    mock_client.thumbnail = b"\xff\xd8\xff\xe0RAW-JPEG-BYTES"
    resp = c.get(f"/assets/{ASSET_ID}/thumb")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == b"\xff\xd8\xff\xe0RAW-JPEG-BYTES"
    assert assets_ui_module._fake_instances  # 确实构造了客户端


def test_thumb_immich_error_returns_404(client, mock_client):
    c, db = client
    ConnectionStore(db).set_immich(
        ImmichConn(url="http://immich.example", api_key="k")
    )
    mock_client.thumb_error = 404
    resp = c.get(f"/assets/{ASSET_ID}/thumb")
    assert resp.status_code == 404


def test_thumb_no_connection_returns_404(client, mock_client):
    c, _ = client
    resp = c.get(f"/assets/{ASSET_ID}/thumb")
    assert resp.status_code == 404
    # 未配置连接时不构造客户端
    assert assets_ui_module._fake_instances == []


def test_list_renders_created_at_and_thumb_img(client):
    c, db = client
    _insert_record(db, ASSET_ID, created_at="2026-10-12 08:00:00")
    html = c.get("/assets").text
    assert "2026-10-12 08:00:00" in html
    assert f'src="/assets/{ASSET_ID}/thumb"' in html
    assert 'loading="lazy"' in html
