"""iwp skill - OAuth 2.1 PKCE 客户端.

完整实现方案 §2.5 / 阶段三-4 §5.2:
  - PKCE 生成(secrets.token_urlsafe(64) + sha256 → base64url)
  - URL 构造:GET /authorize?client_id&redirect_uri&response_type=code&
    code_challenge&code_challenge_method=S256&state&resource
  - 本地回调 server(loopback 双栈 127.0.0.1 + [::1]:9999,后台进程;单脚本 < 30s 阻塞)
  - Token 交换:POST /token code+verifier+resource → tokens
  - 自动续期:401 → refresh_token grant
  - 用户交互集成:呈现方式由 SKILL.md / references/_shared/user-interaction.md 指导

关键工程约束:
  - 单命令阻塞等待 5min 会撞 shell 超时(120/300s)
    → run_local_callback_server 后台启动,agent 分步驱动
  - 本地回调仅 loopback 双栈(IPv4 + IPv6);callback handler 验 state + code 存在
    → 立即返回 200 HTML;PKCE 保证 code 截获不可兑换
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx

# 让 SKILL_DIR(skill_config.py 所在目录)对 Popen 子进程也可见。
# 修复:subprocess.Popen 启动的 _run_callback_server 子进程只把脚本所在
# iwp/scripts/ 加进 sys.path,找不到 SKILL_DIR 根目录的 skill_config.py。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import skill_config
from scripts import token_store

logger = logging.getLogger(__name__)


# === PKCE ===

def _b64url_nopad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """生成 PKCE 配对。

    Returns:
        (verifier, challenge)
        - verifier: 64 字节随机 → base64url(约 86 字符)
        - challenge: sha256(verifier) → base64url(43 字符)

    符合 RFC 7636:
      - verifier 长度 43-128 ✓
      - challenge 长度 43 ✓
      - 字符集 [A-Za-z0-9-._~] ✓(base64url 子集)
    """
    verifier_bytes = secrets.token_bytes(64)
    verifier = _b64url_nopad(verifier_bytes)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _b64url_nopad(digest)
    return verifier, challenge


def generate_state() -> str:
    """生成一次性 state(secrets.token_urlsafe(16) = 22 字符)。"""
    return secrets.token_urlsafe(skill_config.STATE_BYTES)


# === URL 构造 ===

def build_authorize_url(
    *,
    verifier: str | None = None,
    state: str | None = None,
) -> tuple[str, str, str]:
    """构造 /authorize URL。

    Returns:
        (url, verifier, state): agent 把 url 给用户访问;verifier 与 state 由
        本地保留供 callback 后验签 + token 交换。
    """
    if verifier is None or state is None:
        verifier, challenge = generate_pkce()
        state = generate_state()
    else:
        # 用 caller 提供的 verifier 时,需重算 challenge
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = _b64url_nopad(digest)

    params = {
        "response_type": "code",
        "client_id": skill_config.CLIENT_ID,
        "redirect_uri": skill_config.REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        # RFC 8707 必须,等于 MCP_PUBLIC_BASE_URL(与 MCP 端 _canonical_resource() 一致)
        "resource": skill_config.MCP_PUBLIC_BASE_URL,
    }
    url = f"{skill_config.MCP_AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"
    return url, verifier, state


# === 本地回调 server ===

class CallbackResult:
    """回调结果容器(线程安全)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._code: str | None = None
        self._state: str | None = None
        self._error: str | None = None
        self._received_at: float = 0.0

    def set_success(self, code: str, state: str) -> None:
        with self._lock:
            self._code = code
            self._state = state
            self._received_at = time.time()

    def set_error(self, error: str) -> None:
        with self._lock:
            self._error = error
            self._received_at = time.time()

    def is_received(self) -> bool:
        with self._lock:
            return self._code is not None or self._error is not None

    def get(self) -> dict[str, Any]:
        with self._lock:
            return {
                "code": self._code,
                "state": self._state,
                "error": self._error,
                "received_at": self._received_at,
            }


def _make_handler(result: CallbackResult, expected_state: str, result_file: str):
    """构造 HTTPServer handler(闭包捕获 expected_state)。"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - 重写静默
            pass  # 静默(避免污染 skill 输出)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            state = qs.get("state", [None])[0]
            error = qs.get("error", [None])[0]

            if error:
                result.set_error(error)
                body = _render_html("授权失败", f"<p>错误:{error}</p>")
            elif not code:
                result.set_error("missing_code")
                body = _render_html("授权失败", "<p>未收到 code 参数</p>")
            elif state != expected_state:
                result.set_error("state_mismatch")
                body = _render_html(
                    "授权失败",
                    "<p>state 不匹配(可能是 CSRF 攻击)。请重试。</p>",
                )
            else:
                result.set_success(code, state)
                body = _render_html(
                    "授权成功",
                    "<p>可关闭此窗口返回终端。</p>"
                    "<p style='color:#888;font-size:12px'>"
                    "MCP server 正在向本地 :9999 写入 code,agent 将自动继续。</p>",
                )

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

            # 持久化到 result_file(供 agent 轮询)
            # 权限收紧 600:文件含 authorization code,与 token_cache 同等保护
            try:
                rf = Path(result_file)
                rf.write_text(
                    json.dumps(result.get(), ensure_ascii=False),
                    encoding="utf-8",
                )
                try:
                    os.chmod(rf, 0o600)
                except (OSError, NotImplementedError):
                    pass
            except OSError:
                pass

    return Handler


def _render_html(title: str, body: str) -> bytes:
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{title}</title>
<style>
body {{
  font-family: -apple-system, system-ui, sans-serif;
  max-width: 480px; margin: 60px auto; padding: 0 20px;
  text-align: center; color: #333;
}}
h1 {{ font-size: 20px; }}
</style></head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>"""
    return html.encode("utf-8")


class _HTTPServerV6(HTTPServer):
    """IPv6 回环 HTTPServer(仅绑 [::1],保持 loopback-only 安全边界)。

    Windows 默认 AF_INET6 socket 的 IPV6_V6ONLY 语义依系统而定,
    显式置 1 固化「只接 IPv6 回环」:IPv4 由并存的 AF_INET server 接管,
    不通过 v4-mapped 地址混栈。
    """

    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


def _try_bind_ipv6_loopback(port: int, handler_cls: type) -> HTTPServer | None:
    """尝试绑定 [::1]:port;失败(如系统禁用 IPv6)返回 None,由调用方降级。"""
    try:
        return _HTTPServerV6(("::1", port), handler_cls)
    except OSError as exc:
        logger.warning("绑定 [::1]:%d 失败: %s", port, exc)
        return None


def _port_in_use(port: int, host: str = skill_config.LOCAL_CALLBACK_HOST) -> bool:
    """检查端口是否被占用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


def _can_connect(host: str, port: int, timeout_s: float = 1.0) -> bool:
    """探测 host:port 是否可建立 TCP 连接(IPv6 地址需含冒号)。"""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(timeout_s)
        return s.connect_ex((host, port)) == 0


def _wait_callback_ready(port: int, timeout_s: float = 10.0) -> dict[str, bool]:
    """等待本地回调端口进入监听(IPv4 硬性 / IPv6 软性)。

    消除启动竞态:auth start 的 Popen 到 server 实际 bind 之间有秒级窗口,
    浏览器先于监听到达会报「localhost 拒绝连接」;交付 URL 前探测到位。

    Returns:
        {"ipv4": bool, "ipv6": bool};ipv4 恒 True(否则抛 RuntimeError)。

    Raises:
        RuntimeError: IPv4 超时未监听(回调进程启动即死)
    """
    deadline = time.time() + timeout_s
    v4_ok = _can_connect("127.0.0.1", port)
    while not v4_ok and time.time() < deadline:
        time.sleep(0.3)
        v4_ok = _can_connect("127.0.0.1", port)
    if not v4_ok:
        raise RuntimeError(
            f"本地回调 server 启动失败:127.0.0.1:{port} 在 {timeout_s:.0f}s 内未进入监听"
            "(进程可能启动即死;检查 python 环境与依赖)"
        )
    # IPv6 软探测:双栈绑定失败(系统禁用 IPv6)不阻断授权,状态交由输出字段提示
    v6_ok = _can_connect("::1", port)
    if not v6_ok:
        logger.warning("[::1]:%d 未监听(降级模式);浏览器将 localhost 解析为 ::1 时回调会失败", port)
    return {"ipv4": True, "ipv6": v6_ok}


def _list_listeners(port: int, host: str = skill_config.LOCAL_CALLBACK_HOST):
    """列出占用 port 的 LISTEN 进程(pid + Laddr);依赖 psutil。"""
    try:
        import psutil
    except ImportError:
        return []
    out = []
    for c in psutil.net_connections(kind="inet"):
        if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
            out.append(c)
    return out


def run_local_callback_server(
    *,
    expected_state: str,
    result_file: str | None = None,
    timeout_s: int = skill_config.AUTHORIZE_TIMEOUT_S,
) -> dict[str, Any]:
    """前台运行 callback server(阻塞,直到 timeout 或收到回调)。

    Args:
        expected_state: 用于验签的 state(由 build_authorize_url 生成)
        result_file: 同时把结果持久化到此文件(供外部轮询;默认 None)
        timeout_s: 最大等待时间(秒;默认 5 分钟)

    Returns:
        dict: {code, state, error, received_at}

    用法:
        result = auth.run_local_callback_server(expected_state=state)
        if result['error']:
            ...
        elif result['code']:
            token = auth.exchange_code_for_token(result['code'], verifier)
    """
    if _port_in_use(skill_config.LOCAL_CALLBACK_PORT):
        raise RuntimeError(
            f"本地回调端口 {skill_config.LOCAL_CALLBACK_PORT} 已被占用。"
            f"请检查占用进程:`netstat -ano | findstr :{skill_config.LOCAL_CALLBACK_PORT}` "
            "(Windows) / `lsof -i :9999` (macOS/Linux)"
        )

    if result_file is None:
        result_file = str(skill_config.SKILL_DIR / ".callback_result.json")

    result = CallbackResult()
    handler_cls = _make_handler(result, expected_state, result_file)

    # loopback 双栈:redirect_uri 用 localhost,浏览器可能解析到 127.0.0.1(IPv4)
    # 或 ::1(IPv6);只绑单栈时另一栈连接被拒(实测:浏览器报「localhost 拒绝连接」)。
    # IPv4 与 IPv6 各起一个 server,共享同一 CallbackResult,均保持 loopback-only。
    servers: list[HTTPServer] = [
        HTTPServer(
            (skill_config.LOCAL_CALLBACK_HOST, skill_config.LOCAL_CALLBACK_PORT),
            handler_cls,
        )
    ]
    v6_server = _try_bind_ipv6_loopback(skill_config.LOCAL_CALLBACK_PORT, handler_cls)
    if v6_server is not None:
        servers.append(v6_server)
    else:
        logger.warning(
            "IPv6 回环 [::1]:%d 绑定失败,降级为仅 IPv4", skill_config.LOCAL_CALLBACK_PORT
        )
    for server in servers:
        server.timeout = 1.0  # 1s tick 用于主循环检查
    logger.info(
        "本地回调 server 启动: %s",
        ", ".join(f"{server.server_address[0]}:{server.server_address[1]}" for server in servers),
    )

    deadline = time.time() + timeout_s
    try:
        while time.time() < deadline:
            for server in servers:
                server.handle_request()
                if result.is_received():
                    break
            if result.is_received():
                break
    finally:
        for server in servers:
            server.server_close()

    if not result.is_received():
        logger.warning("本地回调超时(%ds)", timeout_s)
        result.set_error("timeout")

    logger.info("回调结果: %s", {k: v for k, v in result.get().items() if k != "received_at"})
    return result.get()


# === Token 交换 ===

def exchange_code_for_token(
    *,
    code: str,
    verifier: str,
    timeout_s: int = skill_config.HTTP_TIMEOUT_S,
) -> dict[str, Any]:
    """用 code + verifier 换 access_token + refresh_token。

    步骤:
      1. POST /token (form-urlencoded)
      2. 必带:grant_type=authorization_code, code, code_verifier,
         redirect_uri, client_id, resource
      3. 返回 {access_token, refresh_token, expires_in, scope, mcp_session_id}

    Raises:
        RuntimeError: 协议错误(4xx 非预期)
    """
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": skill_config.REDIRECT_URI,
        "client_id": skill_config.CLIENT_ID,
        "resource": skill_config.MCP_PUBLIC_BASE_URL,
    }
    return _post_token(payload, timeout_s=timeout_s)


def _post_token(
    payload: dict[str, str],
    *,
    timeout_s: int = skill_config.HTTP_TIMEOUT_S,
) -> dict[str, Any]:
    """调 /token 端点,返回解析后的 JSON。

    失败 → RuntimeError(消息去敏感化)。
    """
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                skill_config.MCP_TOKEN_ENDPOINT,
                data=payload,
                headers={"Accept": "application/json"},
            )
    except (httpx.ConnectError, httpx.ReadTimeout) as exc:
        raise RuntimeError(f"MCP /token 不可达: {exc.__class__.__name__}") from exc

    if resp.status_code != 200:
        # OAuth 错误:error + error_description
        try:
            err_body = resp.json()
            err = err_body.get("error", "unknown")
            desc = err_body.get("error_description", "")
            raise RuntimeError(f"MCP /token 返回 {resp.status_code}: {err} {desc}")
        except (json.JSONDecodeError, ValueError):
            raise RuntimeError(f"MCP /token 返回 {resp.status_code}")

    body = resp.json()
    return body


def refresh_access_token(
    *,
    refresh_token: str,
    timeout_s: int = skill_config.HTTP_TIMEOUT_S,
) -> dict[str, Any]:
    """用 refresh_token 换新 access_token(+ 轮换 refresh_token)。

    Raises:
        RuntimeError: 协议错误(401 / 4xx)
    """
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": skill_config.CLIENT_ID,
        "resource": skill_config.MCP_PUBLIC_BASE_URL,
    }
    return _post_token(payload, timeout_s=timeout_s)


# === Token 持久化辅助 ===

def _parse_expires_in(expires_in: int) -> int:
    return int(time.time()) + int(expires_in)


def save_tokens_from_exchange(
    body: dict[str, Any],
    *,
    mcp_session_id: str | None = None,
) -> dict[str, Any]:
    """把 /token 响应转为 token_dict 并落盘。

    Returns:
        token_dict(已存盘)
    """
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    expires_in = body.get("expires_in", skill_config.MCP_ACCESS_TOKEN_TTL_S)
    if not access or not refresh:
        raise RuntimeError(f"token 响应缺少 access_token/refresh_token: {body.keys()}")

    token_dict = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": _parse_expires_in(expires_in),
        "scope": body.get("scope", ""),
        "client_id": skill_config.CLIENT_ID,
        "resource": skill_config.MCP_PUBLIC_BASE_URL,
        "mcp_session_id": mcp_session_id or "",
    }
    token_store.save(token_dict)
    return token_dict


# === 顶层入口:agent 用 ===

def ensure_authorized(*, force_reauth: bool = False) -> dict[str, Any]:
    """确保 skill 持有有效 token;否则走完整 OAuth 流程。

    这是 agent 在调用任何 MCP 工具前的统一入口。

    Args:
        force_reauth: True → 忽略现有缓存,重新走 OAuth

    Returns:
        token_dict: 包含 access_token / refresh_token / expires_at 等

    Raises:
        RuntimeError: 流程失败(MCP 不可达 / 用户超时 / token 无效)
    """
    if not force_reauth:
        existing = token_store.load()
        if existing and token_store.is_access_token_valid(existing):
            return existing

    # 1. PKCE 生成 + 构造 URL
    url, verifier, state = build_authorize_url()
    result_file = str(skill_config.SKILL_DIR / ".callback_result.json")

    # 2. 准备结果文件(清旧)
    Path(result_file).unlink(missing_ok=True)

    # 3. 启动本地回调 server(后台进程)
    logger.info("启动本地回调 server(后台)...")
    # 注:argparse 在 _cli_run_callback_server 里只识别 --expected-state/--result-file/--timeout,
    # 没有定义 subcommand,所以这里不能传 "_run_callback_server" 这种 positional
    # (会被 argparse 当 unrecognized 拒绝 → 进程启动即死 → 9999 永远没人监听)

    # 注:每次 ensure_authorized 启动新 callback server 前,先确保 9999 端口空闲。
    # 否则旧 callback server(由前一次 ensure_authorized 启动)仍占着 9999,
    # 新 server 在 _port_in_use 检查处 raise → 进程立即死 → 9999 上跑的还是旧 server,
    # 它的 expected_state 不匹配新 URL 的 state → state_mismatch,code 丢失。
    self_script = str(Path(__file__).resolve())
    try:
        import psutil
    except ImportError:
        psutil = None  # type: ignore[assignment]
    for conn in _list_listeners(skill_config.LOCAL_CALLBACK_PORT):
        try:
            old = conn.pid
            if not old:
                continue
            # 防误杀:仅当占用进程的命令行确实是本 callback server 脚本时才 kill。
            # 否则(无关本地应用占用 9999)跳过,由下方 Popen 启动失败自然报错。
            if psutil is not None:
                try:
                    cmdline = psutil.Process(old).cmdline()
                    if not any(self_script in (arg or "") for arg in cmdline):
                        logger.warning(
                            "端口 %d 被 PID %d 占用,但命令行不含本脚本(%s),跳过 kill: %s",
                            skill_config.LOCAL_CALLBACK_PORT, old, self_script,
                            " ".join(cmdline)[:200],
                        )
                        continue
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    # 进程已退出 / 无法读取命令行:保守起见不 kill
                    logger.warning("端口 %d 占用进程 PID %d 无法确认身份,跳过 kill",
                                   skill_config.LOCAL_CALLBACK_PORT, old)
                    continue
            os.kill(old, 9)
            logger.warning("杀掉旧 callback server pid=%d", old)
        except (OSError, ProcessLookupError):
            pass

    p = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--expected-state", state,
            "--result-file", result_file,
            "--timeout", str(skill_config.AUTHORIZE_TIMEOUT_S),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 3.5 等回调 server 就绪再交付 URL(消除启动竞态与降级态不可见)。
    callback_ready = _wait_callback_ready(skill_config.LOCAL_CALLBACK_PORT)

    # 4. 把 URL 写到一个 agent 可读的"next-step"文件;
    #    SKILL.md 指导 agent 向用户展示(见 user-interaction.md)。
    # 注:verifier 必须也持久化,否则 agent 拿到 code 后没法调 /token 换 access_token
    # (verifier 只活在 ensure_authorized() 的局部变量里,丢了就要重新走流程)
    next_step = skill_config.SKILL_DIR / ".oauth_next_step.json"
    next_step.write_text(
        json.dumps({
            "authorize_url": url,
            "expected_state": state,
            "code_verifier": verifier,
            "result_file": result_file,
            "callback_pid": p.pid,
            "callback_ready": callback_ready,
            "timeout_s": skill_config.AUTHORIZE_TIMEOUT_S,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # 权限收紧 600:文件含 PKCE verifier(明文),finalize 前与 token_cache 同等保护
    try:
        os.chmod(next_step, 0o600)
    except (OSError, NotImplementedError):
        pass

    raise RuntimeError(
        f"需要用户授权。请读取 {next_step} 拿 authorize_url,"
        f"向用户展示 authorize_url(呈现方式见 references/_shared/user-interaction.md);"
        f"然后调用 poll_callback_result() 等待回调。"
        f"授权后本地回调 server (pid={p.pid}) 会自动接收 code。"
    )


def poll_callback_result(*, timeout_s: int = 300) -> dict[str, Any]:
    """轮询本地回调结果文件(供 agent 异步等待)。

    Returns:
        dict: {code, state, error, received_at}

    Raises:
        RuntimeError: 等待超时
    """
    result_file = skill_config.SKILL_DIR / ".callback_result.json"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if result_file.is_file():
            try:
                payload = json.loads(result_file.read_text(encoding="utf-8"))
                if payload.get("code") or payload.get("error"):
                    return payload
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(1.0)
    raise RuntimeError(f"等待回调超时({timeout_s}s)")


def finalize_authorization(*, code: str, verifier: str) -> dict[str, Any]:
    """OAuth 流程收尾:code → token → 落盘。

    用法(agent):
        1. ensure_authorized() → 异常信息拿到 URL
        2. 向用户展示 URL(见 user-interaction.md)
        3. poll_callback_result() → {code, state}
        4. finalize_authorization(code=..., verifier=...) → token_dict
    """
    body = exchange_code_for_token(code=code, verifier=verifier)
    token_dict = save_tokens_from_exchange(body)
    _cleanup_oauth_artifacts()
    return token_dict


def _cleanup_oauth_artifacts() -> None:
    """删除授权中间产物(成功路径)。

    .oauth_next_step.json 含明文 PKCE verifier,.callback_result.json 含
    authorization code;token 已安全落盘后两者均无用,必须清除
    (权限 600 只是缓解,不残留才是正解)。失败路径保留文件便于排障。
    """
    for name in (".oauth_next_step.json", ".callback_result.json"):
        try:
            (skill_config.SKILL_DIR / name).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理 %s 失败: %s", name, exc)


def auto_refresh_if_needed(
    token_dict: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """若 token 即将过期 → 自动 refresh。

    Args:
        token_dict: 现有 token
        force: True → 无视 expires_at 立即 refresh

    Returns:
        新 token_dict(已落盘)

    Raises:
        RuntimeError: refresh 失败(需重新授权)
    """
    if not force and token_store.is_access_token_valid(token_dict):
        return token_dict

    refresh_tok = token_dict.get("refresh_token")
    if not refresh_tok:
        raise RuntimeError("refresh_token 缺失,需重新授权")

    try:
        body = refresh_access_token(refresh_token=refresh_tok)
    except RuntimeError as exc:
        # refresh 失败 → 清缓存,引导重新授权
        token_store.invalidate()
        raise RuntimeError(f"refresh_token 失效({exc});请重新授权") from exc

    return save_tokens_from_exchange(
        body,
        mcp_session_id=token_dict.get("mcp_session_id"),
    )


# === CLI:本地回调 server 入口(供 Popen 启动) ===

def _cli_run_callback_server():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-state", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    try:
        result = run_local_callback_server(
            expected_state=args.expected_state,
            result_file=args.result_file,
            timeout_s=args.timeout,
        )
        # 结果已写入 result_file;正常返回
        sys.exit(0)
    except Exception as exc:
        # 写错误到 result_file
        Path(args.result_file).write_text(
            json.dumps({"error": f"server_error: {exc}", "received_at": time.time()}),
            encoding="utf-8",
        )
        sys.exit(1)


if __name__ == "__main__":
    _cli_run_callback_server()