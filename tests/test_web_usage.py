"""M5 Task 7: 用量统计页（GET /usage）契约。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.web.app import create_app


@pytest.fixture()
def client(tmp_path: Path):
    data_dir = tmp_path / "data"
    c = TestClient(
        create_app(data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir)))
    )
    with c:
        c.app.state.scheduler.stop()
        yield c


def _record(db, *, model=None, usage=None, dry_run=0) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, model, usage, status, "
            "dry_run, source, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"a{usage}", model, json.dumps(usage) if usage else None,
             "ok", dry_run, "manual", "2020-01-01 08:00:00"),
        )


def test_usage_page_empty_state(client: TestClient) -> None:
    r = client.get("/usage")
    assert r.status_code == 200
    assert "暂无用量数据" in r.text


def test_usage_page_renders_totals_and_tables(client: TestClient) -> None:
    db = client.app.state.db
    _record(db, model="gpt-4o-mini",
            usage={"prompt_tokens": 10, "completion_tokens": 5})
    _record(db, model="claude", usage={"prompt_tokens": 100, "completion_tokens": 1})
    r = client.get("/usage")
    assert r.status_code == 200
    # 总计卡（累计 2 次请求、116 tokens）
    assert "116" in r.text
    assert "2" in r.text
    # 按模型表
    assert "gpt-4o-mini" in r.text
    assert "claude" in r.text
    # tabular-nums 数字类
    assert "tabular-nums" in r.text
    # 空 job 维度也渲染（无 job_id 记录 → type/status 空展示），不出现 ---
    assert "---" not in r.text


def test_nav_has_usage_link(client: TestClient) -> None:
    r = client.get("/usage")
    assert 'href="/usage"' in r.text


def test_usage_by_job_expired_job_no_link(client: TestClient) -> None:
    """by_job 行的 job_id 不存在（被 retention 清理）时显示"已过期"，不渲染链接。"""
    db = client.app.state.db
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, job_id, model, usage, status, "
            "dry_run, source, created_at) VALUES ('a1', 42, 'm', ?, 'ok', 0, "
            "'manual', '2020-01-01 08:00:00')",
            (json.dumps({"prompt_tokens": 1, "completion_tokens": 1}),),
        )
    r = client.get("/usage")
    assert r.status_code == 200
    assert "已过期" in r.text
    assert 'href="/jobs/42"' not in r.text


def test_usage_by_job_existing_job_renders_link(client: TestClient) -> None:
    db = client.app.state.db
    with db.connect() as conn:
        conn.execute("INSERT INTO jobs (id, type, status) VALUES (42, 'scan', 'done')")
        conn.execute(
            "INSERT INTO asset_records (asset_id, job_id, model, usage, status, "
            "dry_run, source, created_at) VALUES ('a1', 42, 'm', ?, 'ok', 0, "
            "'manual', '2020-01-01 08:00:00')",
            (json.dumps({"prompt_tokens": 1, "completion_tokens": 1}),),
        )
    r = client.get("/usage")
    assert r.status_code == 200
    assert 'href="/jobs/42"' in r.text
