"""★ 多轮会话上下文与指代消解（W24 Day5 + ★ W27 Day2 Redis 化）——"那华南呢？" → "上个月华南的延迟订单有多少"。

对应《W24学习执行手册》Day5 下午 +《03》1.4 节 +《W27学习执行手册》Day2（A3/A4）：
- 会话内保存上一轮 {question, sql, tables}；新问题做指代消解（LLM 一次调用）：
  补全省份/时间/实体省略，**消解后的问题再进生成链路**（SQL 生成感知不到多轮——
  两个关注点分开，各自评测）；
- mock 双路径（手册坑"mock 测链路、real 测效果"）：
  - provider=mock → 确定性规则消解（_mock_resolve，支持区域/时间/状态替换与补插），测链路；
  - provider=real → `build_resolve_messages` + 模型池（真实消解能力，效果只算 real）；

★ W27-D2 改造（A3/A4：会话 Redis 外置）：
- **Redis 权威 + 进程内 L1 读缓存**：KEY = `nl2sql:sess:{owner}:{session_id}`，
  value = JSON [{question, sql, tables}, ...]（≤ max_turns 轮）；状态不在进程内存，
  多实例互通、重启不丢、TTL 天然淘汰（删除原 _MAX_SESSIONS / _SESSIONS.clear() 反模式）；
- **每次读写都刷新 TTL**（LRU 语义：活跃会话不过期，读/写路径都刷）；
- **L1 30s 短缓存**：高频同会话追问省一次 RTT（写穿透、读回填）；
- **并发安全**：append 用 Redis Lua 原子脚本（读-改-写一步完成，防并发覆盖）；
- **Redis 挂降级**：进程内本地存储（fail-open，resolve 仍工作）+ DEGRADED 日志事件。

用法（router 侧）：
    ctx = get_session(session_id)            # Redis 权威；每次返回新实例（状态不在实例上）
    resolved = await ctx.resolve(question, today)
    state = await data_graph.ainvoke({...})  # 用 resolved 入图
    if 查询成功: ctx.record(resolved, sql, tables)

对外接口：
    get_session(session_id) -> SessionContext
    clear_sessions() -> None                      # 清进程内 L1/降级缓存（测试隔离）
    SessionContext.record(question, sql, tables)
    SessionContext.recent() -> dict | None
    SessionContext.resolve(question, today) -> str
    build_resolve_messages(prev_question, prev_sql, question) -> list[dict]
"""

from __future__ import annotations

import json
import re
import threading
import time

from app.domains.data.prompts import DATA_BASE_DATE
from app.shared.llm import get_provider
from app.shared.obs import logger as obs_logger
from app.shared.reliability.redis_client import get_redis_client

_log = obs_logger.get_logger("data.session_ctx")

# ==================== mock 规则消解（确定性，测链路） ====================

_TIMES = ("近7天", "近30天", "上个月", "近90天")
_REGIONS = ("华东", "华北", "华南", "西南")
_STATUSES = ("已支付", "已发货", "已完成", "已取消", "草稿")

# 各实体类别的匹配模式（替换 prev 中最后一个同类实体）
_TIME_RE = r"近\d+天|上个月|近90天"
_REGION_RE = "华东|华北|华南|西南"
_STATUS_RE = "已支付|已发货|已完成|已取消|草稿"


def _replace_last(text: str, pattern: str, repl: str) -> str | None:
    """把 text 中最后一个匹配 pattern 的片段替换为 repl；无匹配返回 None。"""
    last: re.Match[str] | None = None
    for _last in re.finditer(pattern, text):
        last = _last
    if last is None:
        return None
    return text[: last.start()] + repl + text[last.end():]


def _mock_resolve(prev: str, question: str) -> str:
    """规则式指代消解（mock 链路用）：识别"那X呢 / 只看X呢 / X呢"模式并补全。"""
    # 注意：中文句末问号是全角"？"（U+FF1F），半角"?"也兼容
    m = re.match(r"^(?:那|那么)?(.+?)呢[？?]?$", question.strip())
    if not m:
        return question.strip()
    x = m.group(1).strip()
    x = re.sub(r"^(?:只看|只算|换成|改为|都改成)?", "", x).strip()

    # 1) 时间替换/补插：prev 有同类时间 → 替换；否则前置
    if x in _TIMES:
        out = _replace_last(prev, _TIME_RE, x)
        return out if out is not None else f"{x}{prev}"
    # 2) 区域替换/补插：优先换区域词；其次换"各区域"；否则前置
    if x in _REGIONS:
        out = _replace_last(prev, _REGION_RE, x)
        if out is not None:
            return out
        out = _replace_last(prev, "各区域", f"{x}区域")
        return out if out is not None else f"{x}区域{prev}"
    # 3) "那各区域呢？" → 把区域限定换成"各区域"
    if x == "各区域":
        out = _replace_last(prev, "(?:华东|华北|华南|西南)区域", "各区域")
        if out is not None:
            return out
        out = _replace_last(prev, _REGION_RE, "各区域")
        return out if out is not None else f"各区域{prev}"
    # 4) 状态替换/补插："已完成的"→"已完成"
    if x in _STATUSES or x.endswith("的"):
        sx = x.rstrip("的")
        out = _replace_last(prev, _STATUS_RE, sx)
        if out is not None:
            return out
        # 无状态 → 在第一个"订单"前补插（"订单数量"也命中同一位置）
        idx = prev.find("订单")
        if idx != -1:
            return prev[:idx] + f"{sx}的" + prev[idx:]
        return f"{sx}{prev}"
    return question.strip()


# ==================== real 消解 prompt ====================


def _clean_resolved(raw: str) -> str:
    """清洗 LLM 消解输出：去引号/代码块围栏，取首行。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:text|plain)?\s*", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    text = text.splitlines()[0].strip() if text else ""
    return text.strip("\"'「」『』")


def build_resolve_messages(
    prev_question: str, prev_sql: str, question: str
) -> list[dict[str, str]]:
    """指代消解 prompt：一次 LLM 调用，把当前问题补全为独立完整问题。

    手册 Day5 坑：指代消解是**单独一次 LLM 调用**，别塞进 SQL 生成 prompt——
    两个关注点（补全 + 生成 SQL）混在一起两头都差。
    """
    system = (
        "你是中文对话指代消解助手。用户在向一个供应链数据查询助手连续提问。\n"
        "把用户【当前问题】补全为一条独立、完整的自然语言问题：\n"
        "把省略的区域（华东/华北/华南/西南）、时间窗（近7天/近30天/上个月）、"
        "订单状态（已支付/已发货/已完成/已取消）等条件从上一轮问题中补全进来。\n"
        "硬性规则：\n"
        "1. 不得改变业务含义，不得编造上一轮未出现的信息；\n"
        "2. 上一轮 SQL 只作为上下文参考，不要在结果里输出 SQL；\n"
        "3. 只输出补全后的一条完整问题，不要任何解释。"
    )
    user = (
        f"上一轮问题：{prev_question}\n"
        f"上一轮查询 SQL：{prev_sql}\n\n"
        f"当前问题：{question}\n\n"
        "补全后的完整问题："
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ==================== 会话上下文（★ W27-D2：Redis 权威 + L1 缓存） ====================

DEFAULT_TTL_SECONDS = 30 * 60  # 1800s：会话 Redis TTL（读/写路径都刷新，活跃会话不过期）
DEFAULT_MAX_TURNS = 4

# Redis key 前缀与 L1 读缓存 TTL（30s：高频同会话追问省一次 RTT）
_KEY_PREFIX = "nl2sql:sess"
L1_TTL_SECONDS = 30.0

# Lua：原子 append + 截断 + 刷新 TTL。
# 手册坑：轮次列表读-改-写不是原子的（并发追问可能覆盖），但单人会话概率低、
# 不上分布式锁（过度设计）——用 Redis Lua 一步完成，既不丢轮次也零锁开销。
_APPEND_TURN_LUA = """
local raw = redis.call('GET', KEYS[1])
local turns = {}
if raw then turns = cjson.decode(raw) end
table.insert(turns, cjson.decode(ARGV[1]))
while #turns > tonumber(ARGV[2]) do table.remove(turns, 1) end
redis.call('SET', KEYS[1], cjson.encode(turns), 'EX', tonumber(ARGV[3]))
return cjson.encode(turns)
"""

# Lua：读 + 刷新 TTL（LRU 语义）。键不存在返回空串（与"Redis 挂返回 None"区分开）
_GET_TOUCH_LUA = """
local raw = redis.call('GET', KEYS[1])
if raw then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
  return raw
end
return ''
"""

# 模块级：L1 读缓存 + Redis 挂时的进程内降级存储（进程内存；跨实例共享走 Redis）
_L1: dict[str, tuple[list[dict], float]] = {}   # redis_key -> (turns, expire_monotonic)
_LOCAL_TURNS: dict[str, list[dict]] = {}        # redis_key -> turns（Redis 挂时降级）
_L1_LOCK = threading.Lock()
_LOCAL_LOCK = threading.Lock()


def _l1_set(key: str, turns: list[dict]) -> None:
    with _L1_LOCK:
        _L1[key] = (list(turns), time.monotonic() + L1_TTL_SECONDS)


def _l1_get(key: str) -> list[dict] | None:
    with _L1_LOCK:
        entry = _L1.get(key)
        if entry is None:
            return None
        turns, expire = entry
        if expire < time.monotonic():
            _L1.pop(key, None)
            return None
        return turns


def _degrade(op: str, session_id: str) -> None:
    """Redis 不可用 → 进程内降级（fail-open：会话在进程内继续，resolve 仍工作）。"""
    obs_logger.log_event(
        _log, "session_ctx_degraded", level="warning",
        session_id=session_id, op=op, backend="local",
    )


class SessionContext:
    """一次多轮会话的上下文：保存最近几轮 {question, sql, tables} 供指代消解。

    ★ W27-D2：状态权威在 Redis（跨实例/重启不丢），进程内仅 L1 读缓存（30s）；
    Redis 挂 → 进程内降级存储（fail-open，DEGRADED 日志）。
    """

    def __init__(
        self,
        session_id: str,
        owner: str = "",
        max_turns: int = DEFAULT_MAX_TURNS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        redis_client=None,
    ) -> None:
        self.session_id = session_id
        self.owner = owner  # user_id 维度（多租户隔离；当前调用方可不传）
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self.rc = redis_client or get_redis_client()

    @property
    def _key(self) -> str:
        return f"{_KEY_PREFIX}:{self.owner}:{self.session_id}"

    # ---- 状态 ----

    def record(self, question: str, sql: str, tables: list[str] | None = None) -> None:
        """记录一轮已消解的完整问题（作为下轮消解的上下文来源）。

        Redis 权威 + L1 写穿透；append 用 Lua 原子脚本（并发安全）；Redis 挂 → 进程内降级。
        """
        turn = {"question": question, "sql": sql, "tables": tables or []}
        key = self._key
        try:
            raw = self.rc.eval(
                _APPEND_TURN_LUA, 1, key,
                json.dumps(turn, ensure_ascii=False),
                str(self.max_turns), str(int(self.ttl_seconds)),
            )
        except Exception:  # noqa: BLE001  # Redis 抛错（如 ConnectionError）→ 同 fail-open 降级
            raw = None
        if isinstance(raw, str) and raw:
            try:
                turns = json.loads(raw)
                _l1_set(key, turns)
                return
            except (ValueError, TypeError):
                pass
        # Redis 挂 → 进程内降级（fail-open：不丢本轮，resolve 仍工作）
        _degrade("record", self.session_id)
        with _LOCAL_LOCK:
            local = _LOCAL_TURNS.get(key, [])
            local.append(turn)
            if len(local) > self.max_turns:
                del local[: len(local) - self.max_turns]
            _LOCAL_TURNS[key] = local
        _l1_set(key, local)  # 写穿透：降级也更新 L1，进程内视图一致

    def recent(self) -> dict[str, object] | None:
        """最近一轮上下文（None = 首轮）。L1 → Redis（读回填 + 刷 TTL）→ 本地降级。"""
        key = self._key
        turns = _l1_get(key)
        if turns is not None:
            return turns[-1] if turns else None
        try:
            raw = self.rc.eval(_GET_TOUCH_LUA, 1, key, str(int(self.ttl_seconds)))
        except Exception:  # noqa: BLE001  # Redis 抛错 → 同 fail-open 降级
            raw = None
        if raw == "":
            return None  # 键不存在：首轮（Redis 正常）
        if isinstance(raw, str) and raw:
            try:
                turns = json.loads(raw)
                _l1_set(key, turns)
                return turns[-1] if turns else None
            except (ValueError, TypeError):
                pass
        # Redis 挂（eval 返回 None/非 str）→ 进程内降级
        _degrade("recent", self.session_id)
        with _LOCAL_LOCK:
            local = _LOCAL_TURNS.get(key, [])
        return local[-1] if local else None

    # ---- 指代消解 ----

    async def resolve(self, question: str, today: str | None = None) -> str:
        """消解当前问题为独立完整问题（首次/无上下文 → 原样返回）。

        - provider=mock：规则消解（确定性，测链路）；
        - provider=real：`build_resolve_messages` → 模型池生成 → 清洗。
        """
        prev = self.recent()
        if prev is None:
            return question.strip()

        provider = get_provider()
        if provider.name == "mock":
            return _mock_resolve(str(prev["question"]), question)

        today = today or DATA_BASE_DATE.isoformat()  # noqa: F841  # 保留 today 供后续 prompt 版本使用
        messages = build_resolve_messages(
            str(prev["question"]), str(prev["sql"]), question
        )
        raw = await provider.generate(messages, max_tokens=256, temperature=0.0)
        return _clean_resolved(raw)


# ==================== 全局会话入口（★ W27-D2：删除进程内注册表反模式） ====================

# 原 `_MAX_SESSIONS` / `_SESSIONS` / `_SESSIONS.clear()` 整段删除——
# 状态权威在 Redis，TTL 天然淘汰，超限清空这个反模式不再需要。
# get_session 每次返回新实例（状态不在实例上，L1 为模块级共享缓存）。


def get_session(session_id: str, owner: str = "", redis_client=None) -> SessionContext:
    """按 session_id 取会话上下文（Redis 权威；每次返回新实例，L1 模块级共享）。"""
    return SessionContext(session_id, owner=owner, redis_client=redis_client)


def clear_sessions() -> None:
    """清空进程内 L1 缓存与降级存储（测试隔离）。

    Redis 里的会话数据由 TTL 自动淘汰，不主动删除（多实例环境不要互相清）。
    """
    global _L1, _LOCAL_TURNS
    with _L1_LOCK:
        _L1 = {}
    with _LOCAL_LOCK:
        _LOCAL_TURNS = {}
