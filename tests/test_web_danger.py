"""danger_ui 路由契约：页面预览数字、RESET 确认词、活动 job 禁止、
成功 303 跳任务详情。"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store.settings import SettingsStore
from app.web.app import create_app
from app.web.routes import danger_ui


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def client(data_dir: Path):
    app = create_app(
        data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir))
    )
    with TestClient(app) as c:
        # 阻止后台调度线程入队 scan job，污染"任一活动 job"判定
        # （同 test_web_overview.py 的既定模式：立即停调度线程 + 清 jobs）
        c.app.state.scheduler.stop()
        SettingsStore(c.app.state.db).set("poll_enabled", False)
        with c.app.state.db.connect() as conn:
            conn.execute("DELETE FROM jobs")
        yield c, c.app.state.db


class _FakeClient:
    def __init__(self, tag_names=("风景",)):
        self.tag_names = list(tag_names)

    def list_tags(self):
        from types import SimpleNamespace
        return [SimpleNamespace(name=n, id=f"id-{n}") for n in self.tag_names]


@pytest.fixture()
def mock_immich(monkeypatch):
    monkeypatch.setattr(
        "app.classify.reset_ops._load_tag_defs", lambda db: [])
    monkeypatch.setattr(
        danger_ui, "ImmichClient", lambda **kw: _FakeClient())
    return _FakeClient


def test_danger_page_renders_preview(client, mock_immich):
    c, db = client
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (1, 'a1', 'archive', 'apply', '{}')")
        conn.execute(
            "INSERT INTO tags (name, description, kind, enabled, archive, album) "
            "VALUES ('风景', '', 'primary', 1, 0, NULL)")
    html = c.get("/danger").text
    assert "危险区" in html
    assert "1" in html and "1" in html  # unarchive 1 张 / 1 个标签（存在性）
    assert "标签仅从本系统管理过的图片上摘除" in html
    assert "/config/transfer" in html
    assert 'name="confirm"' in html


def test_danger_reset_wrong_confirm_word_422(client, mock_immich):
    c, _ = client
    resp = c.post("/danger/reset", data={"confirm": "reset"}, follow_redirects=False)
    assert resp.status_code == 422
    assert "RESET" in resp.text


def test_danger_reset_correct_creates_job(client, mock_immich):
    c, db = client
    resp = c.post("/danger/reset", data={"confirm": "RESET"}, follow_redirects=False)
    assert resp.status_code == 303
    from app.store import jobs as jobs_store
    job = [j for j in jobs_store.list_jobs(db) if j.type == "reset"][0]
    assert resp.headers["location"] == f"/jobs/{job.id}"


def test_danger_reset_blocked_when_active_job(client, mock_immich):
    c, db = client
    from app.store import jobs as jobs_store
    jobs_store.create_job(db, "scan", {})
    resp = c.post("/danger/reset", data={"confirm": "RESET"}, follow_redirects=False)
    assert resp.status_code == 422
    assert "有任务进行中" in resp.text
    from app.store import jobs as jobs_store2
    assert not [j for j in jobs_store2.list_jobs(db) if j.type == "reset"]


def test_danger_page_shows_active_job_hint(client, mock_immich):
    c, db = client
    from app.store import jobs as jobs_store
    jobs_store.create_job(db, "scan", {})
    html = c.get("/danger").text
    assert "有任务进行中" in html
    assert "disabled" in html
