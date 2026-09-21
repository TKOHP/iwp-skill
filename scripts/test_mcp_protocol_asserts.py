"""mcp_protocol 纯函数单元断言(阶段四验证项 3)."""
from scripts.mcp_protocol import (
    parse_sse_messages,
    pick_response,
    pick_common_version,
    extract_supported_versions,
    method_not_recognized,
)

# 1. SSE 多事件 + 跨行 data
sse = (
    'event: message\r\n'
    'data: {"jsonrpc":"2.0","id":1,\r\n'
    'data: "result":{"ok":true}}\r\n'
    '\r\n'
    'event: message\r\n'
    'data: {"jsonrpc":"2.0","method":"ping","id":99}\r\n'
    '\r\n'
)
msgs = parse_sse_messages(sse)
assert len(msgs) == 2, msgs
resp, others = pick_response(msgs, 1)
assert resp is not None and resp["result"] == {"ok": True}, resp
assert len(others) == 1 and others[0]["method"] == "ping", others

# 2. 单行 data(旧情形兼容)
msgs2 = parse_sse_messages('data: {"jsonrpc":"2.0","id":2,"result":{}}\r\n')
assert pick_response(msgs2, 2)[0] is not None

# 3. 响应 id 不匹配 → 落入 others
msgs3 = parse_sse_messages('data: {"jsonrpc":"2.0","id":7,"result":{}}\r\n')
resp3, others3 = pick_response(msgs3, 2)
assert resp3 is None and len(others3) == 1

# 4. 解析失败事件被跳过(不抛)
msgs4 = parse_sse_messages('data: not-json\r\n\r\ndata: {"jsonrpc":"2.0","id":3,"result":{}}\r\n')
assert pick_response(msgs4, 3)[0] is not None

# 5. 协商纯函数
assert pick_common_version(["2025-03-26", "2025-11-25"]) == "2025-11-25"
assert pick_common_version(["2026-07-28", "2025-03-26"]) == "2026-07-28"
assert pick_common_version(["2024-11-05"]) is None
assert pick_common_version([]) is None
assert pick_common_version([None, 123, "2025-06-18"]) == "2025-06-18"

# 6. 错误体 supported 提取
assert extract_supported_versions({"code": -32022, "data": {"supported": ["2025-03-26"]}}) == ["2025-03-26"]
assert extract_supported_versions({"code": -32602, "message": "Unsupported protocol version",
                                  "data": {"supported": ["2024-11-05", "2025-03-26"]}}) == ["2024-11-05", "2025-03-26"]
assert extract_supported_versions({"code": -32601}) == []
assert extract_supported_versions(None) == []

# 7. method_not_recognized 判定
assert method_not_recognized({"code": -32601, "message": "Method not found"}, None) is True
assert method_not_recognized(None, 404) is True
assert method_not_recognized({"code": -32000, "message": "boom"}, 500) is False
assert method_not_recognized(None, 401) is False

print("PURE_OK")
