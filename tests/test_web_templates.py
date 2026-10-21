"""base.html 渲染契约。

断言 token 互斥：saved 场景断言「已保存」且仅在该场景出现；
error 场景断言「出错了」前缀。全部输出做 em-dash 扫描（设计规范硬性要求）。
"""
from __future__ import annotations

import jinja2
import pytest

from app.web.i18n import install_template_globals


@pytest.fixture()
def env() -> jinja2.Environment:
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader("app/web/templates"),
        autoescape=True,
    )
    # 模板全局 tr/lang：否则模板里的 {{ tr(...) }} / {{ lang() }} 会 UndefinedError
    install_template_globals(environment)
    return environment


def render(env: jinja2.Environment, template: str, **ctx) -> str:
    return env.get_template(template).render(**ctx)


def test_base_renders_product_name_and_assets(env):
    html = render(env, "base.html")
    assert "Immich LLM Tagger" in html
    assert '<link rel="stylesheet" href="/static/tailwind.css">' in html
    assert '<script src="/static/htmx.min.js" defer></script>' in html
    assert 'href="/config/immich"' in html
    assert 'href="/config/llm"' in html
    assert 'lang="zh-CN"' in html
    assert '<meta charset="utf-8">' in html


def test_saved_banner_only_when_truthy(env):
    with_banner = render(env, "base.html", saved=True)
    without = render(env, "base.html")
    assert "已保存" in with_banner
    assert "已保存" not in without
    assert 'role="status"' in with_banner
    assert 'role="status"' not in without


def test_error_banner_only_when_truthy(env):
    with_error = render(env, "base.html", error="连接失败")
    without = render(env, "base.html")
    assert "出错了：连接失败" in with_error
    assert "出错了" not in without
    assert 'role="alert"' in with_error
    assert 'role="alert"' not in without


def test_content_block_overridable(env):
    child = (
        "{% extends 'base.html' %}"
        "{% block title %}子页标题{% endblock %}"
        "{% block content %}<p>CHILD_CONTENT_MARKER</p>{% endblock %}"
    )
    html = env.from_string(child).render()
    assert "<p>CHILD_CONTENT_MARKER</p>" in html
    assert "<title>子页标题</title>" in html


MONITOR_LINKS = ["/", "/jobs", "/assets"]
CONFIG_LINKS = [
    "/config/immich",
    "/config/llm",
    "/config/webhook",
    "/config/poll",
    "/config/scope",
    "/config/tags",
    "/config/markers",
    "/config/prompt",
]


def test_nav_monitor_group_links(env):
    html = render(env, "base.html")
    for href in MONITOR_LINKS:
        assert f'href="{href}"' in html


def test_nav_config_group_links(env):
    html = render(env, "base.html")
    for href in CONFIG_LINKS:
        assert f'href="{href}"' in html


def test_nav_group_labels_present(env):
    html = render(env, "base.html")
    assert "监控" in html
    assert "配置" in html


def test_alpine_script_loaded(env):
    html = render(env, "base.html")
    assert '<script src="/static/alpine.min.js" defer></script>' in html


def test_dark_theme_is_only_theme(env):
    """单深色主题契约：html.dark 固定，无浅色切换逻辑残留。"""
    html = render(env, "base.html")
    assert 'class="dark"' in html
    assert "prefers-color-scheme" not in html
    assert "localStorage.getItem(\"theme\")" not in html


def test_confirm_partial_renders(env):
    html = render(env, "_confirm.html", confirm_title="删除标签", confirm_body="该操作不可撤销")
    assert "删除标签" in html
    assert "该操作不可撤销" in html
    assert "取消" in html
    # 破坏性按钮用红色系
    assert "red-500" in html


def test_confirm_partial_defaults_empty(env):
    html = render(env, "_confirm.html")
    assert "取消" in html


def test_no_em_dash_in_output(env):
    """设计规范禁止 em-dash（—），任何渲染路径都不得出现。"""
    for ctx in ({}, {"saved": True}, {"error": "错误信息"}):
        assert "—" not in render(env, "base.html", **ctx)
    assert "—" not in render(env, "_confirm.html", confirm_title="删除标签")
