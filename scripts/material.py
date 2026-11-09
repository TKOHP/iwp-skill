"""周报素材命令(reports material)的筛选与组装逻辑.

方案: docs/周报智能生成与提示词约束强化-方案设计.md §4.2 + 增量修复
(docs/素材命令真实环境修复与周任务接口-增量方案.md,2026-10-28)。

两条路径,服务端优先:
  1. server 路径: MCP 工具 list_my_week_tasks(weekly-draft raw 模式)——
     周筛选口径单一来源,返回含描述的裁剪字段任务,免 N+1 详情调用;
  2. local 回退: 工具不存在(旧 server)或调用失败时,拉全量任务后本地
     按weekly-draft 同款口径筛选(执行者/未删除/状态1,2/日期重叠/无日期入选)
     并逐任务取详情丰富化。

真实环境防御(2026-10-28 现场复盘):
  - 响应形状收敛: 服务端分页返回可能是 JSON 文本(str),先归一再消费;
  - 截断识别: 文本带截断标记时抛精确错误,不再泛化为"非对象";
  - 限流退避: 详情调用失败按 2s/4s 退避重试(nginx 503 窗口实测)。

纯函数(in_week/select_tasks)可独立单测;build_material 的 fetch 参数
注入,离线可测(tests/test_material.py),不依赖 MCP server 在线。
"""
from __future__ import annotations

import json
import time
from datetime import date
from typing import Any, Callable

from scripts.client import McpAuthExpiredError
from skill_config import TASK_STATUS_LABELS

# 入选状态:进行中+已完成(待开始/已取消不进材料;固定口径不暴露参数,对齐后端默认)
SELECTED_STATUSES = {1, 2}
# 分页大小:响应安全预算内的实测值(服务端 MCP 输出上限 32KB;50/100 实测均超限)
PAGE_SIZE = 20
DETAIL_INTERVAL_S = 1.0  # 逐任务详情批间延隔(0.2s 实测触发 nginx 503)
DETAIL_RETRY_DELAYS_S = (2.0, 4.0)  # 详情失败退避重试间隔(2026-10-28 现场实测参数)
TRUNCATION_MARKER = "[...数据过大"  # 服务端截断标记(backend _shared.prepare_tool_output)
SERVER_TOOL = "list_my_week_tasks"  # 服务端周任务工具(weekly-draft raw 模式)

Fetcher = Callable[[str, dict[str, Any]], Any]


# === 形状收敛(响应边界统一入口) ===

def normalize_result(result: Any) -> dict[str, Any]:
    """响应形状收敛:dict 原样;JSON 文本归一为 dict;截断文本抛精确错误。

    服务端对超 32KB 的响应会硬截断并追加 TRUNCATION_MARKER,客户端
    json.loads 失败后原样透出 str——此处是全部消费方的统一防御层。
    """
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        text = result.strip()
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            if TRUNCATION_MARKER in text:
                raise ValueError(
                    "响应被服务端截断(MCP 输出上限 32KB):"
                    "数据量超出单次返回预算,请减小范围后重试"
                ) from exc
            raise ValueError(f"响应为非 JSON 文本: {text[:80]!r}") from exc
        if isinstance(loaded, dict):
            return loaded
    raise ValueError(f"返回非对象: {type(result).__name__}")


# === 纯函数:筛选判据(local 回退路径) ===

def _as_date(value: Any) -> date | None:
    """'YYYY-MM-DD'(可带时间尾巴)/date → date;None/非法值返回 None。"""
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def effective_dates(task: dict[str, Any]) -> tuple[date | None, date | None]:
    """生效起止:实际日期缺省回退计划日期(对齐后端 should_exclude_task)。"""
    start = _as_date(task.get("actual_start_date")) or _as_date(task.get("planned_start_date"))
    end = _as_date(task.get("actual_end_date")) or _as_date(task.get("planned_end_date"))
    return start, end


def in_week(task: dict[str, Any], week_start: date, week_end: date) -> bool:
    """日期与工作周重叠即入选;起止全缺的任务入选(方案 §4.2 规则 4/5)。"""
    start, end = effective_dates(task)
    if end is not None and end < week_start:
        return False
    if start is not None and start > week_end:
        return False
    return True


def select_tasks(items: list[dict[str, Any]], week_start: date,
                 week_end: date) -> list[dict[str, Any]]:
    """status ∈ {1,2} 且日期与工作周重叠;保持接口原排序。"""
    return [
        item for item in items
        if isinstance(item, dict)
        and item.get("status") in SELECTED_STATUSES
        and in_week(item, week_start, week_end)
    ]


# === 编排 ===

def _extract_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容 server 分页键名漂移(tasks/items/list/records)。"""
    for key in ("tasks", "items", "list", "records"):
        if isinstance(result.get(key), list):
            return [x for x in result[key] if isinstance(x, dict)]
    return []


def _with_labels(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """任务状态中文标签(呈现层翻译,口径见 domain-model.md §2)。"""
    for task in tasks:
        label = TASK_STATUS_LABELS.get(task.get("status"))
        if label:
            task["status_label"] = label
    return tasks


def _try_week_tasks_tool(fetch: Fetcher, week_id: int) -> tuple[list[dict[str, Any]] | None, Exception | None]:
    """服务端路径:list_my_week_tasks(周筛选口径单一来源,免 N+1)。

    Returns:
        (subjects, error):成功返回 (subjects, None);工具不存在或调用失败
        返回 (None, 原始异常)——由调用方回退 local 路径并把异常记入 warnings,
        降级透明。凭证过期(McpAuthExpiredError)不回退,向上传播触发重授权。
    """
    try:
        result = normalize_result(fetch(SERVER_TOOL, {"work_week_id": week_id}))
    except McpAuthExpiredError:
        raise
    except Exception as exc:  # noqa: BLE001 - 回退是既定降级语义
        return None, exc
    subjects: list[dict[str, Any]] = []
    for group in _extract_items(result):
        tasks = group.get("tasks") if isinstance(group, dict) else None
        if not isinstance(tasks, list):
            continue
        subjects.append({
            "subject_id": group.get("subject_id"),
            "subject_title": group.get("subject_title"),
            "tasks": _with_labels([t for t in tasks if isinstance(t, dict)]),
        })
    if not subjects:
        return None, ValueError(f"{SERVER_TOOL} 返回无可消费的 items 结构")
    return subjects, None


def _iter_paged_tasks(fetch: Fetcher, tool: str = "list_my_tasks") -> list[dict[str, Any]]:
    """翻页拉全量任务(自动翻页语义同 cli tasks list --all)。"""
    items: list[dict[str, Any]] = []
    page = 1
    total: int | None = None
    while True:
        result = normalize_result(fetch(tool, {"page": page, "page_size": PAGE_SIZE}))
        batch = _extract_items(result)
        items.extend(batch)
        raw_total = result.get("total")
        if isinstance(raw_total, int):
            total = raw_total
        if not batch:
            break
        if total is not None and len(items) >= total:
            break
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return items


def _subject_titles(fetch: Fetcher, groups: dict[Any, dict[str, Any]]) -> dict[Any, str]:
    """课题名映射:优先取详情返回中的 subject_title;缺失经 list_subjects 补齐。"""
    titles: dict[Any, str] = {}
    for group in groups.values():
        for task in group["tasks"]:
            sid = task.get("subject_id")
            title = task.get("subject_title")
            if sid is not None and isinstance(title, str) and title.strip():
                titles.setdefault(sid, title)
    missing = [sid for sid in groups if sid not in titles]
    page = 1
    while missing:
        result = normalize_result(fetch("list_subjects", {"page": page, "page_size": PAGE_SIZE}))
        batch = _extract_items(result)
        for subject in batch:
            sid = subject.get("id")
            title = subject.get("subject_title")
            if sid in groups and sid not in titles and isinstance(title, str):
                titles[sid] = title
                missing.remove(sid)
        if not batch or len(batch) < PAGE_SIZE:
            break
        page += 1
    return titles


def _fetch_detail_with_retry(fetch: Fetcher, task_id: Any) -> dict[str, Any]:
    """单任务详情,失败按 DETAIL_RETRY_DELAYS_S 退避重试(nginx 503 实测窗口)。

    凭证过期不重试(重试只会白等,且应触发上层重授权引导)。
    """
    delays = (0.0,) + DETAIL_RETRY_DELAYS_S
    last_exc: Exception | None = None
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            result = fetch("get_task_detail", {"task_id": task_id})
            break
        except McpAuthExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001 - 重试语义见模块 docstring
            last_exc = exc
    else:
        raise last_exc  # type: ignore[misc]
    return normalize_result(result)


def _material_local(fetch: Fetcher, week_start: date, week_end: date,
                    warnings: list[str]) -> list[dict[str, Any]]:
    """local 回退:全量拉取 → 本地筛选 → 逐任务详情丰富化 → 课题分组。"""
    items = _iter_paged_tasks(fetch)
    selected = select_tasks(items, week_start, week_end)

    groups: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    for idx, item in enumerate(selected):
        if idx:
            time.sleep(DETAIL_INTERVAL_S)
        task = dict(item)
        try:
            detail = _fetch_detail_with_retry(fetch, task.get("id"))
            if isinstance(detail, dict):
                task.update(detail)
        except McpAuthExpiredError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单条降级不阻塞(方案 §4.2)
            task["detail_error"] = f"{type(exc).__name__}: {exc}"
            warnings.append(f"task {task.get('id')} 详情失败: {exc}")
        label = TASK_STATUS_LABELS.get(task.get("status"))
        if label:
            task["status_label"] = label
        sid = task.get("subject_id")
        if sid not in groups:
            groups[sid] = {"subject_id": sid, "subject_title": None, "tasks": []}
            order.append(sid)
        groups[sid]["tasks"].append(task)

    titles = _subject_titles(fetch, groups)
    subjects = []
    for sid in order:
        group = groups[sid]
        group["subject_title"] = titles.get(sid)
        subjects.append(group)
    return subjects


def build_material(fetch: Fetcher, week_id: int | None = None) -> dict[str, Any]:
    """组装周报素材:工作周 → (服务端周任务 | 本地筛选回退) → 按课题分组。

    工作周不可用(无进行中工作周/指定周不存在)时返回空材料并带 hint,
    该结果不是"本周无任务",由 agent 按 hint 如实区分提示。
    """
    warnings: list[str] = []
    week_args: dict[str, Any] = {} if week_id is None else {"work_week_id": week_id}
    week_data = normalize_result(fetch("get_current_work_week", week_args) or {})
    work_week = week_data.get("work_week")
    identity = week_data.get("identity")

    week_start = _as_date(work_week.get("start_date")) if isinstance(work_week, dict) else None
    week_end = _as_date(work_week.get("end_date")) if isinstance(work_week, dict) else None
    resolved_week_id = work_week.get("id") if isinstance(work_week, dict) else None
    if week_start is None or week_end is None or resolved_week_id is None:
        return {
            "ok": True,
            "command": "reports material",
            "source": "none",
            "work_week": work_week,
            "identity": identity,
            "subjects": [],
            "counts": {"subjects": 0, "tasks": 0},
            "hint": ("工作周不可用(无进行中工作周或指定周不存在):"
                     "如实告知用户并提示到 Web 端创建工作周后重试;"
                     "不得当作本周无任务"),
        }

    # 路径一:服务端周任务工具(口径单一来源,免 N+1 详情)
    subjects, server_err = _try_week_tasks_tool(fetch, resolved_week_id)
    if subjects is not None:
        return {
            "ok": True,
            "command": "reports material",
            "source": "server",
            "work_week": work_week,
            "identity": identity,
            "subjects": subjects,
            "counts": {
                "subjects": len(subjects),
                "tasks": sum(len(g["tasks"]) for g in subjects),
            },
        }
    warnings.append(f"{SERVER_TOOL} 不可用,已回退本地筛选: {server_err}")

    # 路径二:本地筛选回退(旧 server 或服务端调用失败)
    subjects = _material_local(fetch, week_start, week_end, warnings)
    payload: dict[str, Any] = {
        "ok": True,
        "command": "reports material",
        "source": "local",
        "work_week": work_week,
        "identity": identity,
        "subjects": subjects,
        "counts": {
            "subjects": len(subjects),
            "tasks": sum(len(g["tasks"]) for g in subjects),
        },
    }
    if warnings:
        payload["warnings"] = warnings
    return payload
