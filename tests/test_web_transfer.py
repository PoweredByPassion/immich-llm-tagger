"""config_transfer 路由契约：页面渲染、导出下载 headers/内容无凭证、
导入成功 flash、导入失败中文回显。
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store import crud
from app.store.settings import SettingsStore
from app.store.transfer import export_config, render_yaml
from app.web.app import create_app
from app.web.routes import config_transfer


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def client(data_dir: Path):
    app = create_app(
        data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir))
    )
    app.include_router(config_transfer.router)
    with TestClient(app) as c:
        yield c


def _seed(db) -> None:
    crud.insert_tag(db, name="人物", kind="primary")
    crud.insert_tag(db, name="other", kind="primary")
    SettingsStore(db).set("webhook_token", "tok-web-secret")


def test_transfer_page_renders(client):
    html = client.get("/config/transfer").text
    assert "/config/transfer/export" in html
    assert 'action="/config/transfer/import"' in html
    assert "将覆盖现有全部配置" in html  # 二次确认文案


def test_export_download_headers_and_no_secret(client):
    _seed(client.app.state.db)
    resp = client.get("/config/transfer/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/yaml")
    assert resp.headers["content-disposition"].startswith(
        'attachment; filename="autotag-config-'
    )
    assert resp.headers["content-disposition"].endswith('.yaml"')
    body = resp.text
    assert "tags" in body
    assert "tok-web-secret" not in body


def test_import_success_flash(client):
    _seed(client.app.state.db)
    text = render_yaml(export_config(client.app.state.db))
    resp = client.post(
        "/config/transfer/import",
        files={"file": ("cfg.yaml", io.BytesIO(text.encode()), "text/yaml")},
    )
    assert resp.status_code == 200
    html = resp.text
    assert "以下敏感字段已跳过，需手动配置" in html
    assert "Immich API Key" in html
    assert "LLM API Key" in html
    assert "Webhook Token" in html
    assert "激活 Prompt 版本" in html


def test_import_error_chinese_echo(client):
    resp = client.post(
        "/config/transfer/import",
        files={"file": ("bad.yaml", io.BytesIO(b"not: [valid: yaml"), "text/yaml")},
    )
    assert resp.status_code == 200
    assert "YAML 解析失败" in resp.text


def test_import_validation_error_echo(client):
    resp = client.post(
        "/config/transfer/import",
        files={"file": ("bad.yaml", io.BytesIO(b"bogus_table: []"), "text/yaml")},
    )
    assert resp.status_code == 200
    assert "未知配置段" in resp.text
