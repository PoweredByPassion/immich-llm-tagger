"""Task 5: 单密码访问认证（AUTH_PASSWORD）。

未设密码 → 完全旁路（无中间件），既有部署零摩擦；设了密码 → 签名 cookie
会话 + 中间件放行白名单，其余一律 302 /login（API 前缀返回 401 JSON）。

装配模式沿用 tests/test_web_health.py：`with TestClient(create_app(...))`
（TestClient 未显式进入时 lifespan 不执行）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import BootstrapConfig
from app.web import auth
from app.web.app import create_app


def _client(data_dir: Path, password: str | None = None) -> TestClient:
    cfg = BootstrapConfig(data_dir=str(data_dir), auth_password=password)
    return TestClient(create_app(data_dir=data_dir, bootstrap=cfg))


# --------------------------------------------------------------- 无密码：旁路


def test_no_password_open_access(tmp_path: Path):
    data_dir = tmp_path / "data"
    with _client(data_dir, password=None) as client:
        for url in ("/", "/jobs/", "/usage", "/config/immich", "/danger"):
            resp = client.get(url, follow_redirects=False)
            assert resp.status_code == 200, url
        # 未启用认证时 POST /login 也无意义地放行（不种 cookie）
        resp = client.post("/login", data={"password": "whatever"},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert "auth_token" not in resp.headers.get("set-cookie", "")


# --------------------------------------------------------------- 有密码：拦截


def test_with_password_redirects(tmp_path: Path):
    data_dir = tmp_path / "data"
    with _client(data_dir, password="secret") as client:
        resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_login_success_sets_cookie(tmp_path: Path):
    data_dir = tmp_path / "data"
    with _client(data_dir, password="secret") as client:
        resp = client.post("/login", data={"password": "secret"},
                           follow_redirects=False)
        assert resp.status_code == 303
        set_cookie = resp.headers["set-cookie"]
        assert "auth_token=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Max-Age=" in set_cookie
        assert "Path=/" in set_cookie
        # 取 token 显式回带，验证会话生效
        token = set_cookie.split(";", 1)[0].split("=", 1)[1]
        after = client.get("/", cookies={"auth_token": token},
                           follow_redirects=False)
        assert after.status_code == 200


def test_login_wrong_password(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(auth, "LOGIN_DELAY_SECONDS", 0)
    with _client(data_dir, password="secret") as client:
        resp = client.post("/login", data={"password": "nope"},
                           follow_redirects=False)
        assert resp.status_code == 200
        assert "密码错误" in resp.text  # 默认 zh
        # 失败不种 cookie
        assert "auth_token" not in resp.headers.get("set-cookie", "")
        # 错误文案走 i18n：en cookie → 英文
        resp_en = client.post("/login", data={"password": "nope"},
                              cookies={"lang": "en"}, follow_redirects=False)
        assert "Wrong password" in resp_en.text


def test_exempt_paths(tmp_path: Path):
    data_dir = tmp_path / "data"
    with _client(data_dir, password="secret") as client:
        # 静态资源
        assert client.get("/static/tailwind.css").status_code == 200
        # 健康探针
        assert client.get("/api/health").status_code == 200
        # 登录页本身
        assert client.get("/login", follow_redirects=False).status_code == 200
        # webhook：无 auth cookie 但带正确 webhook token → 200（证明未被中间件拦截）
        from app.store.settings import SettingsStore

        token = SettingsStore(client.app.state.db).webhook_token()
        resp = client.post(
            "/api/hooks/immich",
            json={"data": {"asset": {"id": "a1", "type": "IMAGE"}}},
            headers={"x-webhook-token": token},
        )
        assert resp.status_code == 200


def test_exempt_prefix_is_not_overbroad(tmp_path: Path):
    """前缀放行不得波及同前缀的其它路径（否则未来新增路由会静默裸奔）。"""
    data_dir = tmp_path / "data"
    with _client(data_dir, password="secret") as client:
        # /loginXYZ 不能因 /login 前缀被放行
        r = client.get("/loginXYZ", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"
        # /staticXYZ 不能因 /static 前缀被放行
        r = client.get("/staticXYZ", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"
        # /api/healthz 不能因 /api/health 前缀被放行；命中 API 前缀 → 401
        r = client.get("/api/healthz", follow_redirects=False)
        assert r.status_code == 401


def test_api_returns_401_not_redirect(tmp_path: Path):
    data_dir = tmp_path / "data"
    with _client(data_dir, password="secret") as client:
        resp = client.get("/api/sse/overview", follow_redirects=False)
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}
        # /config/api/ 前缀同样返回 401（而非路由层的 404）
        resp2 = client.get("/config/api/models", follow_redirects=False)
        assert resp2.status_code == 401


# --------------------------------------------------------------- token 校验


def test_tampered_token_rejected(tmp_path: Path):
    data_dir = tmp_path / "data"
    password = "secret"
    good = auth.make_token(password)
    tampered = good[:-1] + ("a" if good[-1] != "a" else "b")
    assert auth.verify_token(good, password) is True
    assert auth.verify_token(tampered, password) is False

    with _client(data_dir, password=password) as client:
        resp = client.get("/", cookies={"auth_token": tampered},
                          follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_expired_token_rejected():
    password = "secret"
    expired = auth.make_token(password, ttl=-10)
    assert auth.verify_token(expired, password) is False


def test_malformed_token_rejected_not_500():
    """畸形 cookie 必须被安全拒绝（返回 False），不得抛 ValueError 变 500。"""
    password = "secret"
    bad_tokens = [
        "9" * 5000 + ".abc",  # 超长数字串：int() 可行但被长度上限拦下
        "².abc",              # 上标数字：isdigit() 为真，int() 抛 ValueError
        "١٢٣.abc",            # 阿拉伯-印度数字：isdigit() 为真，int() 抛错
        "abc.def",            # 非数字 payload
        ".",                  # 空 payload + 空签名
        "",                   # 空串
        "garbage",            # 无分隔符
    ]
    for tok in bad_tokens:
        assert auth.verify_token(tok, password) is False, tok


def test_malformed_cookie_does_not_500(tmp_path: Path):
    """端到端：未认证客户端用畸形 cookie 不能触发 500。

    只发 ASCII 可传输的畸形值——非 ASCII（"²"/阿拉伯-印度数字）本就不是合法
    cookie 值，客户端侧就发不出去，那类由 test_malformed_token_rejected_not_500
    在 verify_token 层覆盖。
    """
    data_dir = tmp_path / "data"
    with _client(data_dir, password="secret") as client:
        for tok in ("9" * 5000 + ".abc", "abc.def", "garbage", "."):
            resp = client.get("/", cookies={"auth_token": tok},
                              follow_redirects=False)
            assert resp.status_code == 302, tok
            assert resp.headers["location"] == "/login"


def test_password_matches_non_ascii():
    assert auth.password_matches("密码", "密码") is True
    assert auth.password_matches("密码", "密码x") is False
    assert auth.password_matches("", "secret") is False


def test_auth_password_from_env(monkeypatch):
    monkeypatch.setenv("AUTH_PASSWORD", "hunter2")
    assert BootstrapConfig.from_env().auth_password == "hunter2"
    # 空串等价于未设置 → None（旁路）
    monkeypatch.setenv("AUTH_PASSWORD", "")
    assert BootstrapConfig.from_env().auth_password is None
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    assert BootstrapConfig.from_env().auth_password is None


# ---------------------------------------------------- cookie Secure 自动判断


def _client_with_base(tmp_path: Path, base_url: str) -> TestClient:
    cfg = BootstrapConfig(data_dir=str(tmp_path / "data"), auth_password="secret")
    return TestClient(create_app(data_dir=tmp_path / "data", bootstrap=cfg),
                      base_url=base_url)


def test_cookie_not_secure_over_http(tmp_path: Path):
    """本地 HTTP：cookie 不得带 Secure，否则浏览器不回收、登录失效。"""
    with _client_with_base(tmp_path, "http://testserver") as client:
        resp = client.post("/login", data={"password": "secret"},
                           follow_redirects=False)
    assert "Secure" not in resp.headers["set-cookie"]


def test_cookie_secure_over_https(tmp_path: Path):
    """HTTPS 直连：cookie 带 Secure。"""
    with _client_with_base(tmp_path, "https://testserver") as client:
        resp = client.post("/login", data={"password": "secret"},
                           follow_redirects=False)
    assert "Secure" in resp.headers["set-cookie"]


def test_cookie_secure_behind_forwarded_proto(tmp_path: Path):
    """反代终止 TLS：scheme 仍是 http，靠 X-Forwarded-Proto 判定 Secure。"""
    with _client_with_base(tmp_path, "http://testserver") as client:
        resp = client.post("/login", data={"password": "secret"},
                           headers={"x-forwarded-proto": "https"},
                           follow_redirects=False)
    assert "Secure" in resp.headers["set-cookie"]


def test_cookie_secure_forwarded_proto_not_spoofed_by_suffix(tmp_path: Path):
    """逗号拼接不得骗过判定：只认首个值。"""
    with _client_with_base(tmp_path, "http://testserver") as client:
        resp = client.post("/login", data={"password": "secret"},
                           headers={"x-forwarded-proto": "http, https"},
                           follow_redirects=False)
    assert "Secure" not in resp.headers["set-cookie"]
