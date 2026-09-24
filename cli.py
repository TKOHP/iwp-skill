"""iwp skill CLI 入口.

薄透传内核 + 高频场景子命令。所有协议/业务逻辑委托 scripts/ 现有函数库,
本文件只做:参数解析、命令编排、输出契约、退出码。

输出契约(防 Windows 管道转码乱码,见 docs/cli-化改造-方案设计.md):
  - stdout: 永远 ASCII-safe JSON(ensure_ascii=True),任何管道编码下无损
  - --out FILE: 以 UTF-8(ensure_ascii=False)写文件,保留中文可读,供
    agent 用读文件工具大段阅读
  - 成功 {"ok": true, ...} 退出码 0;失败 {"ok": false, "error": {...}}
    退出码 1;用法错误退出码 2
  - 绝不输出 token / code_verifier 等凭证明文

用法(cwd 建议为技能根目录;内部路径基于 SKILL_DIR,cwd 无关):
  python cli.py auth status
  python cli.py auth start
  python cli.py auth finish            # 单查一次立即返回(waiting/completed/...)
  python cli.py auth finish --wait 110 # 阻塞轮询至 110s(或后台任务中长轮询)
  python cli.py auth finish --code=CODE  # 手动注入 code(浏览器最后一跳中断恢复)
  python cli.py auth doctor            # 授权/连接聚合体检(只读)
  python cli.py tasks list --mine --all
  python cli.py call list_subjects --args '{"page": 1}'
  python cli.py tools --refresh
  python cli.py selfcheck
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

# 保证以模块方式/任意 cwd 运行时都能找到 scripts 包
SKILL_DIR = Path(__file__).resolve().parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from scripts import auth, swagger_meta, token_store  # noqa: E402
from scripts.client import McpClient, call_tool, current_user_id  # noqa: E402
import skill_config  # noqa: E402

# 任务状态渲染知识(集中一处;参考文件不再各自解释)
TASK_STATUS_LABELS = {0: "未开始", 1: "进行中", 2: "已完成", 3: "暂停"}

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


# === 输出契约 ===

def _emit(payload: dict[str, Any], out_file: str | None) -> int:
    """按输出契约写出 payload 并返回退出码。"""
    if out_file:
        path = Path(out_file)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summary = {
            "ok": payload.get("ok", True),
            "output": str(path.resolve()),
            "hint": "结果已写文件(UTF-8 中文),请用读文件工具查看",
        }
        if not payload.get("ok", True):
            summary["error"] = payload.get("error")
        print(json.dumps(summary, ensure_ascii=True))
    else:
        print(json.dumps(payload, ensure_ascii=True))
    return EXIT_OK if payload.get("ok", True) else EXIT_FAIL


def _fail(kind: str, message: str, hint: str = "", out_file: str | None = None) -> int:
    payload: dict[str, Any] = {"ok": False, "error": {"kind": kind, "message": message}}
    if hint:
        payload["error"]["hint"] = hint
    return _emit(payload, out_file)


def _classify_exception(exc: Exception) -> tuple[str, str]:
    """异常 → (kind, hint)。错误翻译与 failure-modes.md 口径一致。"""
    name = type(exc).__name__
    text = str(exc)
    if "Invalid Host header" in text:
        return "server_host_rejected", (
            "服务端 Host 白名单拒绝(421)。检查 MCP server .env 的 "
            "MCP_ALLOWED_HOSTS 是否含端口或 host:* 通配,改后需重启 server"
        )
    if "ConnectionError" in name or "ConnectError" in name or "Connection refused" in text:
        return "server_unreachable", (
            "MCP server 不可达。确认服务已启动,或运行 python cli.py selfcheck 诊断"
        )
    if "Auth" in name or "auth_expired" in text or "需要用户授权" in text:
        return "auth_expired", "运行 python cli.py auth invalidate && python cli.py auth start 重新授权"
    return "tool_error", (
        "按 failure-modes.md 处置;参数类报错先运行 python cli.py tools "
        "核对该工具 inputSchema"
    )


# === 子命令实现 ===

def cmd_call(args: argparse.Namespace) -> int:
    if args.args and args.args_file:
        print(json.dumps({"ok": False, "error": {
            "kind": "usage",
            "message": "--args 与 --args-file 只能用一个",
        }}, ensure_ascii=True))
        return EXIT_USAGE

    arguments: dict[str, Any] = {}
    if args.args_file:
        try:
            arguments = json.loads(
                Path(args.args_file).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            return _fail("usage", f"--args-file 读取/解析失败: {exc}",
                         "文件必须是 JSON 对象;中文参数建议走 --args-file", args.out)
    elif args.args:
        try:
            arguments = json.loads(args.args)
        except json.JSONDecodeError as exc:
            return _fail("usage", f"--args JSON 解析失败: {exc}",
                         "含中文/复杂结构时改用 --args-file <file.json>", args.out)
    if not isinstance(arguments, dict):
        return _fail("usage", "--args 必须是 JSON 对象", out_file=args.out)

    try:
        result = call_tool(args.tool, arguments)
    except Exception as exc:  # noqa: BLE001 - 统一错误契约
        kind, hint = _classify_exception(exc)
        return _fail(kind, f"{type(exc).__name__}: {exc}", hint, args.out)
    return _emit({"ok": True, "tool": args.tool, "result": result}, args.out)


def cmd_tools(args: argparse.Namespace) -> int:
    if args.refresh:
        swagger_meta.invalidate()
    try:
        with McpClient() as client:
            tools = swagger_meta.load_or_refresh(client)
    except Exception as exc:  # noqa: BLE001
        kind, hint = _classify_exception(exc)
        return _fail(kind, f"{type(exc).__name__}: {exc}", hint, args.out)
    return _emit({
        "ok": True,
        "count": len(tools),
        "tools": [
            {"name": t.get("name"), "description": t.get("description"),
             "inputSchema": t.get("inputSchema")}
            for t in tools
        ],
    }, args.out)


def _auth_state() -> tuple[bool, str, bool]:
    """授权态判定(含静默续期);status 与 doctor 共用的唯一实现。

    Returns:
        (authorized, detail, refreshed)
    """
    tokens = token_store.load()
    if not tokens:
        return False, "无凭证缓存", False
    if token_store.is_access_token_valid(tokens):
        return True, f"授权有效,user_id={current_user_id()}", False
    try:
        tokens = auth.auto_refresh_if_needed(tokens)
    except RuntimeError:
        return False, "静默续期失败(refresh_token 失效)", False
    if token_store.is_access_token_valid(tokens):
        return True, f"静默续期成功,user_id={current_user_id()}", True
    return False, "静默续期后仍无效", False


def cmd_auth_status(_args: argparse.Namespace) -> int:
    authorized, _detail, refreshed = _auth_state()
    payload: dict[str, Any] = {"ok": True, "authorized": authorized}
    if authorized:
        payload["user_id"] = current_user_id()
        if refreshed:
            payload["refreshed"] = True
        payload["hint"] = "授权有效,可直接调用工具"
    else:
        if not token_store.load():
            payload["reason"] = "not_configured"
            payload["hint"] = "无凭证缓存(全新安装或未配置): 先按 setup-flow 配置 .env, 再 python cli.py auth start"
        else:
            payload["reason"] = "expired_or_invalid"
            payload["hint"] = "授权已过期或失效: python cli.py auth start"
    return _emit(payload, None)


def cmd_auth_start(args: argparse.Namespace) -> int:
    if not args.force:
        tokens = token_store.load()
        if tokens and token_store.is_access_token_valid(tokens):
            return _emit({
                "ok": True,
                "already_authorized": True,
                "user_id": current_user_id(),
                "hint": "当前授权仍有效;如需重置请先 auth invalidate 或加 --force",
            }, None)
    try:
        auth.ensure_authorized(force_reauth=True)
        # 正常不应走到这里(有效 token 分支已在上方处理)
        return _emit({"ok": True, "already_authorized": True}, None)
    except RuntimeError as exc:
        next_step = SKILL_DIR / ".oauth_next_step.json"
        if next_step.is_file():
            try:
                info = json.loads(next_step.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                info = None
            if info and info.get("authorize_url"):
                return _emit({
                    "ok": True,
                    "need_user_authorization": True,
                    "authorize_url": info["authorize_url"],
                    "state": info["expected_state"],
                    "callback_pid": info.get("callback_pid"),
                    "callback_ready": info.get("callback_ready"),
                    "browser_opened": info.get("browser_opened"),
                    "timeout_s": info.get("timeout_s"),
                    "hint": (
                        "向用户展示 authorize_url(呈现方式见"
                        " references/_shared/user-interaction.md)后立即进入等待:"
                        "运行 python cli.py auth finish(宿主支持后台时用 --wait 长轮询);"
                        "推进以回调到达为准,无需用户口头确认"
                    ),
                }, None)
        return _fail("auth_flow", str(exc),
                     "检查 MCP server 是否可达(python cli.py selfcheck)", None)


def _state_check(payload: dict[str, Any], expected_state: str | None) -> str | None:
    """回调 payload 的 state 校验;不一致返回错误提示文案,一致返回 None。"""
    if expected_state and payload.get("state") != expected_state:
        return "回调 state 与授权事务不一致(按 CSRF 处理)"
    return None


def _finish_exchange(code: str, verifier: str) -> int:
    """code 换 token 落盘并输出 completed 终态(共享尾段)。"""
    try:
        token_dict = auth.finalize_authorization(code=code, verifier=verifier)
    except Exception as exc:  # noqa: BLE001
        kind, hint = _classify_exception(exc)
        return _fail(kind, f"{type(exc).__name__}: {exc}", hint)
    return _emit({
        "ok": True,
        "status": "completed",
        "authorized": True,
        "user_id": current_user_id(),
        "expires_in": token_dict.get("expires_in"),
        "expires_at": token_dict.get("expires_at"),
        "hint": "授权完成,可调用工具",
    }, None)


def _handle_callback_payload(payload: dict[str, Any], expected_state: str | None,
                             verifier: str) -> int:
    """completed 态 payload 分流:denied / 缺 code / state 校验 / 换 token。"""
    if payload.get("error"):
        return _fail("auth_denied",
                     f"用户拒绝或授权出错: {payload['error']}",
                     "按拒绝语义终止,不重试")
    code = payload.get("code")
    if not code:
        return _fail("auth_flow", f"回调结果缺少 code: {payload}")
    err = _state_check(payload, expected_state)
    if err:
        return _fail("state_mismatch", err, "重新运行 python cli.py auth start")
    return _finish_exchange(code, verifier)


def _fail_callback_dead(result: dict[str, Any]) -> int:
    """callback_dead 终态:回调服务已退出仍无回调(授权窗口已过)。"""
    return _fail(
        "callback_dead",
        f"本地回调服务已退出仍无回调(授权发起于 {result.get('elapsed_s')}s 前)",
        "授权窗口已过:主动询问用户是否重新发起授权;同意则重新运行 "
        "python cli.py auth start",
    )


def cmd_auth_finish(args: argparse.Namespace) -> int:
    next_step_file = SKILL_DIR / ".oauth_next_step.json"
    if not next_step_file.is_file():
        return _fail("auth_flow", "找不到 .oauth_next_step.json(授权事务不存在)",
                     "先运行 python cli.py auth start")
    try:
        info = json.loads(next_step_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _fail("auth_flow", f"读取 .oauth_next_step.json 失败: {exc}")

    verifier = info.get("code_verifier")
    expected_state = info.get("expected_state")
    if not verifier:
        return _fail("auth_flow", "next_step 缺少 code_verifier",
                     "重新运行 python cli.py auth start")

    # 手动注入路径(浏览器最后一跳中断的恢复;等号形式 --code=<code>)
    if args.code:
        return _finish_exchange(args.code, verifier)

    if args.wait:
        # --wait N:阻塞轮询至 N 秒;回调服务死亡提前退出,不等满时长
        deadline = time.time() + args.wait
        while time.time() < deadline:
            result = auth.inspect_callback()
            status = result.get("status")
            if status == "no_transaction":
                return _fail("auth_flow", "授权事务不存在或 next_step 损坏",
                             "重新运行 python cli.py auth start")
            if status == "completed":
                return _handle_callback_payload(
                    result.get("payload", {}), expected_state, verifier)
            if status == "callback_dead":
                return _fail_callback_dead(result)
            time.sleep(1.0)
        return _fail("callback_timeout", f"等待回调超时({args.wait}s)",
                     "确认浏览器授权是否完成;回调服务可能仍存活,可继续 --wait 或重新 auth start")

    # 默认:单查一次立即返回(方案 回声授权 §4.2;不阻塞,适配任意宿主 shell 超时)
    result = auth.inspect_callback()
    status = result.get("status")
    if status == "no_transaction":
        return _fail("auth_flow", "授权事务不存在或 next_step 损坏",
                     "重新运行 python cli.py auth start")
    if status == "completed":
        return _handle_callback_payload(
            result.get("payload", {}), expected_state, verifier)
    if status == "callback_dead":
        return _fail_callback_dead(result)
    return _emit({
        "ok": True,
        "status": "waiting",
        "elapsed_s": result.get("elapsed_s"),
        "callback_alive": True,
        "hint": ("等待浏览器授权,回调到达后自动完成;"
                 "宿主支持后台执行时用 --wait <秒> 长轮询,否则稍后再次运行本命令复查"),
    }, None)


def cmd_auth_invalidate(_args: argparse.Namespace) -> int:
    token_store.invalidate()
    return _emit({"ok": True, "invalidated": True,
                  "hint": "本地凭证已清除;重新授权运行 auth start"}, None)


def cmd_auth_doctor(_args: argparse.Namespace) -> int:
    """授权/连接聚合体检(方案 回声授权 §4.3):纯编排复用既有检查,只诊断不修复。"""
    from scripts import preflight

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 依赖 / .env / 可达性:复用 preflight 检查函数
    deps, dep_hints = preflight._check_deps()
    add("deps", all(v.get("ok") for v in deps.values()),
        "; ".join(dep_hints) or "httpx/cryptography 均可导入")
    env_present, env_hints = preflight._check_env()
    add("env_present", env_present, "; ".join(env_hints) or ".env 存在")
    probe, probe_hints = preflight._probe()
    add("probe", bool(probe.get("ok")),
        "; ".join(probe_hints) or f"issuer={probe.get('issuer', '')}")

    # 授权态:复用 _auth_state(status 同源)
    authorized, auth_detail, _refreshed = _auth_state()
    add("authorized", authorized, auth_detail)

    # 回调端口:信息项(占用可能是进行中的授权事务,不算故障)
    port = skill_config.LOCAL_CALLBACK_PORT
    if auth._port_in_use(port):
        listeners = auth._list_listeners(port)
        pids = ", ".join(str(c.pid) for c in listeners if c.pid) or "未知"
        add("callback_port", True,
            f"端口 {port} 占用中(pid={pids};可能是进行中的授权事务)")
    else:
        add("callback_port", True, f"端口 {port} 空闲")

    # 回调存活 + 双栈:仅在进行中的授权事务上下文里有意义
    next_step = SKILL_DIR / ".oauth_next_step.json"
    if next_step.is_file():
        result = auth.inspect_callback()
        status = result.get("status")
        if status == "waiting":
            add("callback_liveness", True,
                f"进行中的授权事务:回调服务存活(已等待 {result.get('elapsed_s')}s)")
            v6_ok = auth._can_connect("::1", port)
            add("dual_stack", v6_ok,
                "IPv6 [::1] 已监听" if v6_ok else
                "IPv6 [::1] 未监听(降级模式):浏览器将 localhost 解析为 ::1 时回调会失败")
        elif status == "callback_dead":
            add("callback_liveness", False,
                f"进行中的授权事务:回调服务已退出(发起于 {result.get('elapsed_s')}s 前),授权窗口已过")
            add("dual_stack", True, "无监听服务,跳过")
        else:  # completed
            add("callback_liveness", True, "授权事务已有回调结果,待 auth finish 换取 token")
            add("dual_stack", True, "无监听服务,跳过")
    else:
        add("callback_liveness", True, "无进行中的授权事务")
        add("dual_stack", True, "无进行中的授权事务,跳过")

    passed = sum(1 for c in checks if c["ok"])
    return _emit({
        "ok": all(c["ok"] for c in checks),
        "summary": f"{passed}/{len(checks)} 项通过",
        "checks": checks,
    }, None)


def _normalize_task_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容 server 分页键名漂移(tasks/items/list/records)。"""
    for key in ("tasks", "items", "list", "records"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def _with_status_labels(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in items:
        status = item.get("status")
        if isinstance(status, int) and status in TASK_STATUS_LABELS:
            item["status_label"] = TASK_STATUS_LABELS[status]
    return items


def cmd_tasks_list(args: argparse.Namespace) -> int:
    if args.all and (args.page != 1):
        print(json.dumps({"ok": False, "error": {
            "kind": "usage",
            "message": "--all 与 --page 不能同时使用",
        }}, ensure_ascii=True))
        return EXIT_USAGE

    tool = "list_my_tasks" if (args.mine or not args.subject) else "list_tasks"
    page_size = min(args.size, 100)
    items: list[dict[str, Any]] = []
    total: int | None = None
    page = 1
    try:
        while True:
            arguments: dict[str, Any] = {"page": page, "page_size": page_size}
            if args.subject:
                arguments["subject_id"] = args.subject
            if args.status:
                arguments["status"] = args.status
            result = call_tool(tool, arguments)
            if not isinstance(result, dict):
                return _fail("tool_error",
                             f"{tool} 返回非对象: {type(result).__name__}",
                             "运行 python cli.py tools 核对该工具 schema", args.out)
            batch = _normalize_task_items(result)
            items.extend(batch)
            raw_total = result.get("total")
            if isinstance(raw_total, int):
                total = raw_total
            if not args.all or not batch:
                break
            if total is not None and len(items) >= total:
                break
            if len(batch) < page_size:
                break
            page += 1
            time.sleep(0.2)
    except Exception as exc:  # noqa: BLE001
        kind, hint = _classify_exception(exc)
        return _fail(kind, f"{type(exc).__name__}: {exc}", hint, args.out)

    items = _with_status_labels(items)
    return _emit({
        "ok": True,
        "tool": tool,
        "count": len(items),
        "total": total if total is not None else len(items),
        "page": 1 if args.all else args.page,
        "page_size": page_size if args.all else args.size,
        "items": items,
    }, args.out)


def cmd_selfcheck(_args: argparse.Namespace) -> int:
    from scripts import protocol_selfcheck
    buf = io.StringIO()
    # protocol_selfcheck.main() 内部直接 parse sys.argv,会把子命令名当自己的
    # 参数 → 置空 argv 再跑;argparse 失败会 SystemExit,一并接住
    saved_argv = sys.argv
    sys.argv = [saved_argv[0]]
    try:
        with contextlib.redirect_stdout(buf):
            rc = protocol_selfcheck.main()
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = saved_argv
    return _emit({"ok": rc == 0, "exit_code": rc, "report": buf.getvalue()}, None)


# === argparse 装配 ===

def _add_out_option(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", default=None, metavar="FILE",
                   help="结果写 UTF-8 文件(保留中文);stdout 只回执路径")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python cli.py",
        description="iwp skill CLI:薄透传 + 高频场景子命令(输出为 ASCII-safe JSON)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("call", help="万能透传:调用任意 MCP 工具")
    p.add_argument("tool", help="工具名,以 tools 子命令输出的清单为准")
    p.add_argument("--args", default=None, help="JSON 对象字符串(简单 ASCII 参数可用)")
    p.add_argument("--args-file", default=None,
                   help="JSON 参数文件路径(含中文/复杂结构时用)")
    _add_out_option(p)
    p.set_defaults(func=cmd_call)

    p = sub.add_parser("tools", help="列出 MCP 工具清单(name/description/inputSchema)")
    p.add_argument("--refresh", action="store_true",
                   help="强制刷新本地 schema 缓存(默认 30 天 TTL)")
    _add_out_option(p)
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("selfcheck", help="MCP 端点连通性/协议协商自检")
    p.set_defaults(func=cmd_selfcheck)

    auth_sub = sub.add_parser("auth", help="授权管理")
    auth_pair = auth_sub.add_subparsers(dest="auth_command", required=True)

    p = auth_pair.add_parser("status", help="查看授权态")
    p.set_defaults(func=cmd_auth_status)

    p = auth_pair.add_parser("start", help="发起授权(输出 authorize_url 交 agent 向用户展示)")
    p.add_argument("--force", action="store_true",
                   help="忽略现有有效凭证,强制重走授权")
    p.set_defaults(func=cmd_auth_start)

    p = auth_pair.add_parser("finish",
                             help="检查/等待回调并换取 token(默认单查立即返回)")
    p.add_argument("--code", default=None,
                   help="手动注入 authorization code(浏览器最后一跳中断时的恢复路径;"
                        "code 常以 - 开头,必须用等号形式 --code=<code>)")
    p.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                   help="阻塞轮询秒数(默认 0=单查一次立即返回;"
                        "后台任务中建议 --wait 110)")
    p.set_defaults(func=cmd_auth_finish)

    p = auth_pair.add_parser("doctor", help="授权/连接聚合体检(只读,纯编排)")
    p.set_defaults(func=cmd_auth_doctor)

    p = auth_pair.add_parser("invalidate", help="作废本地缓存凭证")
    p.set_defaults(func=cmd_auth_invalidate)

    tasks_sub = sub.add_parser("tasks", help="课题任务高频场景")
    tasks_pair = tasks_sub.add_subparsers(dest="tasks_command", required=True)

    p = tasks_pair.add_parser("list", help="列任务(默认我的任务;--subject 查指定课题)")
    p.add_argument("--mine", action="store_true", help="我的任务(list_my_tasks)")
    p.add_argument("--subject", type=int, default=None, metavar="ID",
                   help="按课题查询(list_tasks,需 subject_id)")
    p.add_argument("--status", default=None,
                   help="状态过滤,取值以 tools 子命令输出的 schema 为准")
    p.add_argument("--all", action="store_true", help="自动翻页取全量")
    p.add_argument("--page", type=int, default=1, help="页码(默认 1)")
    p.add_argument("--size", type=int, default=50, help="每页数量(默认 50,上限 100)")
    _add_out_option(p)
    p.set_defaults(func=cmd_tasks_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
