"""识别设置页（/config/poll）+ Webhook 接入页（/config/webhook）路由契约。

轮询/效应器/重试在 /config/poll（顶级「识别设置」tab）；Webhook 卡片
（URL/Header/token/轮换 + Immich Workflow 直达）拆到 /config/webhook，
仍属「连接配置」组。

变异专项（spec §7 加粗规则）：页面 token 必须走 `SettingsStore.webhook_token()`
（DB 无该行时首次生成并持久化），不能读 `get_all()["webhook_token"]`
（后者兜底 None，会渲染成假空）。测试场景：先 rotate 生成 token，
再断言页面渲染出该 token —— 若实现读 get_all()，DB 有值时两者恰好一致，
因此额外覆盖「DB 有值但实现读 None 兜底」不成立；击杀点在
test_page_shows_token_iff_generated 的反向：DB 无值时 get_all() 返回 None，
webhook_token() 会生成 —— 通过「先 rotate 再 rotate 后旧 token 不再渲染」
以及 401 引导文案场景组合钉住行为。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store.settings import SettingsStore
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
        yield c, SettingsStore(c.app.state.db)


# --------------------------------------------------------------- page render

def test_poll_page_reachable(client):
    """识别设置页（顶级 tab）：轮询开关 + 效应器/重试。webhook 已拆到 /config/webhook。"""
    c, _ = client
    resp = c.get("/config/poll")
    assert resp.status_code == 200
    # 开关旁文案（spec §7）
    assert "关闭轮询即全局暂停（webhook 触发仍可入队）" in resp.text
    # 页头即新的顶级 tab 语义
    assert "识别设置" in resp.text
    # webhook 卡片已搬走：本页不再出现 token header 名
    assert "x-webhook-token" not in resp.text.lower()


def test_webhook_page_reachable(client):
    """Webhook 接入页（仍在「连接配置」组）：URL/Header/token + 触发说明。"""
    c, _ = client
    resp = c.get("/config/webhook")
    assert resp.status_code == 200
    # header name 与 hooks.py 实际校验一致
    assert "x-webhook-token" in resp.text.lower()
    # 触发时机说明随 webhook 卡片搬到本页
    assert "AssetTagged" in resp.text


def test_webhook_page_links_to_immich_workflow(client):
    """T6：配好 Immich 后在 webhook 页给出 {url}/workflow 的新标签直达按钮。"""
    from app.store.connections import ConnectionStore, ImmichConn

    c, _ = client
    ConnectionStore(c.app.state.db).set_immich(
        ImmichConn(url="http://immich.example:2283/", api_key="k")
    )
    html = c.get("/config/webhook").text
    assert 'href="http://immich.example:2283/workflow"' in html
    assert 'target="_blank"' in html


def test_webhook_page_no_dead_link_without_immich(client):
    """未配 Immich 时不给链接（避免死链），只给提示。"""
    c, _ = client
    html = c.get("/config/webhook").text
    assert "/workflow" not in html
    assert "请先在「Immich 服务端连接」里填写服务器地址" in html


def test_page_shows_token_iff_generated(client):
    """DB 无 token：首次渲染前尚未生成 → 页面不显示 token 值（显示未生成引导）。

    注意：页面本身渲染时调 webhook_token() 会触发首次生成（与 hooks 401
    首次生成语义一致），因此「未生成」只可能是 DB 中无行且页面渲染前的
    状态。这里钉住的是渲染后 token 非空且回显。"""
    c, settings = client
    settings.set("webhook_token", None)  # 显式清掉（DEFAULTS 兜底为 None）
    token = settings.webhook_token()  # 首次生成并持久化
    html = c.get("/config/webhook").text
    assert token in html


def test_page_token_mutation_kill(client):
    """变异击杀：DB 无 webhook_token 行时 get_all()["webhook_token"] == None，
    webhook_token() 语义是「首次访问生成并持久化」。直接 GET 页面：
    正确实现渲染出生成的 token；错误实现（get_all）渲染出字面 "None"。"""
    c, settings = client
    settings.rotate_webhook_token()  # 先制造"曾经生成过"的前提
    with settings._db.connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = 'webhook_token'")
    assert settings.get_all()["webhook_token"] is None
    html = c.get("/config/webhook").text
    # 页面渲染调 webhook_token() → 生成并持久化，页面上必须就是它
    stored = settings.get("webhook_token")
    assert stored is not None and stored != "None"
    assert stored in html


def test_rotate_generates_new_token(client):
    c, settings = client
    old = settings.webhook_token()
    resp = c.post("/config/webhook/rotate")
    assert resp.status_code == 200
    assert "旧 token 已立即失效，请同步更新 Immich workflow 配置" in resp.text
    new_token = settings.get("webhook_token")
    assert new_token != old
    assert new_token in resp.text


def test_rotate_page_updated_not_old(client):
    c, settings = client
    old = settings.webhook_token()
    html = c.post("/config/webhook/rotate").text
    new = settings.get("webhook_token")
    assert new in html
    assert old != new


# ----------------------------------------------------------------- save form

def test_save_valid_interval_and_switch(client):
    c, settings = client
    resp = c.post("/config/poll", data={
        "poll_enabled": "1", "poll_interval_seconds": "60",
        "queue_tag_name": "未分类"})
    assert resp.status_code == 200
    assert settings.get("poll_enabled") is True
    assert settings.get("poll_interval_seconds") == 60
    assert settings.get("queue_tag_name") == "未分类"


def test_save_switch_off(client):
    c, settings = client
    resp = c.post("/config/poll", data={
        "poll_interval_seconds": "30", "queue_tag_name": "未分类"})
    assert resp.status_code == 200
    assert settings.get("poll_enabled") is False


def test_save_queue_tag_name_empty_rejected(client):
    c, settings = client
    before = settings.get("queue_tag_name")
    resp = c.post("/config/poll", data={
        "poll_interval_seconds": "60", "queue_tag_name": "  "})
    assert resp.status_code == 200
    assert "队列标签名不能为空" in resp.text
    assert settings.get("queue_tag_name") == before


def test_save_interval_below_minimum_rejected(client):
    c, settings = client
    before = (settings.get("poll_enabled"), settings.get("poll_interval_seconds"))
    resp = c.post("/config/poll", data={"poll_enabled": "1", "poll_interval_seconds": "5"})
    assert resp.status_code == 200
    assert "轮询间隔" in resp.text
    assert (settings.get("poll_enabled"), settings.get("poll_interval_seconds")) == before


def test_save_interval_not_integer_rejected(client):
    c, settings = client
    before = settings.get("poll_interval_seconds")
    resp = c.post("/config/poll", data={"poll_interval_seconds": "abc"})
    assert resp.status_code == 200
    assert "轮询间隔" in resp.text
    assert settings.get("poll_interval_seconds") == before


# ------------------------------------------------------------ 401 first-visit copy

def test_unauthorized_webhook_first_visit_hint(client):
    """hooks.py 401：token 由 webhook_token() 首次生成 —— 请求到达 hooks 时
    token 一定已存在，401 只可能因 header 不匹配。页面文案与真实校验对齐：
    header 名 x-webhook-token + 当前 token。"""
    c, settings = client
    token = settings.webhook_token()
    r401 = c.post("/api/hooks/immich", content=b"{}", headers={"x-webhook-token": "wrong"})
    assert r401.status_code == 401
    r200 = c.post("/api/hooks/immich", content=b"{}", headers={"x-webhook-token": token})
    assert r200.status_code == 200
    page = c.get("/config/webhook").text
    assert token in page
    assert "x-webhook-token" in page.lower()


# ------------------------------------------------------------ §8.1 效应器控件

def test_page_renders_effector_controls(client):
    """poll 页并入 §8.1 三控件（不新开配置页）；默认全开 3 个 checkbox 都勾选。

    只有全局效应器：评分/描述按标签控制（tags.write_rating /
    write_description），页面改为指向「标签体系」的说明文字。
    """
    import re
    c, settings = client
    html = c.get("/config/poll").text
    for eff in ("tag", "visibility", "album"):
        assert re.search(
            r'name="enabled_effects" value="%s"[^>]*checked' % eff, html, re.S
        ), eff
    # 下放的 rating/description 不能再出现在全局复选组里（关掉也不生效）
    assert 'name="enabled_effects" value="rating"' not in html
    assert 'name="enabled_effects" value="description"' not in html
    assert "标签体系" in html      # 指向逐标签开关的说明
    assert 'name="skip_marker_tag_name"' in html
    assert 'name="apply_max_attempts"' in html
    # 未显式配置时跳过标记标签名渲染为空串（None 不能渲染成字面 "None"）
    assert 'value="None"' not in html


def test_save_effector_settings(client):
    c, settings = client
    resp = c.post("/config/poll", data={
        "poll_enabled": "1", "poll_interval_seconds": "60",
        "queue_tag_name": "未分类",
        "enabled_effects": ["tag", "rating"],
        "skip_marker_tag_name": "我改过的名字",
        "apply_max_attempts": "7",
    })
    assert resp.status_code == 200
    assert settings.get("enabled_effects") == ["tag", "rating"]
    assert settings.get("skip_marker_tag_name") == "我改过的名字"
    assert settings.get("apply_max_attempts") == 7


def test_save_effectors_all_unchecked_clears(client):
    """复选组全部取消勾选 → 浏览器不发送该键 → 必须清空（不能"缺键即保持"）。"""
    c, settings = client
    resp = c.post("/config/poll", data={
        "poll_interval_seconds": "60", "queue_tag_name": "未分类"})
    assert resp.status_code == 200
    assert settings.get("enabled_effects") == []


def test_save_blank_skip_marker_resets_to_language_default(client):
    """留空 = 未显式配置（None）→ resolve_skip_marker_tag_name 跟随语言。"""
    c, settings = client
    settings.set("skip_marker_tag_name", "旧名字")
    resp = c.post("/config/poll", data={
        "poll_interval_seconds": "60", "queue_tag_name": "未分类",
        "skip_marker_tag_name": "   ", "apply_max_attempts": "3"})
    assert resp.status_code == 200
    assert settings.get("skip_marker_tag_name") is None


def test_save_invalid_max_attempts_rejected_without_partial_write(client):
    c, settings = client
    before = (settings.get("apply_max_attempts"), settings.get("queue_tag_name"))
    resp = c.post("/config/poll", data={
        "poll_interval_seconds": "60", "queue_tag_name": "改过的队列名",
        "apply_max_attempts": "0"})
    assert resp.status_code == 200
    assert "不小于 1 的整数" in resp.text
    # 校验失败不留半套写入
    assert (settings.get("apply_max_attempts"), settings.get("queue_tag_name")) == before


def test_save_non_integer_max_attempts_rejected(client):
    c, settings = client
    before = settings.get("apply_max_attempts")
    resp = c.post("/config/poll", data={
        "poll_interval_seconds": "60", "queue_tag_name": "未分类",
        "apply_max_attempts": "abc"})
    assert resp.status_code == 200
    assert "不小于 1 的整数" in resp.text
    assert settings.get("apply_max_attempts") == before


def test_effector_rows_render_one_per_line_with_description(client):
    """每个全局效应器独占一行，并带本地化说明（zh）。"""
    c, _ = client
    html = c.get("/config/poll").text
    for name, desc in (
        ("tag", "把预测出的主标签与 Marker 标签写回 Immich。"),
        ("visibility", "把判定为低价值或敏感的图片归档"),
        ("album", "把图片加入其标签所配置的相册"),
    ):
        assert f'value="{name}"' in html
        assert desc in html


def test_effector_descriptions_localized_english(client):
    """效应器说明走 i18n，en 下不残留中文。"""
    c, _ = client
    html = c.get("/config/poll", cookies={"lang": "en"}).text
    assert "Write the predicted primary tag and markers back to Immich." in html
    assert "把预测出的主标签" not in html
