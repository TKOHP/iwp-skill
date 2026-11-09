"""周报素材筛选与组装的单元测试(方案 §7 第 1/2 项 + 2026-10-28 增量修复回归).

覆盖:日期重叠五组边界、状态过滤、服务端路径(list_my_week_tasks)、
本地回退路径、响应形状收敛(str 归一/截断识别)、详情退避重试、
单条详情失败降级、工作周不可用、分页(20/页)、CLI 用法错误。
纯离线,不依赖 MCP server:fetch 注入假服务,time.sleep 打桩。
运行: python -m unittest tests.test_material -v
"""
from __future__ import annotations

import json
import unittest
from datetime import date
from unittest import mock

import cli
from scripts import material


WEEK = {"work_week": {"id": 7, "week_name": "W44", "start_date": "2026-10-26",
                      "end_date": "2026-11-01"},
        "identity": {"user_id": 39, "real_name": "张三", "org_id": 2, "org_name": "研发部"}}


def task(tid, status, start=None, end=None, subject_id=26, actual_start=None,
         actual_end=None, title="任务"):
    return {"id": tid, "task_title": title, "status": status,
            "subject_id": subject_id,
            "planned_start_date": start, "planned_end_date": end,
            "actual_start_date": actual_start, "actual_end_date": actual_end}


class FakeServer:
    """假 MCP 服务:list_my_week_tasks / list_my_tasks / get_task_detail / list_subjects."""

    def __init__(self, week=WEEK, week_tool="missing", tasks=None,
                 details=None, subjects=None, raw_list_tasks=False,
                 list_tasks_text=None):
        self.week = week
        self.week_tool = week_tool      # list[items] | "missing" | Exception
        self.tasks = tasks or []        # 全量任务,按 PAGE_SIZE 动态分页
        self.details = details or {}    # task_id -> dict | Exception | [弹出序列]
        self.subjects = subjects or []
        self.raw_list_tasks = raw_list_tasks    # True=list_my_tasks 返回 JSON 文本
        self.list_tasks_text = list_tasks_text  # 非空=list_my_tasks 返回该原文(截断用)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool, args):
        self.calls.append((tool, dict(args)))
        if tool == "get_current_work_week":
            return self.week
        if tool == "list_my_week_tasks":
            if isinstance(self.week_tool, Exception):
                raise self.week_tool
            if self.week_tool == "missing":
                raise RuntimeError("Unknown tool: list_my_week_tasks")
            return {"draft_mode": "raw", "selected_statuses": [1, 2],
                    "items": self.week_tool}
        if tool == "list_my_tasks":
            if self.list_tasks_text is not None:
                return self.list_tasks_text
            page, size = args["page"], args["page_size"]
            chunk = self.tasks[(page - 1) * size: page * size]
            payload = {"tasks": chunk, "total": len(self.tasks),
                       "page": page, "page_size": size}
            return json.dumps(payload, ensure_ascii=False) if self.raw_list_tasks else payload
        if tool == "get_task_detail":
            detail = self.details.get(args["task_id"])
            if isinstance(detail, list):        # 弹出序列:模拟前 N 次失败
                item = detail.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            if isinstance(detail, Exception):
                raise detail
            if detail is None:
                raise RuntimeError(f"task {args['task_id']} not found")
            return detail
        if tool == "list_subjects":
            return {"list": self.subjects}
        raise AssertionError(f"unexpected tool {tool}")

    def calls_of(self, tool):
        return [a for t, a in self.calls if t == tool]


class InWeekTest(unittest.TestCase):
    WS, WE = date(2026, 10, 26), date(2026, 11, 1)

    def test_no_dates_included(self):
        self.assertTrue(material.in_week(task(1, 1), self.WS, self.WE))

    def test_end_before_week_excluded(self):
        self.assertFalse(material.in_week(
            task(1, 1, start="2026-10-01", end="2026-10-25"), self.WS, self.WE))

    def test_start_after_week_excluded(self):
        self.assertFalse(material.in_week(
            task(1, 1, start="2026-11-02", end="2026-11-30"), self.WS, self.WE))

    def test_spanning_week_included(self):
        self.assertTrue(material.in_week(
            task(1, 1, start="2026-10-01", end="2026-11-30"), self.WS, self.WE))

    def test_inside_week_included(self):
        self.assertTrue(material.in_week(
            task(1, 1, start="2026-10-27", end="2026-10-30"), self.WS, self.WE))

    def test_actual_overrides_planned(self):
        # 计划结束在周前,实际结束落在周内 → 生效日期取实际 → 入选
        self.assertTrue(material.in_week(
            task(1, 1, start="2026-10-01", end="2026-10-20",
                 actual_start="2026-10-01", actual_end="2026-10-28"),
            self.WS, self.WE))

    def test_select_filters_status(self):
        items = [task(1, 0), task(2, 1), task(3, 2), task(4, 3)]
        selected = material.select_tasks(items, self.WS, self.WE)
        self.assertEqual([t["id"] for t in selected], [2, 3])


class BuildMaterialTest(unittest.TestCase):
    def setUp(self):
        # 打桩 sleep:批间延隔与重试退避不拖慢测试
        sleep_patch = mock.patch("scripts.material.time.sleep", lambda s: None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_server_path_used_when_tool_available(self):
        items = [{"subject_id": 26, "subject_title": "课题甲",
                  "tasks": [{"id": 101, "task_title": "A", "status": 1,
                             "progress_percentage": 50.0,
                             "task_description": "A 的描述"}]}]
        server = FakeServer(week_tool=items)
        payload = material.build_material(server)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "server")
        self.assertEqual(payload["counts"], {"subjects": 1, "tasks": 1})
        group = payload["subjects"][0]
        self.assertEqual(group["subject_title"], "课题甲")     # 服务端直接带课题名
        self.assertEqual(group["tasks"][0]["status_label"], "进行中")
        self.assertEqual(payload["identity"]["user_id"], 39)  # 三元组随素材返回
        self.assertEqual(server.calls_of("list_my_tasks"), [])  # 未走本地翻页
        self.assertEqual(server.calls_of("get_task_detail"), [])  # 免 N+1

    def test_fallback_to_local_when_tool_missing(self):
        server = FakeServer(
            week_tool="missing",
            tasks=[task(101, 1, start="2026-10-27", title="A"),
                   task(102, 2, start="2026-10-28", title="B")],
            details={101: {"task_description": "A 描述"},
                     102: {"task_description": "B 描述"}},
            subjects=[{"id": 26, "subject_title": "课题甲"}],
        )
        payload = material.build_material(server)
        self.assertEqual(payload["source"], "local")
        self.assertTrue(payload["warnings"][0].startswith("list_my_week_tasks 不可用"))
        self.assertEqual(payload["counts"], {"subjects": 1, "tasks": 2})
        paged = server.calls_of("list_my_tasks")
        self.assertTrue(all(a["page_size"] == material.PAGE_SIZE for a in paged))
        self.assertEqual(payload["subjects"][0]["subject_title"], "课题甲")

    def test_list_tasks_json_text_normalized(self):
        # 现场 bug 复现回归:服务端返回 JSON 文本(str)→ 归一后正常消费
        server = FakeServer(
            tasks=[task(101, 1, start="2026-10-27")],
            details={101: {"task_description": "ok"}},
            raw_list_tasks=True,
        )
        payload = material.build_material(server)
        self.assertEqual(payload["source"], "local")
        self.assertEqual(payload["counts"]["tasks"], 1)

    def test_truncated_response_raises_precise_error(self):
        text = json.dumps({"tasks": [task(1, 1)], "total": 99}, ensure_ascii=False)[:-40]
        text += "\n\n[...数据过大,请使用分页参数缩小范围]"
        server = FakeServer(list_tasks_text=text)
        with self.assertRaises(ValueError) as ctx:
            material.build_material(server)
        self.assertIn("截断", str(ctx.exception))

    def test_detail_retry_then_success(self):
        detail_calls = []

        class CountingServer(FakeServer):
            def __call__(self, tool, args):
                if tool == "get_task_detail":
                    detail_calls.append(args["task_id"])
                return super().__call__(tool, args)

        server = CountingServer(
            tasks=[task(101, 1, start="2026-10-27")],
            details={101: [RuntimeError("503"), RuntimeError("503"),
                           {"task_description": "ok"}]},
        )
        payload = material.build_material(server)
        self.assertEqual(payload["counts"]["tasks"], 1)
        self.assertEqual(detail_calls, [101, 101, 101])  # 失败 2 次后第 3 次成功
        self.assertNotIn("detail_error", payload["subjects"][0]["tasks"][0])

    def test_detail_failure_degrades_not_blocks(self):
        server = FakeServer(
            tasks=[task(101, 1, start="2026-10-27"),
                   task(102, 1, start="2026-10-27")],
            details={101: RuntimeError("503"), 102: {"task_description": "ok"}},
        )
        payload = material.build_material(server)
        tasks = {t["id"]: t for t in payload["subjects"][0]["tasks"]}
        self.assertIn("detail_error", tasks[101])      # 失败降级,投影字段保留
        self.assertNotIn("detail_error", tasks[102])   # 其余不受阻塞
        self.assertEqual(len(payload["warnings"]), 2)  # 回退原因 + 详情失败

    def test_week_unavailable_returns_empty_with_hint(self):
        server = FakeServer(week={"work_week": None, "identity": None})
        payload = material.build_material(server)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["source"], "none")
        self.assertEqual(payload["counts"]["tasks"], 0)
        self.assertIn("Web 端", payload["hint"])

    def test_pagination_merge_at_page_size_20(self):
        server = FakeServer(
            tasks=[task(i, 2, start="2026-10-28") for i in range(45)],
            details={},
        )
        payload = material.build_material(server)
        self.assertEqual(payload["counts"]["tasks"], 45)
        paged = server.calls_of("list_my_tasks")
        self.assertEqual([a["page"] for a in paged], [1, 2, 3])  # 20+20+5

    def test_cancelled_and_out_of_week_tasks_excluded(self):
        server = FakeServer(
            tasks=[task(1, 3, start="2026-10-27"),                    # 已取消
                   task(2, 1, start="2026-12-01"),                    # 开始晚于周结束
                   task(3, 1, end="2026-10-20", start="2026-10-01")],  # 结束早于周开始
        )
        payload = material.build_material(server)
        self.assertEqual(payload["source"], "local")
        self.assertEqual(payload["counts"]["tasks"], 0)


class NormalizeResultTest(unittest.TestCase):
    def test_dict_passthrough(self):
        self.assertEqual(material.normalize_result({"a": 1}), {"a": 1})

    def test_json_text_normalized(self):
        self.assertEqual(material.normalize_result('{"a": 1}'), {"a": 1})

    def test_non_object_rejected(self):
        with self.assertRaises(ValueError):
            material.normalize_result(42)


class CliUsageTest(unittest.TestCase):
    def test_week_bad_value_returns_usage_error(self):
        self.assertEqual(cli.main(["reports", "material", "--week", "abc"]), 2)

    def test_mutually_exclusive_args_untouched(self):
        self.assertEqual(cli.main(["call", "x", "--args", "{}", "--args-file", "a"]), 2)


if __name__ == "__main__":
    unittest.main()
