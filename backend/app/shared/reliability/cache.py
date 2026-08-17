"""★ 查询缓存（W21 Day3）：Redis 优先，内存兜底（fail-open）。

用途：查单（query_order）/报表（generate_report）结果缓存——TTL 内第二次查询
不落库、不调 LLM（手册验收：缓存命中验证，第二次查询不落库/不调 LLM）。

设计（生产视角）：
- key = sha256(业务名 + 参数 JSON)（确定性、可复现）
- TTL 60s（config.REDIS_CACHE_TTL）：短 TTL 保证"最多 60s 旧"的数据
- 命中打标记：get 返回 (value, hit)——hit=True 时调用方在 meta 标注 source=cache
- fail-open：Redis 不可用 → 降级到进程内 dict（内存兜底）；内存也不可用 → miss
  （缓存透明，不影响功能——读操作永远可以回源）

接口：
    QueryCache(ttl=60, redis_client=None, use_memory=True)
    .get(*key_parts) -> (value|None, hit: bool)
    .set(value, *key_parts) -> None
    .delete(*key_parts) -> None
"""
import hashlib
import json
import time

from app.shared.reliability.redis_client import get_redis_client


class QueryCache:
    """两级查询缓存：Redis（跨实例共享）→ 进程内内存（本地兜底）。"""

    def __init__(self, ttl: int = 60, redis_client=None, use_memory: bool = True):
        from app.shared import config
        self.ttl = ttl if ttl is not None else config.REDIS_CACHE_TTL
        self.rc = redis_client or get_redis_client()
        self.use_memory = use_memory
        self._mem: dict[str, tuple[float, dict]] = {}  # key -> (expire_ts, value)

    # ---- key 生成 ----

    @staticmethod
    def build_key(*parts) -> str:
        """key = sha256(业务名 + 参数)。parts 可含 dict/list（JSON 序列化）。"""
        raw = "|".join(
            json.dumps(p, ensure_ascii=False, sort_keys=True) if not isinstance(p, str) else p
            for p in parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ---- 读写 ----

    def get(self, *key_parts) -> tuple[dict | None, bool]:
        """取缓存：返回 (value, hit)。hit=True 表示缓存命中。"""
        key = self.build_key(*key_parts)
        # 1) Redis（跨实例共享）
        raw = self.rc.get(f"cache:{key}")
        if raw is not None:
            try:
                return json.loads(raw), True
            except (ValueError, TypeError):
                pass
        # 2) 内存兜底
        if self.use_memory:
            entry = self._mem.get(key)
            if entry is not None:
                expire_ts, value = entry
                if expire_ts > time.time():
                    return value, True
                self._mem.pop(key, None)
        return None, False

    def set(self, value: dict, *key_parts) -> None:
        """写缓存（TTL 过期自动清理）。Redis 失败静默（内存兜底仍生效）。"""
        key = self.build_key(*key_parts)
        payload = json.dumps(value, ensure_ascii=False)
        self.rc.set(f"cache:{key}", payload, ex=self.ttl)
        if self.use_memory:
            self._mem[key] = (time.time() + self.ttl, value)

    def delete(self, *key_parts) -> None:
        key = self.build_key(*key_parts)
        self.rc.delete(f"cache:{key}")
        self._mem.pop(key, None)
