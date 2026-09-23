"""iwp skill 事前检查:依赖可导入性 + .env 存在性 + MCP server 可达性探测.

用法(会话首次调工具前运行一次,详见 references/_shared/setup-flow.md):
    python scripts/preflight.py           # 基础检查:依赖 + .env 存在性
    python scripts/preflight.py --probe   # 追加探测 MCP server 元数据端点
                                          # (.env 写入后验证可达性用)

输出: stdout 为 ASCII-safe JSON,与 cli.py 输出契约一致
    基础检查全部通过 -> {"ok": true, ...} 退出码 0
    任一缺失/失败   -> {"ok": false, ...} 退出码 1

实现约束: 仅使用标准库 + skill_config(后者仅依赖 pathlib)。
缺第三方依赖(httpx/cryptography)时本脚本仍可正常运行并报告缺失项;
依赖检测用 importlib.util.find_spec(不真实导入,无副作用)。
--probe 探测 {MCP_PUBLIC_BASE_URL}/.well-known/oauth-authorization-server,
该端点无需授权,响应中的 issuer 字段可提前暴露服务端配置错位。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# skill_config 位于技能根目录(scripts/ 的上一级);直接运行本文件时
# sys.path[0] 是 scripts/,需显式补根目录才能 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import skill_config  # noqa: E402

PROBE_TIMEOUT_S = 10
WELL_KNOWN_PATH = "/.well-known/oauth-authorization-server"


def _check_deps() -> tuple[dict[str, Any], list[str]]:
    """检测 requirements.txt 声明的第三方依赖是否可导入。"""
    deps: dict[str, Any] = {}
    missing: list[str] = []
    for name in ("httpx", "cryptography"):
        spec = importlib.util.find_spec(name)
        deps[name] = {"ok": spec is not None}
        if spec is None:
            missing.append(name)
    hints: list[str] = []
    if missing:
        hints.append(
            "missing deps: " + ", ".join(missing)
            + " -> run: pip install -r requirements.txt, then rerun preflight"
        )
    return deps, hints


def _check_env() -> tuple[bool, list[str]]:
    """检测技能根目录 .env 是否存在(仅存在性,不校验内容)。"""
    present = (skill_config.SKILL_DIR / ".env").is_file()
    hints: list[str] = []
    if not present:
        hints.append(
            ".env missing -> follow references/_shared/setup-flow.md: "
            "ask user for MCP_PUBLIC_BASE_URL, create .env from .env.example"
        )
    return present, hints


def _probe() -> tuple[dict[str, Any], list[str]]:
    """探测 MCP server 元数据端点(未认证),成功判据 = HTTP 200 + issuer 字段。"""
    url = skill_config.MCP_PUBLIC_BASE_URL + WELL_KNOWN_PATH
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        issuer = body.get("issuer")
        if not issuer:
            return (
                {"ok": False, "url": url, "error": "response has no issuer field"},
                ["unexpected metadata response; check MCP server deployment"],
            )
        return (
            {"ok": True, "url": url, "issuer": issuer},
            [],
        )
    except urllib.error.HTTPError as exc:
        return (
            {"ok": False, "url": url, "error": f"HTTP {exc.code}"},
            [f"metadata endpoint returned HTTP {exc.code}; check MCP_PUBLIC_BASE_URL"],
        )
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return (
            {"ok": False, "url": url, "error": f"unreachable: {reason}"},
            [
                "MCP server unreachable; verify the server is running and "
                "MCP_PUBLIC_BASE_URL is correct (current: "
                + skill_config.MCP_PUBLIC_BASE_URL + ")"
            ],
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return (
            {"ok": False, "url": url, "error": f"non-JSON response: {exc}"},
            ["metadata endpoint returned non-JSON; check MCP_PUBLIC_BASE_URL"],
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="iwp skill 事前检查")
    parser.add_argument(
        "--probe", action="store_true",
        help="追加探测 MCP server 元数据端点(.env 写入后验证可达性)",
    )
    args = parser.parse_args()

    checks: dict[str, Any] = {
        "python": {"ok": True, "version": platform.python_version()},
    }
    hints: list[str] = []

    deps, dep_hints = _check_deps()
    checks["deps"] = deps
    hints.extend(dep_hints)

    env_present, env_hints = _check_env()
    checks["env_present"] = env_present
    hints.extend(env_hints)

    if args.probe:
        probe, probe_hints = _probe()
        checks["probe"] = probe
        hints.extend(probe_hints)

    ok = all(
        entry.get("ok", True) if isinstance(entry, dict) else bool(entry)
        for entry in checks.values()
    )
    payload: dict[str, Any] = {"ok": ok, "checks": checks}
    if hints:
        payload["hints"] = hints
    print(json.dumps(payload, ensure_ascii=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
