"""i18n 基础设施契约：双语字典、cookie 解析、模板全局 tr/lang。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store import jobs as jobs_store
from app.store.settings import SettingsStore
from app.web.app import create_app


class FakeRequest:
    """最小请求桩：仅暴露 cookies 属性供 pass_context 全局读取。"""

    def __init__(self, cookies: dict | None = None):
        self.cookies = cookies or {}


@pytest.fixture()
def client(tmp_path: Path):
    data_dir = tmp_path / "data"
    c = TestClient(
        create_app(data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir)))
    )
    with c:
        # 与 test_web_overview.py 同款：停调度线程，避免后台 scan job 干扰
        c.app.state.scheduler.stop()
        SettingsStore(c.app.state.db).set("poll_enabled", False)
        with c.app.state.db.connect() as conn:
            conn.execute("DELETE FROM jobs")
        yield c


def test_t_zh_default():
    from app.web.i18n import t
    assert t("nav.overview", "zh") == "概览"


def test_t_en():
    from app.web.i18n import t
    assert t("nav.overview", "en") == "Overview"


def test_t_missing_key_falls_back_to_key():
    from app.web.i18n import t
    assert t("no.such.key", "en") == "no.such.key"


def test_lang_from_cookie():
    from app.web.i18n import lang_from_cookie
    assert lang_from_cookie({"lang": "en"}) == "en"
    assert lang_from_cookie({}) == "zh"
    assert lang_from_cookie({"lang": "fr"}) == "zh"


def test_pass_context_tr_reads_request_cookie():
    """模板全局 tr 从 request cookie 解析语言（渲染冒烟）。"""
    from jinja2 import Environment
    from app.web.i18n import install_template_globals
    env = Environment()
    install_template_globals(env)
    out = env.from_string('{{ tr("nav.overview") }}').render(
        request=FakeRequest(cookies={"lang": "en"}))
    assert out == "Overview"


def test_lang_cookie_switches_html_lang(client):
    """端到端：真路由渲染时 request 确实进了模板 context（防 legacy 签名回归）。"""
    assert '<html lang="en"' in client.get("/", cookies={"lang": "en"}).text
    assert '<html lang="zh-CN"' in client.get("/", cookies={"lang": "zh"}).text


def test_every_entry_has_both_languages():
    """key-first 结构的守门测试：防 300 条词条静默漏翻。"""
    from app.web import i18n
    for key, entry in i18n._DICTIONARY.items():
        assert entry.get("zh"), key
        assert entry.get("en"), key


def _referenced_keys() -> set[str]:
    """扫描模板与路由里字面量调用的 i18n key（tr("k") / t("k", ...)）。

    只认字面量首参：字典/列表里以变量形式调用的 key（如 _TYPE_BADGE_KEYS
    的 t(v, lang)）不在此列——那些由渲染它们的路由测试兜底。
    """
    import re
    root = Path(__file__).resolve().parent.parent / "app" / "web"
    pattern = re.compile(r"""(?<![\w.])(?:tr|t)\(\s*["']([^"']+)["']""")
    keys: set[str] = set()
    files = (
        list((root / "templates").rglob("*.html"))
        + list((root / "routes").glob("*.py"))
        + [root / "app.py"]  # 装配层也有 t("...") 调用（如登录错误文案）
    )
    for path in files:
        keys.update(pattern.findall(path.read_text(encoding="utf-8")))
    return keys


def test_every_referenced_key_exists():
    """模板/路由引用的每个 key 都必须在字典里（防拼写错误渲染成裸 key）。"""
    from app.web import i18n
    missing = sorted(k for k in _referenced_keys() if k not in i18n._DICTIONARY)
    assert not missing, f"模板/路由引用了字典中不存在的 i18n key: {missing}"


def _store_problem_keys() -> set[str]:
    """扫描 app/store/*.py 里字面量构造的 Problem("key")。"""
    root = Path(__file__).resolve().parent.parent / "app" / "store"
    pattern = re.compile(r"""Problem\(\s*["']([^"']+)["']""")
    keys: set[str] = set()
    for path in root.glob("*.py"):
        keys.update(pattern.findall(path.read_text(encoding="utf-8")))
    return keys


def test_every_store_problem_key_exists():
    """store 层抛出的每个 Problem key 都必须在字典里（web 层据此渲染）。"""
    from app.web import i18n
    missing = sorted(k for k in _store_problem_keys() if k not in i18n._DICTIONARY)
    assert not missing, f"store 引用了字典中不存在的 i18n key: {missing}"


def test_store_problem_renders_both_languages():
    from app.store.errors import Problem
    from app.web.i18n import render_problems
    problems = [Problem("store.tag.exists", ("风景",)),
                Problem("store.exclusion.value_required", ("path",))]
    assert render_problems(problems, "zh") == "标签「风景」已存在；排除类型 path 的值不能为空"
    assert render_problems(problems, "en") == (
        'Tag "风景" already exists; Value for exclusion type path cannot be empty'
    )


def test_validation_error_localized_on_post(client):
    """POST 校验错误路径也走 i18n（en 下不得残留中文）。"""
    c = client
    resp = c.post("/config/exclusions", data={"type": "tag", "value": "nope"},
                  cookies={"lang": "en"})
    # Jinja 自动转义会把引号变成 &#34;，只断言不含引号的片段
    assert "nope" in resp.text and "does not exist" in resp.text
    assert "不存在" not in resp.text
    resp_zh = c.post("/config/exclusions", data={"type": "tag", "value": "nope"},
                     cookies={"lang": "zh"})
    assert "标签「nope」不存在" in resp_zh.text


# ------------------------------------------------------- 英文渲染守门（防漏翻/拼接回归）

# 用户可见文本里的 CJK：全角标点 + 中日韩表意文字 + 全角字母数字。
_CJK = re.compile("[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")

# 页面路由用不触发重定向的规范 URL：per-request cookie 在 307/302 重定向中会丢，
# 直接命中会被误判成 zh（见 test_web_jobs / test_web_overview 同款注释）。
_PAGE_ROUTES = [
    "/", "/jobs/", "/usage", "/assets/", "/assets/does-not-exist",
    "/config/immich", "/config/llm", "/config/webhook", "/config/poll",
    "/config/scope", "/config/tags", "/config/markers",
    "/config/prompt", "/config/transfer", "/danger",
]


def _visible_text(html: str) -> str:
    """只留用户可见文本：剥掉 HTML 注释、<script>/<style> 与标签。

    模板/脚本里的中文注释是代码不是 UI 文案；语言切换按钮的「中文」是刻意保留的
    目标语言 endonym，单独剔除。
    """
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", "", html).replace("中文", "")


def test_english_render_has_no_cjk(client):
    """每个页面在 lang=en 下渲染出的可见文本都不得残留中文（防漏翻）。"""
    c = client
    jid = jobs_store.create_job(c.app.state.db, "single", params={"asset_id": "x"})
    for url in [*_PAGE_ROUTES, f"/jobs/{jid}"]:
        html = c.get(url, cookies={"lang": "en"}).text
        leftover = "".join(_CJK.findall(_visible_text(html)))
        assert not leftover, f"{url} 英文渲染残留中文: {leftover!r}"


def test_english_render_has_no_broken_artifacts(client):
    """钉死 prefix/suffix 拼接回归：缺空格、重复词、CJK 标点泄漏。"""
    c = client
    transfer = _visible_text(c.get("/config/transfer", cookies={"lang": "en"}).text)
    assert "willoverwrite" not in transfer
    assert "will overwrite all existing configuration" in transfer

    sandbox = _visible_text(c.post(
        "/config/prompt/sandbox", data={"asset_id": "x"},
        cookies={"lang": "en"}).text)
    assert "page page." not in sandbox
    assert "Assets page." in sandbox

    for url in ("/config/prompt", "/config/transfer", "/config/scope"):
        html = c.get(url, cookies={"lang": "en"}).text
        assert " ；" not in html
        assert "、" not in html


def test_dictionary_percent_placeholders_match():
    """zh/en 的 %s 占位符数量必须一致（防翻译时漏参/多参）。"""
    from app.web import i18n
    for key, entry in i18n._DICTIONARY.items():
        assert entry["zh"].count("%s") == entry["en"].count("%s"), key


def test_dictionary_has_no_bare_percent():
    """字典值里除 %s 外不得出现裸 %（防意外触发 % 格式化）。"""
    from app.web import i18n
    for key, entry in i18n._DICTIONARY.items():
        for lang in ("zh", "en"):
            assert "%" not in entry[lang].replace("%s", ""), (key, lang)


def test_retry_pending_has_two_placeholders():
    """assets.retry_pending 是「待重试 (尝试次数/上限)」，需要两个 %s；
    zh/en 都必须有两个（模板用 % (attempts, cap) 渲染，少一个会 TypeError）。"""
    from app.web import i18n
    entry = i18n._DICTIONARY["assets.retry_pending"]
    assert entry["zh"].count("%s") == 2
    assert entry["en"].count("%s") == 2


def test_task16_new_keys_present():
    """Task 16 新增的 6 条词条（apply 重试 + effector 配置）都在字典里。"""
    from app.web import i18n
    for key in (
        "assets.retry_pending", "assets.apply_gave_up",
        "config.effects.title", "config.effects.enabled",
        "config.effects.skip_marker", "config.effects.max_attempts",
    ):
        assert key in i18n._DICTIONARY, key
