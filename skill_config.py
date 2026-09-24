"""iwp skill 配置常量.

集中管理 MCP server 端点、客户端身份、缓存路径等。所有其他脚本均从此处读取。

配置唯一外部来源是技能根目录的 .env 文件(SKILL_DIR/.env)。
shell 环境变量不再参与读取,避免宿主环境残留变量造成干扰;
取值语义: 代码默认值 < .env(该 KEY 在 .env 中存在才覆盖)。
"""
from __future__ import annotations

from pathlib import Path

# 技能根目录(.env 与凭证文件所在,路径解析与 cwd 无关)
SKILL_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> dict[str, str]:
    """解析技能根目录 .env(若存在),返回 KEY->VALUE 映射;不写入 os.environ.

    支持格式: KEY=VALUE、# 行注释与行尾注释、空行、可选 export 前缀、
    值两端单/双引号剥除、两侧空白剥除。不支持变量插值与多行值。
    非法行静默跳过,文件缺失/不可读时返回空 dict(零配置可用)。
    """
    path = SKILL_DIR / ".env"
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue  # 非法行跳过
        key = key.strip()
        value = value.strip()
        if value and value[0] not in "\"'":
            # 行尾注释仅在值未被引号包裹时剥离
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


# 模块导入时加载一次;配置常量在下方求值前完成,保证 .env 生效
_ENV_FILE = _load_dotenv()


def _cfg(key: str, default: str) -> str:
    """从 .env 取配置值;该 KEY 未设置时回退代码默认值。"""
    return _ENV_FILE.get(key, default)


# === MCP server ===
MCP_PUBLIC_BASE_URL = _cfg(
    "MCP_PUBLIC_BASE_URL", "http://127.0.0.1:8000"
).rstrip("/")
MCP_AUTHORIZE_ENDPOINT = f"{MCP_PUBLIC_BASE_URL}/authorize"
MCP_TOKEN_ENDPOINT = f"{MCP_PUBLIC_BASE_URL}/token"
MCP_REVOKE_ENDPOINT = f"{MCP_PUBLIC_BASE_URL}/revoke"
MCP_JWKS_ENDPOINT = f"{MCP_PUBLIC_BASE_URL}/jwks"
MCP_TOOLS_ENDPOINT = f"{MCP_PUBLIC_BASE_URL}/mcp"  # Streamable HTTP

# === OAuth 客户端身份 ===
# 阶段 1:本地 skill 用静态 client_id,必须注册到 MCP_ALLOWED_CLIENTS
CLIENT_ID = _cfg("IWP_CLIENT_ID", "test-client")
# 本地回调 server(127.0.0.1 独占端口;与 MCP_ALLOWED_REDIRECT_URIS 一致)
REDIRECT_URI = _cfg("IWP_REDIRECT_URI", "http://localhost:9999/callback")
LOCAL_CALLBACK_HOST = "127.0.0.1"
LOCAL_CALLBACK_PORT = int(_cfg("IWP_LOCAL_CALLBACK_PORT", "9999"))
# auth start 是否自动打开浏览器(默认开;0=仅返回 URL 由用户手动访问)
IWP_AUTO_OPEN = _cfg("IWP_AUTO_OPEN", "1") == "1"

# === PKCE / 授权参数 ===
AUTHORIZE_TIMEOUT_S = 300  # 5 分钟(与 MCP 事务 TTL 一致)
STATE_BYTES = 16  # secrets.token_urlsafe(16) = 22 字符 base64url

# === Token 缓存(本机 Fernet 加密) ===
TOKEN_KEY_PATH = SKILL_DIR / ".token_key"  # Fernet key,权限 600
TOKEN_CACHE_PATH = SKILL_DIR / ".token_cache.enc"  # Fernet 加密的 JSON
TOKEN_KEY_TTL_S = 30 * 24 * 3600  # 30 天(Fernet key 与加密缓存文件的保留周期)

# === Swagger 缓存(tool 列表本地化,减少 RPC) ===
SWAGGER_META_PATH = SKILL_DIR / ".swagger_meta.enc"
SWAGGER_META_TTL_S = 30 * 24 * 3600  # 30 天

# === Token 寿命 / 缓存策略 ===
MCP_ACCESS_TOKEN_TTL_S = 1800  # 30 分钟(JWT exp,实际以 /token 响应 expires_in 为准)
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
