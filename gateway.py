"""Single public entrypoint that authenticates users and proxies to per-user instances."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from src.multi_instance import InstanceManager, UserRegistry, configure_gateway_log_relay, utc_now


BASE_DIR = Path(__file__).resolve().parent
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "127.0.0.1").strip() or "127.0.0.1"
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8000") or "8000")
IDLE_TIMEOUT_SECONDS = int(os.getenv("INSTANCE_IDLE_TIMEOUT_SECONDS", "1800") or "1800")
SESSION_TTL_SECONDS = int(os.getenv("GATEWAY_SESSION_TTL_SECONDS", str(60 * 60 * 24 * 30)) or str(60 * 60 * 24 * 30))
SESSION_SECRET = os.getenv("GATEWAY_SESSION_SECRET", "dev-session-secret-change-me")
SESSION_COOKIE = "chat_gateway_session"
_JOB_POLL_PATH = re.compile(r"^/api/chat/generate/jobs/[0-9a-f]{32}$", re.IGNORECASE)

registry = UserRegistry(BASE_DIR, gateway_port=GATEWAY_PORT)
instance_manager = InstanceManager(BASE_DIR, registry, host="127.0.0.1", idle_timeout_seconds=IDLE_TIMEOUT_SECONDS)


class AuthRequest(BaseModel):
    username: str
    password: str


class _JobPollingAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            method = str(record.args[1])
            path = str(record.args[2])
        except (IndexError, KeyError, TypeError):
            return True
        return not (method == "GET" and _JOB_POLL_PATH.fullmatch(path))


def _configure_access_log_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if any(isinstance(log_filter, _JobPollingAccessLogFilter) for log_filter in access_logger.filters):
        return
    access_logger.addFilter(_JobPollingAccessLogFilter())


def _login_page(message: str = "") -> str:
    notice = f"<p style='color:#8b0000;margin:0 0 12px'>{message}</p>" if message else ""
    return f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Persona Chat Gateway</title>
  <style>
    body {{ font-family: "Segoe UI", sans-serif; background: linear-gradient(135deg, #f6efe7, #d9e7f3); min-height: 100vh; margin: 0; display: grid; place-items: center; }}
    .card {{ width: min(92vw, 420px); background: rgba(255,255,255,.92); border-radius: 20px; padding: 28px; box-shadow: 0 20px 60px rgba(42,61,84,.16); }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    p {{ color: #556; line-height: 1.5; }}
    form {{ display: grid; gap: 12px; margin-top: 18px; }}
    input {{ border: 1px solid #cdd5df; border-radius: 12px; padding: 12px 14px; font-size: 14px; }}
    button {{ border: 0; border-radius: 12px; padding: 12px 14px; font-size: 14px; cursor: pointer; }}
    .primary {{ background: #183a5a; color: #fff; }}
    .secondary {{ background: #eef3f8; color: #183a5a; }}
    .actions {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    .note {{ margin-top: 14px; font-size: 12px; color: #667; }}
  </style>
</head>
<body>
  <main class="card">
    <h1>Persona Chat</h1>
    <p>8000 端口作为统一入口。登录后会自动进入你的独立实例。</p>
    {notice}
    <form id="authForm">
      <input name="username" placeholder="用户名" autocomplete="username" required>
      <input name="password" type="password" placeholder="密码" autocomplete="current-password" required>
      <div class="actions">
        <button class="primary" type="button" id="loginButton">登录</button>
        <button class="secondary" type="button" id="registerButton">注册</button>
      </div>
    </form>
    <div class="note">首次注册的用户会自动继承当前项目的旧 data 数据。</div>
  </main>
  <script>
    async function submit(path) {{
      const form = document.getElementById('authForm');
      const data = Object.fromEntries(new FormData(form).entries());
      const response = await fetch(path, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(data),
      }});
      if (!response.ok) {{
        let detail = `请求失败 (${{response.status}})`;
        try {{ detail = (await response.json()).detail || detail; }} catch (_) {{}}
        alert(detail);
        return;
      }}
      window.location.href = '/';
    }}
    document.getElementById('loginButton').addEventListener('click', () => submit('/gateway/login'));
    document.getElementById('registerButton').addEventListener('click', () => submit('/gateway/register'));
  </script>
</body>
</html>
"""


def _gateway_admin_page() -> str:
        return """
<!doctype html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Persona Chat Gateway Admin</title>
    <style>
        body { font-family: "Segoe UI", sans-serif; background: linear-gradient(180deg, #f8efe5, #e5f0f8); margin: 0; color: #2a3340; }
        main { max-width: 1080px; margin: 32px auto; padding: 0 16px 24px; }
        .top { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
        h1 { margin: 0; font-size: 30px; }
        p { margin: 6px 0 0; color: #526070; }
        .actions { display: flex; gap: 10px; flex-wrap: wrap; }
        button, a { border: 0; border-radius: 10px; padding: 10px 14px; text-decoration: none; font-size: 14px; cursor: pointer; }
        .primary { background: #153754; color: #fff; }
        .ghost { background: rgba(255,255,255,0.9); color: #234; border: 1px solid #cfdae6; }
        .card { background: rgba(255,255,255,0.95); border: 1px solid #d8e2ed; border-radius: 16px; padding: 16px; box-shadow: 0 10px 24px rgba(47,70,97,.08); }
        table { width: 100%; border-collapse: collapse; margin-top: 6px; font-size: 14px; }
        th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #e6edf4; }
        th { font-size: 12px; color: #607081; letter-spacing: .02em; text-transform: uppercase; }
        .tag { display: inline-block; border-radius: 999px; padding: 4px 10px; font-size: 12px; font-weight: 600; }
        .tag.up { background: #e7f5ec; color: #18603b; }
        .tag.down { background: #f8e6e6; color: #7a2323; }
        .muted { color: #708091; font-size: 12px; }
        @media (max-width: 760px) {
            table, thead, tbody, tr, th, td { display: block; }
            thead { display: none; }
            tr { border-bottom: 1px solid #e6edf4; padding: 6px 0; }
            td { border: 0; padding: 6px 0; }
        }
    </style>
</head>
<body>
    <main>
        <section class="top">
            <div>
                <h1>实例管理页</h1>
                <p>显示已注册用户、固定端口、实例运行状态和最后活跃时间。</p>
            </div>
            <div class="actions">
                <a class="ghost" href="/">返回聊天</a>
                <button class="ghost" id="logoutButton" type="button">退出登录</button>
                <button class="primary" id="refreshButton" type="button">刷新状态</button>
            </div>
        </section>
        <section class="card">
            <div id="summary" class="muted">加载中...</div>
            <table>
                <thead>
                    <tr>
                        <th>用户</th>
                        <th>端口</th>
                        <th>实例状态</th>
                        <th>最后活跃</th>
                        <th>数据目录</th>
                        <th>操作</th>
                    </tr>
                </thead>
                <tbody id="rows"></tbody>
            </table>
        </section>
    </main>
    <script>
        function formatTime(value) {
            if (!value) return '-';
            try {
                return new Date(value).toLocaleString('zh-CN');
            } catch (_) {
                return String(value);
            }
        }

        async function loadInstances() {
            const response = await fetch('/gateway/instances');
            if (response.status === 401) {
                window.location.href = '/';
                return;
            }
            if (!response.ok) {
                document.getElementById('rows').innerHTML = '<tr><td colspan="6">读取失败</td></tr>';
                return;
            }
            const data = await response.json();
            const rows = document.getElementById('rows');
            const items = Array.isArray(data.instances) ? data.instances : [];
            document.getElementById('summary').textContent = `当前登录用户：${data.current_user} · 注册用户总数：${items.length}`;
            if (!items.length) {
                rows.innerHTML = '<tr><td colspan="6">暂无用户</td></tr>';
                return;
            }
            rows.innerHTML = items.map((item) => {
                const running = item.running ? '<span class="tag up">运行中</span>' : '<span class="tag down">未运行</span>';
                const action = item.username === data.current_user
                    ? '<button class="ghost restartButton" type="button">重启实例</button>'
                    : '-';
                return `<tr><td>${item.username || '-'}</td><td>${item.port || '-'}</td><td>${running}</td><td>${formatTime(item.last_used_at)}</td><td>${item.data_dir || '-'}</td><td>${action}</td></tr>`;
            }).join('');
            rows.querySelectorAll('.restartButton').forEach((button) => button.addEventListener('click', restartCurrentInstance));
        }

        async function restartCurrentInstance(event) {
            const button = event.currentTarget;
            button.disabled = true;
            button.textContent = '重启中...';
            try {
                const response = await fetch('/gateway/instances/restart', { method: 'POST' });
                if (!response.ok) {
                    let detail = `重启失败 (${response.status})`;
                    try { detail = (await response.json()).detail || detail; } catch (_) {}
                    throw new Error(detail);
                }
                clearTransientChatCache();
                await loadInstances();
            } catch (error) {
                alert(error.message || '重启失败');
                button.disabled = false;
                button.textContent = '重启实例';
            }
        }

        function clearTransientChatCache() {
            const cacheKeys = [
                'persona-chat:draft:v1',
                'persona-chat:active-job:v1',
                'persona-chat:input:v1',
            ];
            try {
                cacheKeys.forEach((key) => localStorage.removeItem(key));
                const resetKey = 'persona-chat:instance-restarted:v1';
                localStorage.setItem(resetKey, String(Date.now()));
                localStorage.removeItem(resetKey);
            } catch (_) {}
        }

        async function logout() {
            await fetch('/gateway/logout', { method: 'POST' });
            window.location.href = '/';
        }

        document.getElementById('refreshButton').addEventListener('click', loadInstances);
        document.getElementById('logoutButton').addEventListener('click', logout);
        loadInstances();
    </script>
</body>
</html>
"""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _session_signature(payload_part: str) -> str:
    digest = hmac.new(SESSION_SECRET.encode("utf-8"), payload_part.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def _encode_session(username: str) -> str:
    payload = {
        "username": username,
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(8),
    }
    payload_part = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
    signature = _session_signature(payload_part)
    return f"{payload_part}.{signature}"


def _decode_session(token: str) -> dict[str, Any] | None:
    try:
        payload_part, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = _session_signature(payload_part)
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def _current_username(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    payload = _decode_session(token)
    if payload is None:
        return None
    username = str(payload.get("username", "")).strip()
    if not username:
        return None
    if registry.get_user(username) is None:
        return None
    return username


def _set_session_cookie(response: Response, username: str) -> None:
    token = _encode_session(username)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS)


def _clear_session_cookie(response: Response, request: Request) -> None:
    del request
    response.delete_cookie(SESSION_COOKIE)


def _proxy(request: Request, username: str, full_path: str) -> Response:
    record = instance_manager.ensure_running(username)
    registry.touch_user(username)
    query = request.url.query
    target_path = "/" + full_path
    if query:
        target_path += f"?{query}"
    connection = http.client.HTTPConnection("127.0.0.1", record.port, timeout=180)
    body = request.scope.get("_body")
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection", "cookie"}
    }
    connection.request(request.method, target_path, body=body, headers=headers)
    upstream = connection.getresponse()
    payload = upstream.read()
    response_headers = {
        key: value
        for key, value in upstream.getheaders()
        if key.lower() not in {"connection", "content-length", "transfer-encoding", "date", "server"}
    }
    return Response(content=payload, status_code=upstream.status, headers=response_headers)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_gateway_log_relay()
    _configure_access_log_filter()
    stop_event = threading.Event()

    def _janitor() -> None:
        while not stop_event.wait(60):
            instance_manager.cleanup_idle_instances()

    worker = threading.Thread(target=_janitor, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop_event.set()
        instance_manager.stop_all()


app = FastAPI(title="Persona Chat Gateway", version="1.0.0", lifespan=lifespan)


@app.get("/gateway/health")
def gateway_health() -> dict[str, Any]:
    return {"ok": True, "users": len(registry.list_users())}


@app.get("/gateway/instances")
def gateway_instances(request: Request) -> dict[str, Any]:
    username = _current_username(request)
    if not username:
        raise HTTPException(401, "请先登录")
    return {"current_user": username, "instances": instance_manager.list_runtime()}


@app.post("/gateway/instances/restart")
def gateway_restart_current_instance(request: Request) -> dict[str, Any]:
    username = _current_username(request)
    if not username:
        raise HTTPException(401, "请先登录")
    try:
        record = instance_manager.restart_instance(username)
    except (KeyError, RuntimeError, TimeoutError) as exc:
        raise HTTPException(503, f"实例重启失败：{exc}") from exc
    return {"ok": True, "username": record.username, "port": record.port}


@app.get("/gateway/admin", include_in_schema=False)
def gateway_admin_page(request: Request) -> Response:
    username = _current_username(request)
    if not username:
        return RedirectResponse(url="/", status_code=303)
    return HTMLResponse(_gateway_admin_page())


@app.post("/gateway/register")
def gateway_register(payload: AuthRequest) -> JSONResponse:
    try:
        record = registry.create_user(payload.username, payload.password)
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    instance_manager.ensure_running(record.username)
    response = JSONResponse({"username": record.username, "port": record.port})
    _set_session_cookie(response, record.username)
    return response


@app.post("/gateway/login")
def gateway_login(payload: AuthRequest) -> JSONResponse:
    try:
        record = registry.verify_user(payload.username, payload.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if record is None:
        raise HTTPException(401, "用户名或密码错误")
    instance_manager.ensure_running(record.username)
    response = JSONResponse({"username": record.username, "port": record.port})
    _set_session_cookie(response, record.username)
    return response


@app.post("/gateway/logout")
def gateway_logout(request: Request) -> JSONResponse:
    response = JSONResponse({"ok": True})
    _clear_session_cookie(response, request)
    return response


@app.get("/", include_in_schema=False)
async def gateway_root(request: Request) -> Response:
    username = _current_username(request)
    if not username:
        return HTMLResponse(_login_page())
    request.scope["_body"] = b""
    return _proxy(request, username, "")


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
async def gateway_catch_all(full_path: str, request: Request) -> Response:
    if full_path.startswith("gateway/"):
        raise HTTPException(404, "接口不存在")
    if full_path == "favicon.ico":
        return Response(status_code=204)
    username = _current_username(request)
    if not username:
        return RedirectResponse(url="/", status_code=303)
    request.scope["_body"] = await request.body()
    return _proxy(request, username, full_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("gateway:app", host=GATEWAY_HOST, port=GATEWAY_PORT, reload=False)