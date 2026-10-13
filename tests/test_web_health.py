"""create_app 装配契约与 /api/health 行为。

注意：TestClient 未显式进入时 lifespan 不会执行，所以每个用例都
用 `with TestClient(create_app(...))`，离开作用域即触发 shutdown。
`db_writable` 正常路径与故障路径的断言 token 互斥（True vs False），
避免「两边都断言了同一个东西」导致的假绿。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.db.connection import Database
from app.web.app import create_app


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _client(data_dir: Path) -> TestClient:
    return TestClient(create_app(data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir))))


def test_health_full_payload(data_dir: Path):
    with _client(data_dir) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    # 整体断言：三个键齐全、值正确，而不是逐字段抽查
    assert resp.json() == {"status": "ok", "schema_version": 10, "db_writable": True}


def test_migrations_ran_and_db_file_exists(data_dir: Path):
    with _client(data_dir):
        pass
    db_file = data_dir / "autotag.db"
    assert db_file.is_file(), "启动后 DB 文件必须已落盘"
    conn = sqlite3.connect(db_file)
    try:
        (v,) = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    finally:
        conn.close()
    assert v == 10


def test_health_reports_false_when_db_unusable(data_dir: Path, monkeypatch):
    def boom(self):
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover

    with _client(data_dir) as client:
        # 启动后再破坏 DB —— 只考验 health 端点，不影响 lifespan
        monkeypatch.setattr(Database, "connect", boom)
        resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "schema_version": 0, "db_writable": False}


def test_root_renders_overview(data_dir: Path):
    # M4 起 / 是概览页（routes/overview.py），不再 307 到配置页。
    with _client(data_dir) as client:
        resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200
    assert "概览" in resp.text or "队列" in resp.text


def test_data_dir_override_selects_db_file(tmp_path: Path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    with _client(dir_a), _client(dir_b):
        pass
    assert (dir_a / "autotag.db").is_file()
    assert (dir_b / "autotag.db").is_file()
    # 两个库是独立的文件，而不是同一个
    assert not (dir_a / "autotag.db").samefile(dir_b / "autotag.db")


def test_state_db_and_bootstrap_populated(data_dir: Path):
    cfg = BootstrapConfig(data_dir=str(data_dir))
    app = create_app(data_dir=data_dir, bootstrap=cfg)
    with TestClient(app):
        assert isinstance(app.state.db, Database)
        assert app.state.db.path == data_dir / "autotag.db"
        assert app.state.bootstrap == cfg
        assert app.state.bootstrap.data_dir == str(data_dir)


def test_config_router_prefix_contract():
    # 占位 router 无路由，prefix 在 HTTP 层不可观测，直接断言契约
    from app.web.routes import config_conn

    assert config_conn.router.prefix == "/config"


def test_seed_from_env_ran_on_startup(tmp_path: Path):
    # 断言 seed 的实际效果：env 里的 immich/llm 配置必须落到 DB
    data_dir = tmp_path / "seeded"
    cfg = BootstrapConfig(
        immich_url="http://immich.example",
        immich_api_key="k1",
        llm_base_url="http://llm.example",
        llm_api_key="k2",
        llm_model="m1",
        data_dir=str(data_dir),
    )
    app = create_app(data_dir=data_dir, bootstrap=cfg)
    with TestClient(app):
        from app.store.connections import ConnectionStore

        store = ConnectionStore(app.state.db)
        assert store.get_immich().url == "http://immich.example"
        assert store.get_immich().api_key == "k1"
        assert store.get_llm().base_url == "http://llm.example"
        assert store.get_llm().model == "m1"


def test_static_mounted_when_dir_exists(tmp_path: Path, monkeypatch):
    import app.web.app as app_module

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    monkeypatch.setattr(app_module, "_STATIC_DIR", static_dir)
    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir)
    routes = {getattr(r, "path", None) for r in app.routes}
    assert "/static" in routes


def test_static_not_mounted_when_dir_missing(tmp_path: Path, monkeypatch):
    import app.web.app as app_module

    monkeypatch.setattr(
        app_module, "_STATIC_DIR", tmp_path / "definitely-missing-static"
    )
    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir)
    routes = {getattr(r, "path", None) for r in app.routes}
    assert "/static" not in routes


def test_bootstrap_defaults_from_env(data_dir: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", "/data")  # 显式 data_dir 参数必须覆盖它
    app = create_app(data_dir=data_dir)
    with TestClient(app):
        assert app.state.bootstrap.data_dir == str(data_dir)
        assert app.state.bootstrap.db_path == data_dir / "autotag.db"
