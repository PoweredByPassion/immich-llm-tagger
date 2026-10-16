"""config_conn 路由契约：路径可达、api_key write-only、测试连接、校验。

断言 token 互斥：secret（immich）与 secret-llm（llm）各只在其场景被存入 DB，
所有响应体都断言真实密钥字符串本身从未出现。掩码场景用
`value="********"` 整体断言，而不是子串抽查。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.store.connections import ConnectionStore, ImmichConn, LLMConn
from app.web.app import create_app
from app.web.routes import config_conn as config_conn_module

SECRET_IMMICH = "immich-secret-key-DO-NOT-LEAK"
SECRET_LLM = "llm-secret-key-DO-NOT-LEAK"


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def client(data_dir: Path):
    c = TestClient(
        create_app(data_dir=data_dir, bootstrap=BootstrapConfig(data_dir=str(data_dir)))
    )
    with c:
        yield c, ConnectionStore(c.app.state.db)


# ------------------------------------------------------------- route paths

def test_all_routes_reachable(client):
    c, _ = client
    for path in ("/config/immich", "/config/llm"):
        resp = c.get(path)
        assert resp.status_code == 200, path
        assert 'action="/config' in resp.text
    for path in ("/config/immich/test", "/config/llm/test"):
        resp = c.post(path)
        assert resp.status_code == 200, path


# ------------------------------------------------------- immich form & key

def test_immich_form_empty_when_no_key(client):
    c, _ = client
    html = c.get("/config/immich").text
    # 未存 key：掩码不出现（整体断言，非子串抽查）
    assert 'value="********"' not in html
    assert SECRET_IMMICH not in html


def test_immich_key_masked_and_not_leaked(client):
    c, store = client
    store.set_immich(ImmichConn(url="http://immich.example", api_key=SECRET_IMMICH))
    html = c.get("/config/immich").text
    assert 'name="api_key" value="********"' in html
    assert SECRET_IMMICH not in html


def test_immich_post_mask_preserves_key(client):
    c, store = client
    store.set_immich(ImmichConn(url="http://immich.example", api_key=SECRET_IMMICH))
    resp = c.post(
        "/config/immich",
        data={"url": "http://immich2.example", "api_key": "********", "verify_ssl": "1"},
    )
    assert resp.status_code == 200
    assert "已保存" in resp.text
    assert store.get_immich().api_key == SECRET_IMMICH
    assert store.get_immich().url == "http://immich2.example"
    assert SECRET_IMMICH not in resp.text


def test_immich_post_empty_key_preserves_key(client):
    c, store = client
    store.set_immich(ImmichConn(url="http://immich.example", api_key=SECRET_IMMICH))
    resp = c.post("/config/immich", data={"url": "http://immich2.example", "api_key": ""})
    assert resp.status_code == 200
    assert store.get_immich().api_key == SECRET_IMMICH


def test_immich_post_new_key_replaces(client):
    c, store = client
    store.set_immich(ImmichConn(url="http://immich.example", api_key="old-immich-key"))
    resp = c.post(
        "/config/immich",
        data={"url": "http://immich2.example", "api_key": "new-immich-key"},
    )
    assert resp.status_code == 200
    assert store.get_immich().api_key == "new-immich-key"
    assert "old-immich-key" not in resp.text


def test_immich_round_trip(client):
    c, store = client
    c.post(
        "/config/immich",
        data={"url": "http://roundtrip.example", "api_key": SECRET_IMMICH, "verify_ssl": "1"},
    )
    assert store.get_immich() == ImmichConn(
        url="http://roundtrip.example", api_key=SECRET_IMMICH, verify_ssl=True
    )
    html = c.get("/config/immich").text
    assert 'value="http://roundtrip.example"' in html
    assert "checked" in html
    assert SECRET_IMMICH not in html


def test_immich_verify_ssl_unchecked_round_trip(client):
    c, store = client
    c.post("/config/immich", data={"url": "http://x.example", "api_key": "k"})
    assert store.get_immich().verify_ssl is False


# --------------------------------------------------------------- immich test

def _immich_stats():
    class Stats:
        images = 12
        videos = 3
    return Stats()


def test_immich_test_success_shows_counts(client, monkeypatch):
    c, store = client
    store.set_immich(ImmichConn(url="http://immich.example", api_key=SECRET_IMMICH))

    class FakeImmichClient:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "http://immich.example"
            assert kwargs["api_key"] == SECRET_IMMICH

        def statistics(self):
            return _immich_stats()

        def list_tags(self):
            return [object(), object()]

    monkeypatch.setattr("app.web.routes.config_conn.ImmichClient", FakeImmichClient)
    resp = c.post("/config/immich/test")
    assert resp.status_code == 200
    assert 'data-test-ok="1"' in resp.text
    assert "12 张图片" in resp.text and "3 个视频" in resp.text and "2 个标签" in resp.text
    assert 'data-test-ok="0"' not in resp.text


def test_immich_test_not_configured(client):
    c, _ = client
    resp = c.post("/config/immich/test")
    assert resp.status_code == 200
    assert 'data-test-ok="0"' in resp.text
    assert "尚未配置" in resp.text
    assert 'data-test-ok="1"' not in resp.text


def test_immich_test_failure_returns_200(client, monkeypatch):
    c, store = client
    store.set_immich(ImmichConn(url="http://immich.example", api_key=SECRET_IMMICH))

    from app.clients.immich import ImmichError

    class FakeImmichClient:
        def __init__(self, **kwargs):
            pass

        def statistics(self):
            raise ImmichError("connection refused")

        def list_tags(self):
            raise AssertionError("statistics 失败后不应继续调用")

    monkeypatch.setattr("app.web.routes.config_conn.ImmichClient", FakeImmichClient)
    resp = c.post("/config/immich/test")
    assert resp.status_code == 200
    assert 'data-test-ok="0"' in resp.text
    assert "connection refused" in resp.text
    assert 'data-test-ok="1"' not in resp.text
    assert SECRET_IMMICH not in resp.text


# ---------------------------------------------------------- llm form & key

def test_llm_form_empty_when_no_key(client):
    c, _ = client
    html = c.get("/config/llm").text
    assert 'value="********"' not in html
    assert SECRET_LLM not in html


def test_llm_key_masked_and_not_leaked(client):
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM, model="m"))
    html = c.get("/config/llm").text
    assert 'name="api_key" value="********"' in html
    assert SECRET_LLM not in html


def test_llm_post_mask_preserves_key(client):
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM, model="m"))
    resp = c.post(
        "/config/llm",
        data={
            "base_url": "http://llm2.example", "api_key": "********",
            "model": "m2", "timeout": "60", "max_tokens": "500",
        },
    )
    assert resp.status_code == 200
    assert "已保存" in resp.text
    stored = store.get_llm()
    assert stored.api_key == SECRET_LLM
    assert stored.base_url == "http://llm2.example"
    assert stored.model == "m2"
    assert SECRET_LLM not in resp.text


def test_llm_post_empty_key_preserves_key(client):
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM, model="m"))
    c.post(
        "/config/llm",
        data={"base_url": "http://llm2.example", "api_key": "", "model": "m2",
              "timeout": "60", "max_tokens": "500"},
    )
    assert store.get_llm().api_key == SECRET_LLM


def test_llm_post_new_key_replaces(client):
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key="old-llm-key", model="m"))
    c.post(
        "/config/llm",
        data={"base_url": "http://llm2.example", "api_key": "new-llm-key",
              "model": "m2", "timeout": "30", "max_tokens": "100"},
    )
    assert store.get_llm().api_key == "new-llm-key"


def test_llm_round_trip(client):
    c, store = client
    c.post(
        "/config/llm",
        data={"base_url": "http://rt.example", "api_key": SECRET_LLM,
              "model": "qwen-vl-plus", "timeout": "90",
              "max_tokens": "256", "multimodal": "1"},
    )
    assert store.get_llm() == LLMConn(
        base_url="http://rt.example", api_key=SECRET_LLM, model="qwen-vl-plus",
        timeout=90, max_tokens=256, multimodal=True,
    )
    html = c.get("/config/llm").text
    assert 'value="http://rt.example"' in html
    assert 'value="qwen-vl-plus"' in html
    assert SECRET_LLM not in html


def test_llm_validation_rejects_below_one(client):
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key="old-llm-key",
                          model="m", timeout=50, max_tokens=400))
    resp = c.post(
        "/config/llm",
        data={"base_url": "http://llm2.example", "api_key": "new-llm-key",
              "model": "m2", "timeout": "0", "max_tokens": "300"},
    )
    assert resp.status_code == 200
    assert "出错了" in resp.text
    assert "已保存" not in resp.text
    # 未持久化：旧的 base_url / api_key 保持不变
    stored = store.get_llm()
    assert stored.base_url == "http://llm.example"
    assert stored.api_key == "old-llm-key"
    assert stored.timeout == 50


# ---------------------------------------------------------------- llm test

def test_llm_test_success_shows_models(client, monkeypatch):
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM, model="m"))

    class FakeLLMClient:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "http://llm.example"
            assert kwargs["api_key"] == SECRET_LLM

        def list_models(self):
            return ["qwen-vl-plus", "gpt-4o-mini"]

    monkeypatch.setattr("app.web.routes.config_conn.LLMClient", FakeLLMClient)
    resp = c.post("/config/llm/test")
    assert resp.status_code == 200
    assert 'data-test-ok="1"' in resp.text
    assert "qwen-vl-plus" in resp.text and "gpt-4o-mini" in resp.text
    assert 'data-test-ok="0"' not in resp.text


def test_llm_test_not_configured(client):
    c, _ = client
    resp = c.post("/config/llm/test")
    assert resp.status_code == 200
    assert 'data-test-ok="0"' in resp.text
    assert "尚未配置" in resp.text


def test_llm_test_failure_returns_200(client, monkeypatch):
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM, model="m"))

    from app.clients.llm import LLMError

    class FakeLLMClient:
        def __init__(self, **kwargs):
            pass

        def list_models(self):
            raise LLMError("LLM 返回 401: bad key")

    monkeypatch.setattr("app.web.routes.config_conn.LLMClient", FakeLLMClient)
    resp = c.post("/config/llm/test")
    assert resp.status_code == 200
    assert 'data-test-ok="0"' in resp.text
    assert "401" in resp.text
    assert SECRET_LLM not in resp.text


# ------------------------------------------------------- llm models fetch

def _fake_llm(models=None, exc=None, seen=None):
    """构造 list_models() 返回固定列表或抛异常的 LLMClient 替身；
    记录构造 kwargs 到 seen（供断言 api_key/base_url/timeout）。"""
    class FakeLLMClient:
        def __init__(self, **kwargs):
            if seen is not None:
                seen.update(kwargs)

        def list_models(self):
            if exc is not None:
                raise exc
            return models or []
    return FakeLLMClient


def test_llm_models_success(client, monkeypatch):
    """POST /config/api/llm/models: masked api_key resolved from DB;
    returns the model list as JSON."""
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM, model="m"))
    seen: dict = {}
    monkeypatch.setattr("app.web.routes.config_conn.LLMClient",
                        _fake_llm(models=["m1", "m2"], seen=seen))
    resp = c.post("/config/api/llm/models",
                  data={"base_url": "http://x", "api_key": "********"})
    assert resp.status_code == 200
    assert resp.json() == {"models": ["m1", "m2"], "error": None}
    # 掩码回读库真值，且 base_url 用表单值
    assert seen["api_key"] == SECRET_LLM
    assert seen["base_url"] == "http://x"
    assert SECRET_LLM not in resp.text
    # 硬性要求：固定 5s 超时，绝不继承连接配置的 180s
    assert seen["timeout"] == config_conn_module._MODELS_TIMEOUT


def test_llm_models_failure_silent(client, monkeypatch):
    """Fetch failure → 200 + empty list + non-empty error (silent degrade)."""
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM, model="m"))

    from app.clients.llm import LLMError

    monkeypatch.setattr("app.web.routes.config_conn.LLMClient",
                        _fake_llm(exc=LLMError("LLM 返回 401: bad key")))
    resp = c.post("/config/api/llm/models",
                  data={"base_url": "http://x", "api_key": "sk-test"})
    assert resp.status_code == 200
    assert resp.json()["models"] == []
    assert resp.json()["error"]
    assert SECRET_LLM not in resp.text


def test_llm_models_plain_key_used_as_is(client, monkeypatch):
    """非掩码 api_key 原样透传（走请求体，不进 URL），不回读库真值。"""
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM, model="m"))
    seen: dict = {}
    monkeypatch.setattr("app.web.routes.config_conn.LLMClient",
                        _fake_llm(models=[], seen=seen))
    resp = c.post("/config/api/llm/models",
                  data={"base_url": "http://x", "api_key": "sk-plain"})
    assert resp.status_code == 200
    assert seen["api_key"] == "sk-plain"
    assert "sk-plain" not in resp.request.url.query.decode()


def test_llm_models_empty_base_url_falls_back_to_stored(client, monkeypatch):
    """base_url 空串 → 回退到库中已存的 base_url（未保存也能试的表单值优先反之）。"""
    c, store = client
    store.set_llm(LLMConn(base_url="http://stored.example", api_key=SECRET_LLM, model="m"))
    seen: dict = {}
    monkeypatch.setattr("app.web.routes.config_conn.LLMClient",
                        _fake_llm(models=["a"], seen=seen))
    resp = c.post("/config/api/llm/models",
                  data={"base_url": "", "api_key": "********"})
    assert resp.status_code == 200
    assert seen["base_url"] == "http://stored.example"
    assert seen["api_key"] == SECRET_LLM


def test_llm_models_not_configured_when_base_url_empty(client, monkeypatch):
    """表单与库中 base_url 均空 → 早退（不构造客户端），返回未配置错误。"""
    c, _ = client
    seen: dict = {}
    monkeypatch.setattr("app.web.routes.config_conn.LLMClient",
                        _fake_llm(models=["a"], seen=seen))
    resp = c.post("/config/api/llm/models", data={"base_url": "", "api_key": ""})
    assert resp.status_code == 200
    assert resp.json()["models"] == []
    assert resp.json()["error"]
    assert seen == {}


def test_llm_form_has_fetch_models_controls(client):
    """模型输入配真下拉（非 datalist）+ 获取按钮 + 静默提示位；i18n key 已解析。

    回归：曾用 <datalist>，浏览器按输入框已有内容做前缀过滤，已填 model 时
    下拉只剩一条（表现为"必须清空才能选别的"）。改为独立 <select
    id="model-picker"> 后才列全部模型。
    """
    c, _ = client
    html = c.get("/config/llm").text
    assert '<select id="model-picker"' in html
    assert '<datalist id="model-options">' not in html
    assert 'list="model-options"' not in html
    assert 'onclick="fetchModels(this)"' in html
    assert 'id="model-fetch-hint"' in html
    assert "获取模型列表" in html  # 默认 zh 文案，非裸 key
    assert "config.llm.fetch_models" not in html
    assert "config.llm.model_picker" not in html


# ---------------------------------------------------------- multimodal 勾选

def test_llm_post_without_checkbox_persists_multimodal_false(client):
    """未勾选（表单无 multimodal 字段）→ 存 False；默认勾选由模板保证。"""
    c, store = client
    c.post("/config/llm", data={"base_url": "http://x.example", "api_key": "k",
                                "model": "m"})
    assert store.get_llm().multimodal is False


def test_llm_form_checkbox_reflects_stored_value(client):
    """llm 页唯一的 checkbox 就是 multimodal，可用 checked 有无整体断言。"""
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key="k", model="m",
                          multimodal=False))
    html = c.get("/config/llm").text
    assert 'name="multimodal"' in html
    assert "checked" not in html
    store.set_llm(LLMConn(base_url="http://llm.example", api_key="k", model="m",
                          multimodal=True))
    assert "checked" in c.get("/config/llm").text


# ------------------------------------------------------- llm test-recognition

def _recognition_client(reply: str, sent: list):
    """构造一个 chat() 返回固定文案、并记录 messages 的 LLMClient 替身。"""
    class FakeLLMClient:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "http://llm.example"

        def chat(self, messages, retries=4):
            sent.append(messages)
            return SimpleNamespace(content=reply, model="m", usage=None)
    return FakeLLMClient


def test_recognition_success_shows_reply_and_sends_image(client, monkeypatch):
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM, model="m"))
    sent: list = []
    monkeypatch.setattr("app.web.routes.config_conn.LLMClient",
                        _recognition_client("图里是 Immich 的标识", sent))
    resp = c.post("/config/llm/test-recognition")
    assert resp.status_code == 200
    assert 'data-test-ok="1"' in resp.text
    assert "图里是 Immich 的标识" in resp.text
    assert SECRET_LLM not in resp.text
    content = sent[0][0]["content"]
    image_item = next(i for i in content if i["type"] == "image_url")
    assert image_item["image_url"]["url"].startswith("data:image/png;base64,")


def test_recognition_failure_returns_200(client, monkeypatch):
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM, model="m"))

    from app.clients.llm import LLMError

    class FakeLLMClient:
        def __init__(self, **kwargs):
            pass

        def chat(self, messages, retries=4):
            raise LLMError("LLM 返回 401: bad key")

    monkeypatch.setattr("app.web.routes.config_conn.LLMClient", FakeLLMClient)
    resp = c.post("/config/llm/test-recognition")
    assert resp.status_code == 200
    assert 'data-test-ok="0"' in resp.text
    assert "401" in resp.text
    assert SECRET_LLM not in resp.text


def test_recognition_text_only_when_multimodal_off(client, monkeypatch):
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM,
                          model="m", multimodal=False))
    sent: list = []
    monkeypatch.setattr("app.web.routes.config_conn.LLMClient",
                        _recognition_client("纯文本回答", sent))
    resp = c.post("/config/llm/test-recognition")
    assert resp.status_code == 200
    assert 'data-test-ok="1"' in resp.text
    assert "未勾选多模态，仅文本通路" in resp.text
    assert sent[0] == [{"role": "user", "content": "请描述这张图片的内容"}]


def test_recognition_not_configured(client):
    c, _ = client
    resp = c.post("/config/llm/test-recognition")
    assert resp.status_code == 200
    assert 'data-test-ok="0"' in resp.text
    assert "尚未配置" in resp.text


def test_llm_form_shows_thumb_settings(client):
    """LLM 配置页渲染缩略图尺寸/质量（settings 默认 large/95）。"""
    c, _ = client
    resp = c.get("/config/llm")
    assert resp.status_code == 200
    assert 'value="large"' in resp.text
    assert 'name="thumb_quality"' in resp.text


def test_llm_save_persists_thumb_settings(client):
    c, store = client
    from app.store.settings import SettingsStore

    resp = c.post("/config/llm", data={
        "base_url": "http://llm.example", "model": "m",
        "thumb_preset": "medium", "thumb_quality": "70",
    })
    assert resp.status_code == 200
    s = SettingsStore(store._db).get_all()
    assert s["thumb_preset"] == "medium"
    assert s["thumb_quality"] == 70


def test_llm_save_rejects_bad_thumb_preset(client):
    c, store = client
    from app.store.settings import SettingsStore

    resp = c.post("/config/llm", data={
        "base_url": "http://llm.example", "model": "m",
        "thumb_preset": "huge", "thumb_quality": "85",
    })
    assert resp.status_code == 200
    assert "缩略图尺寸" in resp.text  # 错误横幅
    s = SettingsStore(store._db).get_all()
    assert s["thumb_preset"] == "large"  # 未被破坏


def test_recognition_success_includes_usage(client, monkeypatch):
    """识别成功文案附带 usage token 数，供判断模型是否消费了图片。"""
    c, store = client
    store.set_llm(LLMConn(base_url="http://llm.example", api_key=SECRET_LLM, model="m"))

    class FakeLLMClient:
        def __init__(self, **kwargs):
            pass

        def chat(self, messages, retries=4):
            return SimpleNamespace(
                content="是 Immich logo", model="m",
                usage={"prompt_tokens": 1234, "completion_tokens": 20},
            )

    monkeypatch.setattr("app.web.routes.config_conn.LLMClient", FakeLLMClient)
    resp = c.post("/config/llm/test-recognition")
    assert resp.status_code == 200
    assert "prompt 1234" in resp.text
    assert "completion 20" in resp.text
