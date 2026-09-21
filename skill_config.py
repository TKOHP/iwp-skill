"""iwp skill 配置常量.

集中管理 MCP server 端点、客户端身份、缓存路径等。所有其他脚本均从此处读取。
环境变量(.env / shell)可覆盖默认值。
"""
from __future__ import annotations

import os
from pathlib import Path


# === MCP server ===
MCP_PUBLIC_BASE_URL = os.environ.get(
    "MCP_PUBLIC_BASE_URL", "http://127.0.0.1:8000"
).rstrip("/")
MCP_AUTHORIZE_ENDPOINT = f"{MCP_PUBLIC_BASE_URL}/authorize"
MCP_TOKEN_ENDPOINT = f"{MCP_PUBLIC_BASE_URL}/token"
MCP_REVOKE_ENDPOINT = f"{MCP_PUBLIC_BASE_URL}/revoke"
MCP_JWKS_ENDPOINT = f"{MCP_PUBLIC_BASE_URL}/jwks"
MCP_TOOLS_ENDPOINT = f"{MCP_PUBLIC_BASE_URL}/mcp"  # Streamable HTTP

# === OAuth 客户端身份 ===
# 阶段 1:本地 skill 用静态 client_id,必须注册到 MCP_ALLOWED_CLIENTS
CLIENT_ID = os.environ.get("IWP_CLIENT_ID", "test-client")
# 本地回调 server(127.0.0.1 独占端口;与 MCP_ALLOWED_REDIRECT_URIS 一致)
REDIRECT_URI = os.environ.get("IWP_REDIRECT_URI", "http://localhost:9999/callback")
LOCAL_CALLBACK_HOST = "127.0.0.1"
LOCAL_CALLBACK_PORT = int(os.environ.get("IWP_LOCAL_CALLBACK_PORT", "9999"))

# === PKCE / 授权参数 ===
AUTHORIZE_TIMEOUT_S = 300  # 5 分钟(与 MCP 事务 TTL 一致)
STATE_BYTES = 16  # secrets.token_urlsafe(16) = 22 字符 base64url

# === Token 缓存(本机 Fernet 加密) ===
SKILL_DIR = Path(__file__).resolve().parent
TOKEN_KEY_PATH = SKILL_DIR / ".token_key"  # Fernet key,权限 600
TOKEN_CACHE_PATH = SKILL_DIR / ".token_cache.enc"  # Fernet 加密的 JSON
TOKEN_KEY_TTL_S = 30 * 24 * 3600  # 30 天(refresh_token TTL 对齐)

# === Swagger 缓存(tool 列表本地化,减少 RPC) ===
SWAGGER_META_PATH = SKILL_DIR / ".swagger_meta.enc"
SWAGGER_META_TTL_S = 30 * 24 * 3600  # 30 天

# === Token 寿命 / 缓存策略 ===
MCP_ACCESS_TOKEN_TTL_S = 1800  # 30 分钟(JWT exp)
IWP_REFRESH_TOKEN_TTL_S = 30 * 24 * 3600  # 30 天
# 提前 5 分钟续期
REFRESH_LEEWAY_S = 5 * 60

# === 重试 / 超时 ===
HTTP_TIMEOUT_S = 30  # 单次请求超时
MAX_RETRIES = 1  # 5xx 自动重试 1 次
RETRY_BACKOFF_S = 0.2

# === MCP 协议自适应(见 docs/技能内置MCP-client完整实现-方案设计.md) ===
# client 支持的协议版本(降序,协商时取最高共同版本)
SUPPORTED_PROTOCOL_VERSIONS = [
    "2026-07-28",   # 无状态:无握手/无会话,_meta 携带元数据
    "2025-11-25",   # 会话式:initialize + notifications/initialized + MCP-Protocol-Version 头
    "2025-06-18",   # 会话式:结构化输出/分页
    "2025-03-26",   # 会话式:Streamable HTTP 首版
]
# 无状态代(2026-07-28)行为分界
STATELESS_PROTOCOL_VERSION = "2026-07-28"
# client 身份(initialize clientInfo / _meta clientInfo)
CLIENT_NAME = "iwp-skill"
CLIENT_VERSION = "1.0.0"


def ensure_skill_dir() -> None:
    """确保 skill 目录存在(首次安装时)。"""
    SKILL_DIR.mkdir(parents=True, exist_ok=True)