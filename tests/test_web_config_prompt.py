"""config_prompt 路由契约：模板编辑（保存即新版本）、预览走真实渲染器、token 估算。

预览端点必须调用 app.classify.prompt.render_prompt —— 变异自查：
把预览改成 f-string 拼接会导致渲染内容断言变红。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store.crud import (
    create_prompt_version,
    get_latest_prompt_version,
    list_prompt_versions,
)
from app.web.app import create_app


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def client(data_dir: Path):
    c = TestClient(
        create_app(data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir)))
    )
    with c:
        yield c


TEMPLATE_BODY = "请为照片分类。\n候选标签：\n{{TAG_LIST}}\n文件：{{FILENAME}}"

# 固定 fixture：与真实管线一致的注入块结构（tag_list 是 (名称, 描述) 元组列表）
FIXED_TAGS = [
    ("风景", "自然风光、山川湖泊"),
    ("人像", "以人物为主体的照片"),
]


def test_page_renders_latest_template(client):
    c = client
    create_prompt_version(c.app.state.db, body=TEMPLATE_BODY, note="v1")
    html = c.get("/config/prompt").text
    assert TEMPLATE_BODY in html
    assert "v1" in html
    # 保存即新版本的提示
    assert "新版本" in html


def test_page_empty_when_no_version(client):
    c = client
    html = c.get("/config/prompt").text
    assert "尚未创建" in html
    # 无版本时编辑框预填内置默认模板（与管线 DEFAULT_PROMPT_TEMPLATE 一致）
    # 注意 HTML 转义：断言取模板的首行（不含引号）作为标记
    from app.engine.executor import DEFAULT_PROMPT_TEMPLATE
    assert DEFAULT_PROMPT_TEMPLATE.splitlines()[0] in html
    assert "{{TAG_LIST}}" in html


def test_save_creates_new_version_row(client):
    c = client
    db = c.app.state.db
    create_prompt_version(db, body="旧模板 {{TAG_LIST}}", note="旧")
    resp = c.post("/config/prompt", data={"body": TEMPLATE_BODY, "note": "新说明"})
    assert resp.status_code == 200
    versions = list_prompt_versions(db)
    assert len(versions) == 2
    latest = get_latest_prompt_version(db)
    assert latest is not None
    assert latest.body == TEMPLATE_BODY
    assert latest.note == "新说明"
    assert "已保存" in resp.text


def test_save_rejects_empty_body(client):
    c = client
    db = c.app.state.db
    create_prompt_version(db, body="旧模板 {{TAG_LIST}}", note="旧")
    resp = c.post("/config/prompt", data={"body": "   \n  ", "note": ""})
    assert resp.status_code == 200
    assert "已保存" not in resp.text
    assert len(list_prompt_versions(db)) == 1
    assert get_latest_prompt_version(db).body == "旧模板 {{TAG_LIST}}"


def test_preview_uses_real_renderer(client):
    """预览输出必须等于 render_prompt 的真实结果（变异：改 f-string 拼接会红）。"""
    c = client
    resp = c.post("/config/prompt/preview", data={"body": TEMPLATE_BODY})
    assert resp.status_code == 200
    text = resp.text
    # 注入块内容：标签名与描述都出现，且为 "- name: desc" 行
    assert "- 风景: 自然风光、山川湖泊" in text
    assert "- 人像: 以人物为主体的照片" in text
    # 可选占位符被示例数据替换
    assert "{{FILENAME}}" not in text
    assert "IMG_4032.jpg" in text
    # 占位符本身不再残留
    assert "{{TAG_LIST}}" not in text
    # token 估算展示
    assert "token" in text.lower()
    assert "~" in text


def test_preview_empty_body_hint(client):
    c = client
    resp = c.post("/config/prompt/preview", data={"body": "   "})
    assert resp.status_code == 200
    assert "请输入" in resp.text
    assert "- 风景" not in resp.text


def test_preview_matches_render_prompt_exactly(client):
    from app.classify.prompt import PromptContext, render_prompt

    c = client
    body = "标签：\n{{TAG_LIST}}\n范围：{{SCOPE}}\n日期：{{DATE}}\nEXIF：\n{{EXIF}}"
    resp = c.post("/config/prompt/preview", data={"body": body})
    rendered = render_prompt(body, _sample_context())
    assert rendered in resp.text


def _sample_context() -> PromptContext:
    from app.web.routes.config_prompt import sample_prompt_context

    return sample_prompt_context()


def test_token_estimate_deterministic(client):
    """同一 body 的 token 估算必须稳定，且按字符近似公式计算。"""
    from app.web.routes.config_prompt import estimate_tokens

    assert estimate_tokens("abcd" * 10) == estimate_tokens("abcd" * 10)
    # 40 个 ASCII 字符 → 40/4 = 10
    assert estimate_tokens("a" * 40) == 10
    # 中日韩字符按 ~1.5 字符/token 加权
    assert estimate_tokens("风" * 30) == 45


def test_history_listed_with_details(client):
    c = client
    db = c.app.state.db
    create_prompt_version(db, body="第一版 {{TAG_LIST}}", note="初稿")
    create_prompt_version(db, body="第二版 {{TAG_LIST}}", note="改进")
    html = c.get("/config/prompt").text
    # 最新版在编辑表单
    assert "第二版" in html
    # 历史列表包含两版，details 折叠展示内容
    assert "初稿" in html
    assert "改进" in html
    assert "<details" in html
    assert "第一版" in html


# -------------------------------------------------- M5 Task 6: 激活/沙盒


def _add_tag(db, name, kind="primary"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO tags (name, description, kind, enabled, archive, album) "
            "VALUES (?, '', ?, 1, 0, NULL)", (name, kind))


def test_activate_sets_active_version(client):
    c = client
    db = c.app.state.db
    _add_tag(db, "other")
    v1 = create_prompt_version(db, body=TEMPLATE_BODY, note="v1")
    create_prompt_version(db, body=TEMPLATE_BODY, note="v2")
    resp = c.post(f"/config/prompt/{v1}/activate")
    assert resp.status_code == 200
    from app.store.settings import SettingsStore
    assert SettingsStore(db).get("active_prompt_version_id") == v1
    # 页面上激活行有徽章
    html = c.get("/config/prompt").text
    assert "当前激活" in html


def test_activate_rejects_missing_version(client):
    c = client
    db = c.app.state.db
    _add_tag(db, "other")
    resp = c.post("/config/prompt/9999/activate")
    assert resp.status_code == 200
    assert "不存在" in resp.text
    from app.store.settings import SettingsStore
    assert SettingsStore(db).get("active_prompt_version_id") is None


def test_use_default_clears_active_version(client):
    c = client
    db = c.app.state.db
    _add_tag(db, "other")
    v1 = create_prompt_version(db, body=TEMPLATE_BODY, note="v1")
    c.post(f"/config/prompt/{v1}/activate")
    resp = c.post("/config/prompt/activate-default")
    assert resp.status_code == 200
    from app.store.settings import SettingsStore
    assert SettingsStore(db).get("active_prompt_version_id") is None


def test_page_highlights_latest_when_null(client):
    c = client
    db = c.app.state.db
    create_prompt_version(db, body=TEMPLATE_BODY, note="v1")
    html = c.get("/config/prompt").text
    # null 时"最新版本"行高亮（emerald 徽章）
    assert "最新版本" in html
    assert "当前激活" not in html


def test_page_renders_confirm_and_sandbox_sections(client):
    c = client
    db = c.app.state.db
    create_prompt_version(db, body=TEMPLATE_BODY, note="v1")
    html = c.get("/config/prompt").text
    # 激活切换二次确认（Alpine confirmOpen 模式）
    assert "confirmOpen" in html
    assert "影响后续所有分类" in html
    # 沙盒区
    assert "沙盒" in html
    assert "/config/prompt/sandbox" in html
    assert "随机取一张" in html


def test_sandbox_submit_creates_dryrun_job(client):
    c = client
    db = c.app.state.db
    resp = c.post("/config/prompt/sandbox", data={"asset_id": "asset-x"})
    assert resp.status_code == 200
    assert "沙盒任务已入队" in resp.text
    from app.store.jobs import list_jobs
    jobs_list = list_jobs(db)
    dry = [j for j in jobs_list if j.type == "dryrun"]
    assert len(dry) == 1
    assert dry[0].params == {"asset_id": "asset-x", "source": "manual"}


def test_sandbox_submit_requires_asset_id(client):
    c = client
    resp = c.post("/config/prompt/sandbox", data={"asset_id": ""})
    assert "请填写 assetId" in resp.text


def test_sandbox_submit_carries_editor_draft_body(client):
    """沙盒试跑要带编辑框当前内容（未保存也可试跑）。"""
    c = client
    db = c.app.state.db
    resp = c.post("/config/prompt/sandbox", data={
        "asset_id": "asset-x", "body": "DRAFT\n{{TAG_LIST}}\n"})
    assert resp.status_code == 200
    from app.store.jobs import list_jobs
    dry = [j for j in list_jobs(db) if j.type == "dryrun"]
    assert len(dry) == 1
    assert dry[0].params["prompt_body"] == "DRAFT\n{{TAG_LIST}}"


def test_sandbox_submit_blank_body_not_persisted(client):
    """空白草稿不带 prompt_body，executor 侧走已保存的激活模板。"""
    c = client
    db = c.app.state.db
    c.post("/config/prompt/sandbox", data={"asset_id": "asset-x", "body": "  "})
    from app.store.jobs import list_jobs
    dry = [j for j in list_jobs(db) if j.type == "dryrun"]
    assert "prompt_body" not in dry[0].params


def test_sandbox_random_fills_asset_id(client, monkeypatch):
    c = client
    from app.store.connections import ConnectionStore, ImmichConn
    ConnectionStore(c.app.state.db).set_immich(
        ImmichConn(url="http://immich.example", api_key="k")
    )
    captured = {}

    class _Asset:
        id = "uuid-1"
        library_id = None
        original_path = "/lib/a.jpg"

    class _Dto:
        pass

    class _FakeClient:
        def __init__(self, **kw):
            pass

        def ensure_tags(self, names):
            return {n: f"tag-{n}" for n in names}

        def iter_assets(self, dto):
            captured["dto"] = dto
            yield _Asset()

    import app.web.routes.config_prompt as mod
    monkeypatch.setattr(mod, "ImmichClient", _FakeClient)
    monkeypatch.setattr(mod, "MetadataSearchDto", _Dto)
    monkeypatch.setattr(mod, "AssetTypeEnum", type("_E", (), {"IMAGE": "image"}))

    resp = c.post("/config/prompt/sandbox/random")
    assert resp.status_code == 200
    assert "uuid-1" in resp.text


def test_sandbox_random_no_connection_error(client):
    c = client
    resp = c.post("/config/prompt/sandbox/random")
    assert "尚未配置 Immich 连接" in resp.text
