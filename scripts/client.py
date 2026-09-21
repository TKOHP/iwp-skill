"""iwp skill - MCP 客户端(协议自适应,两代行为).

覆盖:
  - 会话式协议(2025-03-26 / 2025-06-18 / 2025-11-25):
    initialize 握手 → notifications/initialized → Mcp-Session-Id /
    MCP-Protocol-Version 头 → 会话 DELETE 终止
  - 无状态协议(2026-07-28):
    无握手;每请求带 MCP-Protocol-Version / Mcp-Method / Mcp-Name 头,
    _meta 携带 protocolVersion / clientInfo / clientCapabilities
  - server/discover 探测协商版本;协商结果按 endpoint 进程内缓存

保留(不变):
  - Bearer 注入 + 401 自动 refresh + 5xx 重试
  - JSON-RPC 错误码 → 异常翻译;工具 isError 处理

规范依据:docs/技能内置MCP-client完整实现-方案设计.md §二/§五。
"""
from __future__ import annotations

import itertools
import json
import logging
import time
from typing import Any

import httpx

import skill_config
from scripts import auth, token_store
from scripts import mcp_protocol as proto

logger = logging.getLogger(__name__)

# 协商缓存:endpoint → (mode, version)。server 升级协议需重启 agent 进程,
# 可接受;避免一次性 client 每次调用都探测。
_NEGOTIATION_CACHE: dict[str, tuple[str, str]] = {}


class McpToolError(Exception):
    """MCP 工具调用错误。"""

    def __init__(self, code: str, message: str, *, raw: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.raw = raw or {}


class McpAuthExpiredError(McpToolError):
    """MCP / IWP 凭证失效 → 需重新授权。"""

    def __init__(self, message: str = "凭证已失效,请重新授权") -> None:
        super().__init__("credential_expired", message)


class McpClient:
    """同步 MCP 客户端(配合 with 使用)。

    用法不变:
        with McpClient() as client:
            result = client.call_tool("list_my_tasks", {"status": "in_progress"})
    """

    def __init__(
        self,
        *,
        mcp_endpoint: str | None = None,
        timeout_s: int = skill_config.HTTP_TIMEOUT_S,
    ) -> None:
        self.endpoint = (mcp_endpoint or skill_config.MCP_TOOLS_ENDPOINT).rstrip("/")
        self.timeout_s = timeout_s
        self._client: httpx.Client | None = None
        self._ids = itertools.count(1)
        # 协商状态
        self.mode: str | None = None          # proto.MODE_SESSION / MODE_STATELESS
        self.protocol_version: str | None = None
        self._session_id: str | None = None
        self.server_info: dict[str, Any] = {}

    def __enter__(self) -> "McpClient":
        self._client = httpx.Client(timeout=self.timeout_s)
        return self

    def __exit__(self, *exc) -> None:
        # 会话式协议:终止 SHOULD 发 DELETE(server 释放会话),失败静默
        if self._client is not None and self.mode == proto.MODE_SESSION and self._session_id:
            try:
                self._client.delete(
                    self.endpoint,
                    headers={proto.HEADER_SESSION_ID: self._session_id},
                    timeout=5,
                )
            except Exception as exc:  # noqa: BLE001 - 终止失败不影响主流程
                logger.debug("会话 DELETE 失败(忽略): %s", exc)
        if self._client is not None:
            self._client.close()
            self._client = None

    # === Public API ===

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """调 MCP 工具。返回工具数据;错误语义见 McpToolError 各子类。"""
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        result = self._rpc_call("tools/call", params, mcp_name=name)
        return self._parse_tool_result(result, name=name)

    def list_tools(self) -> list[dict]:
        """拉取 MCP 工具清单(自动跟随 cursor 分页,合并返回)。"""
        tools: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            result = self._rpc_call("tools/list", params)
            tools.extend(t for t in (result.get("tools") or []) if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    # === 协商与会话 ===

    def _ensure_session(self, headers_base: dict[str, str]) -> None:
        """确保协商完成;会话式协议完成 initialize + initialized。"""
        if self.mode is not None:
            return

        cached = _NEGOTIATION_CACHE.get(self.endpoint)
        if cached:
            self.mode, self.protocol_version = cached
        else:
            self._negotiate(headers_base)
            _NEGOTIATION_CACHE[self.endpoint] = (self.mode, self.protocol_version)

        if self.mode == proto.MODE_SESSION and self._session_id is None:
            self._initialize(headers_base)

    def _negotiate(self, headers_base: dict[str, str]) -> None:
        """server/discover 探测 → 回退 initialize 提议(见方案 §四)。"""
        # 1. 探针:2026-07-28 server/discover
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "server/discover",
            "params": self._stateless_meta({}),
        }
        status, _, messages, raw_text, err = self._post_raw(body, headers_base)

        if err is not None:
            supported = proto.extract_supported_versions(err)
            if proto.method_not_recognized(err, status):
                # 旧会话式 server:无 discover 方法 → 回退会话式提议
                self.mode = proto.MODE_SESSION
                self.protocol_version = "2025-11-25"
                logger.debug("discover 不支持(-32601),回退会话式 %s", self.protocol_version)
                return
            common = proto.pick_common_version(supported)
            if common is not None:
                self._adopt(common)
                return
            # 非版本类错误且无 supported 列表 → 带着原始错误走会话式回退
            self.mode = proto.MODE_SESSION
            self.protocol_version = "2025-11-25"
            logger.debug("discover 失败(%s),回退会话式提议", err.get("message"))
            return

        # 探针成功 → server 支持 2026-07-28 发现
        result = self._response_result(messages, body["id"], raw_text)
        advertised = result.get("supportedVersions") or [skill_config.STATELESS_PROTOCOL_VERSION]
        if isinstance(advertised, list) is False:
            advertised = [skill_config.STATELESS_PROTOCOL_VERSION]
        common = proto.pick_common_version(advertised)
        if common is None:
            raise McpToolError(
                "version_mismatch",
                f"server 支持版本 {advertised} 与 client 支持集 "
                f"{skill_config.SUPPORTED_PROTOCOL_VERSIONS} 无交集",
            )
        self._adopt(common)

    def _adopt(self, version: str) -> None:
        self.protocol_version = version
        self.mode = proto.MODE_STATELESS if proto.is_stateless(version) else proto.MODE_SESSION

    def _initialize(self, headers_base: dict[str, str]) -> None:
        """会话式握手:initialize → 校验版本 → notifications/initialized。"""
        proposed = self.protocol_version or "2025-11-25"
        for _ in range(2):  # 最多一次版本重协商
            body = {
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": "initialize",
                "params": {
                    "protocolVersion": proposed,
                    # capabilities 声明即契约:仅 tools,server 不会发起
                    # sampling/elicitation/roots 等未声明请求
                    "capabilities": {},
                    "clientInfo": {
                        "name": skill_config.CLIENT_NAME,
                        "version": skill_config.CLIENT_VERSION,
                    },
                },
            }
            status, resp_headers, messages, raw_text, err = self._post_raw(body, headers_base)

            if err is not None:
                supported = proto.extract_supported_versions(err)
                common = proto.pick_common_version(supported)
                if common is not None and common != proposed:
                    proposed = common
                    continue
                raise McpToolError(
                    "version_mismatch" if supported else "protocol",
                    f"initialize 失败: [{err.get('code')}] {err.get('message')}",
                    raw=err,
                )

            result = self._response_result(messages, body["id"], raw_text)
            server_version = str(result.get("protocolVersion") or proposed)
            if server_version not in skill_config.SUPPORTED_PROTOCOL_VERSIONS:
                # client 不支持 server 响应版本 → 规范 SHOULD 断开;先按错误给出明确信息
                raise McpToolError(
                    "version_mismatch",
                    f"server 协议版本 {server_version} 不在 client 支持集内",
                )
            self.protocol_version = server_version
            self._adopt(server_version)
            self.server_info = result.get("serverInfo") or {}

            sid = resp_headers.get(proto.HEADER_SESSION_ID)
            if not sid:
                raise McpToolError(
                    "protocol", "initialize 响应缺少 mcp-session-id 头",
                )
            self._session_id = sid
            break

        # MUST:握手完成后发 notifications/initialized(POST,容忍 2xx 任意形态)
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        note_headers = self._session_headers(headers_base)
        try:
            self._post_raw(note, note_headers)
        except McpToolError as exc:
            logger.warning("notifications/initialized 发送失败(继续): %s", exc)

    # === RPC 通道 ===

    def _rpc_call(self, method: str, params: dict[str, Any], *, mcp_name: str | None = None) -> dict[str, Any]:
        """发送一次 JSON-RPC 请求并返回 result(含会话失效自动重建)。"""
        assert self._client is not None, "McpClient 必须在 with 中使用"
        tokens = token_store.load()
        if not tokens:
            raise McpAuthExpiredError("无 token,请先调用 auth.ensure_authorized()")
        try:
            tokens = auth.auto_refresh_if_needed(tokens)
        except RuntimeError as exc:
            raise McpAuthExpiredError(str(exc)) from exc

        headers_base = {"Authorization": f"Bearer {tokens['access_token']}"}
        self._ensure_session(headers_base)

        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        if self.mode == proto.MODE_STATELESS:
            body["params"] = {**params, "_meta": self._stateless_meta({})}

        headers = dict(headers_base)
        if self.mode == proto.MODE_SESSION:
            headers.update(self._session_headers(headers_base))
        else:
            headers[proto.HEADER_PROTOCOL_VERSION] = self.protocol_version
            headers[proto.HEADER_MCP_METHOD] = method
            if mcp_name:
                headers[proto.HEADER_MCP_NAME] = mcp_name

        status, _, messages, raw_text, err = self._post_raw(body, headers, tokens=tokens)

        if err is not None:
            self._raise_rpc_error(err, method=method)

        result = self._response_result(messages, body["id"], raw_text)
        return self._normalize_result(result)

    # === 底层 POST(401 refresh / 5xx 重试 / JSON 与 SSE 双解析) ===

    def _post_raw(
        self,
        body: dict[str, Any],
        headers_base: dict[str, str],
        *,
        tokens: dict[str, Any] | None = None,
    ) -> tuple[int, httpx.Headers, list[dict[str, Any]] | None, str, dict[str, Any] | None]:
        """POST 一个 JSON-RPC 消息。返回 (status, headers, messages, raw_text, error)。

        - 401 → 强制 refresh 后重试一次(调用方传入 tokens 时才启用);
        - 5xx → 现有退避重试;
        - 响应 JSON / SSE 双解析,产出消息列表;
        - error 字段不在此抛出(协商器需要原始错误),由调用方决定语义。
        """
        assert self._client is not None, "McpClient 必须在 with 中使用"
        headers = {
            **headers_base,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id and self.mode == proto.MODE_SESSION:
            headers[proto.HEADER_SESSION_ID] = self._session_id

        last_exc: Exception | None = None
        for attempt in range(skill_config.MAX_RETRIES + 1):
            try:
                resp = self._client.post(self.endpoint, json=body, headers=headers)
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                last_exc = exc
                if attempt < skill_config.MAX_RETRIES:
                    time.sleep(skill_config.RETRY_BACKOFF_S)
                    continue
                raise McpToolError("network", f"MCP 不可达: {exc.__class__.__name__}")

            if resp.status_code == 401 and tokens is not None:
                # 凭证失效 → refresh + 重试一次
                try:
                    new_tokens = auth.auto_refresh_if_needed(tokens, force=True)
                except RuntimeError as exc:
                    token_store.invalidate()
                    raise McpAuthExpiredError(str(exc)) from exc
                headers["Authorization"] = f"Bearer {new_tokens['access_token']}"
                if attempt < skill_config.MAX_RETRIES:
                    continue
                raise McpAuthExpiredError("MCP 401 且 refresh 后仍失败")

            if resp.status_code >= 500 and attempt < skill_config.MAX_RETRIES:
                last_exc = McpToolError("upstream", f"MCP {resp.status_code};重试中")
                time.sleep(skill_config.RETRY_BACKOFF_S)
                continue
            break

        raw_text = resp.text or ""
        content_type = resp.headers.get("content-type", "")

        # JSON-RPC error 响应可能以 HTTP 错误码携带(如 400 UnsupportedProtocolVersion)
        jsonrpc_error: dict[str, Any] | None = None
        messages: list[dict[str, Any]] | None = None
        if "text/event-stream" in content_type:
            messages = proto.parse_sse_messages(raw_text)
        else:
            try:
                obj = json.loads(raw_text)
                messages = [obj] if isinstance(obj, dict) else []
            except json.JSONDecodeError:
                # 非 JSON 且非 SSE:按纯文本错误处理(保留原始信息)
                messages = []

        if messages:
            for msg in messages:
                if isinstance(msg, dict) and isinstance(msg.get("error"), dict):
                    jsonrpc_error = msg["error"]
                    break

        return resp.status_code, resp.headers, messages, raw_text, jsonrpc_error

    # === 流内消息处理 / 结果解析 ===

    def _response_result(
        self,
        messages: list[dict[str, Any]] | None,
        request_id: int,
        raw_text: str,
    ) -> dict[str, Any]:
        """挑出目标响应,处理流内其余消息(通知/服务器请求),返回 result。"""
        if not messages:
            raise McpToolError("protocol", f"MCP 返回不可解析: {raw_text[:200]}")

        response, others = proto.pick_response(messages, request_id)

        for msg in others:
            method = msg.get("method", "")
            if "id" in msg and method == "ping":
                # receiver MUST 及时回应 ping(会话式协议)
                self._respond_to_server(msg["id"], result={})
            elif "id" in msg:
                # 未声明能力的 server→client 请求:按规范拒绝
                logger.warning("拒绝未声明的服务器请求: %s", method)
                self._respond_to_server(
                    msg["id"], error={"code": proto.RPC_METHOD_NOT_FOUND,
                                      "message": f"client 未声明能力: {method}"}
                )
            else:
                logger.debug("忽略服务器通知: %s", method)

        if response is None:
            raise McpToolError("protocol", f"未收到 id={request_id} 的响应(流内含 {len(others)} 条其他消息)")

        if response.get("error"):
            err = response["error"]
            self._raise_rpc_error(err, method=f"request#{request_id}")

        result = response.get("result")
        if not isinstance(result, dict):
            raise McpToolError("protocol", "响应缺少 result 对象")

        # 2026-07-28:MRTR 暂不支持(未声明相关能力,server 正常不会发)
        if result.get("resultType") == proto.RESULT_TYPE_INPUT_REQUIRED:
            raise McpToolError(
                "protocol",
                "server 返回 input_required(多轮请求),当前客户端未启用 MRTR 能力",
            )
        return result

    def _respond_to_server(
        self, request_id: Any, *, result: dict | None = None, error: dict | None = None
    ) -> None:
        """向 server 回应一个 server→client 请求(ping 回应 / 能力外拒绝)。"""
        assert self._client is not None
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result or {}
        headers = {"Content-Type": "application/json"}
        if self.mode == proto.MODE_SESSION and self._session_id:
            headers[proto.HEADER_SESSION_ID] = self._session_id
            headers[proto.HEADER_PROTOCOL_VERSION] = self.protocol_version or ""
        try:
            self._client.post(self.endpoint, json=message, headers=headers, timeout=10)
        except Exception as exc:  # noqa: BLE001 - 应答失败不影响主请求
            logger.debug("回应服务器请求失败(忽略): %s", exc)

    def _normalize_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """tools/list 等非 tools/call 结果原样返回(供 list_tools 处理分页)。"""
        return result

    def _parse_tool_result(self, result: dict[str, Any], *, name: str) -> Any:
        """tools/call 结果解析:isError → 异常;content JSON 优先;structuredContent 兜底。

        content 优先的原因:工作流消费方依赖历史返回形状(内层 JSON dict),
        且 SDK 2.x 会把"返回 JSON 字符串"的工具包成
        structuredContent={"result": "<json 字符串>"}(非对象返回值的包装壳),
        直接采用会改变形状。structuredContent 仅在 content 缺失时兜底,
        并对 {"result": "<可解析 JSON 字符串>"} 包装壳解包。
        """
        if result.get("isError"):
            content = result.get("content") or []
            detail = ""
            if content and isinstance(content[0], dict):
                detail = str(content[0].get("text", ""))[:300]
            raise McpToolError("tool_error", detail or "工具执行失败", raw=result)

        content = result.get("content") or []
        if content and isinstance(content[0], dict) and "text" in content[0]:
            text = content[0]["text"]
            if isinstance(text, str) and text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text

        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            if set(structured) == {"result"} and isinstance(structured["result"], str):
                try:
                    return json.loads(structured["result"])
                except json.JSONDecodeError:
                    pass
            return structured
        return result

    def _raise_rpc_error(self, err: dict[str, Any], *, method: str) -> None:
        """JSON-RPC error → 统一异常(沿用既有语义映射)。"""
        code = err.get("code", -32000)
        message = err.get("message", "(no message)")

        if code in (-32001, "iwp_credential_expired", -32002, "mcp_token_invalid"):
            token_store.invalidate()
            raise McpAuthExpiredError(message)
        if code == proto.RPC_UNSUPPORTED_PROTOCOL_VERSION:
            supported = proto.extract_supported_versions(err)
            raise McpToolError(
                "version_mismatch",
                f"server 不支持协议版本 {self.protocol_version};"
                f"server 支持: {supported or '(未声明)'}",
                raw=err,
            )
        if code == proto.RPC_INVALID_PARAMS:
            raise McpToolError("validation", message, raw=err)
        if code == proto.RPC_METHOD_NOT_FOUND:
            raise McpToolError("not_found", f"方法不存在: {method}", raw=err)
        raise McpToolError(f"rpc_{code}", message, raw=err)

    # === 头 / 元数据构造 ===

    def _session_headers(self, headers_base: dict[str, str]) -> dict[str, str]:
        """会话式协议的完整请求头。"""
        headers = {
            **headers_base,
            proto.HEADER_SESSION_ID: self._session_id or "",
            proto.HEADER_PROTOCOL_VERSION: self.protocol_version or "",
        }
        headers.pop("Content-Type", None)  # 由 _post_raw 统一设
        headers.pop("Accept", None)
        return headers

    def _stateless_meta(self, extra: dict[str, Any]) -> dict[str, Any]:
        """2026-07-28 无状态模式的 params._meta。"""
        return {
            **extra,
            proto.META_PROTOCOL_VERSION: self.protocol_version
            or skill_config.STATELESS_PROTOCOL_VERSION,
            proto.META_CLIENT_INFO: {
                "name": skill_config.CLIENT_NAME,
                "version": skill_config.CLIENT_VERSION,
            },
            proto.META_CLIENT_CAPABILITIES: {},
        }


def call_tool(name: str, arguments: dict | None = None) -> Any:
    """便利函数:一次性调用(签名不变,工作流零改动)。"""
    with McpClient() as client:
        return client.call_tool(name, arguments)


def current_user_id() -> int | None:
    """从本地缓存的 MCP access_token sub claim 提取 user_id(无需验签,本地可信)。

    用于 client 端构造 user_id 过滤参数(如 my_reports)。
    """
    import jwt
    tokens = token_store.load()
    if not tokens:
        return None
    try:
        payload = jwt.decode(
            tokens["access_token"],
            options={"verify_signature": False},
        )
        sub = payload.get("sub")
        return int(sub) if sub is not None else None
    except Exception as exc:
        logger.warning("从 access_token 提取 user_id 失败: %s", exc)
        return None


def revoke_current_token() -> bool:
    """撤销当前 token(MCP /revoke + 清本地缓存)。"""
    tokens = token_store.load()
    if not tokens:
        return True

    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")

    success = True
    if access:
        try:
            with httpx.Client(timeout=skill_config.HTTP_TIMEOUT_S) as c:
                resp = c.post(
                    skill_config.MCP_REVOKE_ENDPOINT,
                    data={
                        "token": refresh or access,
                        "token_type_hint": "refresh_token" if refresh else "access_token",
                    },
                    headers={"Authorization": f"Bearer {access}"},
                )
                if resp.status_code not in (200, 401):
                    # 401 也视为"已失效,目标达成"
                    success = False
        except (httpx.ConnectError, httpx.ReadTimeout) as exc:
            logger.warning("MCP /revoke 调用失败: %s", exc)
            success = False

    token_store.invalidate()
    return success
