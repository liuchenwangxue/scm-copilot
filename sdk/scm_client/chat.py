"""chat_stream：SSE 流式解析（httpx stream + 事件分发）。

协议对齐 backend kb/chat 与 ops/chat：
- kb：progress / message / citations / data_table / done / error
- ops：progress / approval_request / message / done / error

健壮性（手册 Day5 坑）：
- `data:` 行可能多行拼接（SSE 规范允许多行 data 合成为一个事件）→ 缓冲拼接
- 空行（`\n\n`）分隔事件；流尾无空行的残留缓冲也兜底提交
- 单个 data 行 JSON 解析失败 → 跳过该事件（流不中断），不向调用方抛错
- 兼容 `event:` 命名行（忽略字段名，以 data 内 type 为准——后端统一走 data: 单字段）
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from scm_client.models import ChatEvent


def parse_sse_events(lines: Iterator[str]) -> Iterator[ChatEvent]:
    """把 SSE 行流解析为 `ChatEvent` 迭代器（生成器：惰性消费，不整读）。"""
    buffer: list[str] = []

    def _flush() -> Iterator[ChatEvent]:
        nonlocal buffer
        if not buffer:
            return
        text = "\n".join(buffer)
        buffer = []
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                yield ChatEvent.from_payload(payload)
        except json.JSONDecodeError:
            # 单个事件体非法 JSON：跳过（流不中断；调优期可加日志）
            return

    for line in lines:
        if line == "":  # 空行 = 事件结束分隔符
            yield from _flush()
        elif line.startswith("data:"):
            buffer.append(line[5:].lstrip())
        elif line.startswith("event:"):
            # 后端统一无 event: 字段；出现时忽略命名，type 以 data 内为准
            continue
        # 其余行（注释/心跳等）忽略

    # 流尾残留缓冲兜底（部分实现不发结尾空行）
    yield from _flush()
