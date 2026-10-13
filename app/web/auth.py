"""单密码 auth：签名 cookie + 中间件。未设密码 → 完全旁路（无中间件）。

secret = sha256(启动随机盐 + 密码)；进程内稳定、跨进程不共享（可接受：
重启后重新登录）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

COOKIE_NAME = "auth_token"
SESSION_TTL = 30 * 24 * 3600
_SALT = os.urandom(32)

# 未登录也放行的路径。登录页 / 健康探针用**精确**匹配：前缀匹配会连带放行
# 未来的 /login-help、/api/health/detail 之类路由（静默裸奔）。
EXEMPT_EXACT = ("/login", "/static", "/api/health")
# 仅对真正的「前缀形」挂载点做前缀匹配（末尾带 /）。webhook 自带 token 校验。
EXEMPT_PREFIXES = ("/static/", "/api/hooks/")
# 这些前缀下的未登录请求返回 401 JSON，而不是 302 重定向——JSON/SSE 调用方
# 拿到重定向毫无意义。`/config/api/` 是后续任务新增的模型列表端点所在处。
_API_PREFIXES = ("/api/", "/config/api/")

# 登录失败后的固定延迟（秒），抗暴力破解。测试可 monkeypatch 为 0。
LOGIN_DELAY_SECONDS = 0.5

# exp 是十进制时间戳（约 10 位）；上限防止超长数字串喂给 int() 造成 CPU 浪费。
_MAX_EXP_DIGITS = 20


def _secret(password: str) -> bytes:
    return hashlib.sha256(_SALT + password.encode()).digest()


def make_token(password: str, ttl: int = SESSION_TTL) -> str:
    exp = str(int(time.time()) + ttl)
    sig = hmac.new(_secret(password), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_token(token: str, password: str) -> bool:
    payload, _, sig = token.partition(".")
    # isascii()+isdigit()：排除非 ASCII 数字（"²"、阿拉伯-印度数字等 isdigit() 为真
    # 但 int() 会抛错）。长度上限同样为防 int() 处理超长串。
    if (
        not sig
        or not payload.isascii()
        or not payload.isdigit()
        or len(payload) > _MAX_EXP_DIGITS
    ):
        return False
    try:
        exp = int(payload)
    except ValueError:  # 理论上已被上面的守卫挡住，兜底防回归
        return False
    if exp < time.time():
        return False
    expect = hmac.new(_secret(password), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, sig)


def password_matches(supplied: str, password: str) -> bool:
    """比较密码。hmac.compare_digest 对**内容**定长（不随内容提前返回），但比较
    耗时仍随长度变化——密码长度可被计时观测，此处由登录失败的固定延迟
    （LOGIN_DELAY_SECONDS）掩盖。两侧编码为 bytes 以兼容非 ASCII 密码。"""
    return hmac.compare_digest(supplied.encode(), password.encode())


def is_exempt(path: str) -> bool:
    return path in EXEMPT_EXACT or path.startswith(EXEMPT_PREFIXES)


def request_is_secure(request: Request) -> bool:
    """请求是否经 HTTPS 到达——决定登录 cookie 是否带 Secure。

    公网部署带 Secure 防明文回传；本地 HTTP 若误带 Secure，浏览器不回收
    cookie，登录必然失效。故按请求协议自动判断，而非无条件开启。

    反代终止 TLS 时 scheme 仍是 http，需看 X-Forwarded-Proto（只取首个值，
    防逗号拼接欺骗）；uvicorn 的 ProxyHeadersMiddleware 会把 scheme 也改对，
    两条路径都覆盖。
    """
    if request.url.scheme == "https":
        return True
    proto = request.headers.get("x-forwarded-proto", "")
    return proto.split(",")[0].strip().lower() == "https"


def make_middleware(password: str):
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if is_exempt(path):
            return await call_next(request)
        if verify_token(request.cookies.get(COOKIE_NAME, ""), password):
            return await call_next(request)
        if path.startswith(_API_PREFIXES):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

    return auth_middleware
