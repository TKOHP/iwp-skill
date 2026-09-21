"""iwp skill - MCP tools/list 本地缓存.

减少每次启动都 RPC 拉清单的延迟;30 天 TTL。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import skill_config
from scripts import token_store

logger = logging.getLogger(__name__)


def _fernet():
    from cryptography.fernet import Fernet
    p = skill_config.TOKEN_KEY_PATH
    if not p.is_file():
        # 兜底:让 token_store 模块顺带生成
        from scripts import token_store as _ts
        _ts._read_key()
    return Fernet(p.read_bytes().strip())


def _read() -> dict[str, Any] | None:
    p = skill_config.SWAGGER_META_PATH
    if not p.is_file():
        return None
    try:
        raw = _fernet().decrypt(p.read_bytes())
        data = json.loads(raw.decode("utf-8"))
        if data.get("_expires_at", 0) < time.time():
            return None
        return data
    except Exception as exc:
        logger.warning("swagger meta 读失败: %s", exc)
        return None


def _write(tools: list[dict]) -> None:
    skill_config.ensure_skill_dir()
    payload = {
        "_tools": tools,
        "_saved_at": int(time.time()),
        "_expires_at": int(time.time()) + skill_config.SWAGGER_META_TTL_S,
        "_version": 1,
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    enc = _fernet().encrypt(raw)
    skill_config.SWAGGER_META_PATH.write_bytes(enc)
    try:
        os.chmod(skill_config.SWAGGER_META_PATH, 0o600)
    except (OSError, NotImplementedError):
        pass


def _normalize_tools(tools: Any) -> list[dict]:
    """归一化工具清单形状。

    历史 client 的 list_tools 曾返回 {"tools": [...]} 包装 dict,旧缓存中
    可能存有该形状;新 client 返回裸 list。统一输出为 list[dict]。
    """
    if isinstance(tools, dict) and isinstance(tools.get("tools"), list):
        return [t for t in tools["tools"] if isinstance(t, dict)]
    if isinstance(tools, list):
        return [t for t in tools if isinstance(t, dict)]
    return []


def load_or_refresh(client) -> list[dict]:
    """读缓存;过期或缺失则通过 client 拉取后写回。

    Args:
        client: McpClient 实例(在 with 中;调用 list_tools)
    """
    cached = _read()
    if cached:
        return _normalize_tools(cached.get("_tools", []))
    tools = _normalize_tools(client.list_tools())
    _write(tools)
    return tools


def invalidate() -> None:
    try:
        skill_config.SWAGGER_META_PATH.unlink(missing_ok=True)
    except OSError:
        pass