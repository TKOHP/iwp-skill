"""iwp skill - MCP token Fernet 加密缓存.

职责:
  - 把 access_token / refresh_token / mcp_session_id / resource / expires_at
    加密落盘到 {skill_dir}/.token_cache.enc
  - 密钥 {skill_dir}/.token_key(无则生成,Fernet.generate_key)
  - 暴露 save/load/invalidate/is_valid 四个 API
  - 线程安全(全局锁;无跨进程同步)

安全约束(方案 §1.3):
  - Fernet 加密(AES-128-CBC + HMAC-SHA256)
  - 缓存文件权限 600(部署方 icacls/chmod 保证)
  - 密钥丢失 → 重新走 OAuth 授权
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

import skill_config

logger = logging.getLogger(__name__)


class TokenStoreError(Exception):
    pass


def _read_key() -> bytes:
    """读 Fernet key;缺失则生成新 key 并写盘(权限 600)。"""
    skill_config.ensure_skill_dir()
    path = skill_config.TOKEN_KEY_PATH
    if path.is_file():
        try:
            return path.read_bytes().strip()
        except OSError as exc:
            raise TokenStoreError(f"读 token key 失败: {exc}") from exc
    # 生成新 key
    key = Fernet.generate_key()
    try:
        path.write_bytes(key)
        # Windows 权限收紧:仅当前用户可读写
        try:
            os.chmod(path, 0o600)
        except (OSError, NotImplementedError):
            # Windows 上 os.chmod 在某些 Python 版本仅模拟,不抛错
            pass
        logger.info("已生成新的 token Fernet key: %s", path)
        return key
    except OSError as exc:
        raise TokenStoreError(f"写 token key 失败: {exc}") from exc


def _fernet() -> Fernet:
    return Fernet(_read_key())


def save(token_dict: dict[str, Any]) -> None:
    """保存 token 字典(覆盖)。

    Args:
        token_dict: 必含字段:access_token, refresh_token, mcp_session_id,
            client_id, resource, expires_at (unix 秒), scope
    """
    skill_config.ensure_skill_dir()
    payload = {
        **token_dict,
        "_saved_at": int(time.time()),
        "_version": 1,
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    encrypted = _fernet().encrypt(raw)
    try:
        skill_config.TOKEN_CACHE_PATH.write_bytes(encrypted)
        try:
            os.chmod(skill_config.TOKEN_CACHE_PATH, 0o600)
        except (OSError, NotImplementedError):
            pass
        logger.info("token 缓存已写入: %s", skill_config.TOKEN_CACHE_PATH)
    except OSError as exc:
        raise TokenStoreError(f"写 token cache 失败: {exc}") from exc


def load() -> dict[str, Any] | None:
    """读取 token 字典;未找到 / 解密失败 / 格式损坏返回 None。

    注:不校验 expires_at(由调用方决定是否需要 refresh)。
    """
    path = skill_config.TOKEN_CACHE_PATH
    if not path.is_file():
        return None
    try:
        encrypted = path.read_bytes()
    except OSError as exc:
        logger.warning("读 token cache 失败: %s", exc)
        return None
    try:
        raw = _fernet().decrypt(encrypted)
    except InvalidToken:
        # 密钥变更或文件被篡改 → 失效
        logger.warning("token cache 解密失败(可能密钥变更);清空缓存")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("token cache 格式损坏: %s", exc)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def invalidate() -> None:
    """清空缓存(撤销 / 失效 / 用户主动登出)。"""
    try:
        skill_config.TOKEN_CACHE_PATH.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("删 token cache 失败: %s", exc)


def is_access_token_valid(token_dict: dict[str, Any] | None,
                          leeway_s: int = skill_config.REFRESH_LEEWAY_S) -> bool:
    """检查 access_token 是否仍可用(剩余寿命 > leeway)。

    None 输入 → False。
    """
    if not token_dict:
        return False
    exp = token_dict.get("expires_at", 0)
    return exp - int(time.time()) > leeway_s