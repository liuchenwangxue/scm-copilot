"""W25 Day2 vector_cleanup 测试：孤儿向量判定 + 语义缓存过期键清理（纯逻辑）。

覆盖手册 Day2 下午：
- 孤儿向量（payload source_doc_id 已不在 docs 表 active）→ 待清理
- 语义缓存过期键：TTL 漏网（entry key 不存在）+ 版本失效标记（version 不符）
"""

import json

from app.platform.scheduler.jobs.vector_cleanup import (
    _aggregate_by_doc,
    _clean_semcache,
    find_expired_semcache_members,
    find_orphan_doc_ids,
)

# ==================== 孤儿向量判定 ====================


def test_aggregate_by_doc_counts_points():
    points = [
        {"payload": {"source_doc_id": "A", "text": "x"}},
        {"payload": {"source_doc_id": "A", "text": "y"}},
        {"payload": {"doc_id": "B"}},  # 无 source_doc_id 兜底取 doc_id
        {"payload": {}},  # 无 doc_id → 忽略
    ]
    assert _aggregate_by_doc(points) == {"A": 2, "B": 1}


def test_find_orphan_doc_ids():
    point_docs = {"A": 3, "B": 2, "C": 1}
    active = {"A"}  # B/C 已不在 docs 表 active → 孤儿
    assert find_orphan_doc_ids(point_docs, active) == ["B", "C"]


def test_find_orphan_no_orphan():
    point_docs = {"A": 3, "B": 2}
    assert find_orphan_doc_ids(point_docs, {"A", "B"}) == []


# ==================== 语义缓存过期键判定 ====================


def test_find_expired_ttl_leak_and_version():
    entries = {
        "aaa": None,  # TTL 漏网：entry key 已过期（get 返回 None）
        "bbb": {"version": "kb-v0"},  # 版本失效标记：与当前 kb-v1 不符
        "ccc": {"version": "kb-v1"},  # 正常保留
    }
    stale = find_expired_semcache_members(entries, "kb-v1")
    assert sorted(stale) == ["aaa", "bbb"]


def test_find_expired_empty():
    assert find_expired_semcache_members({}, "kb-v1") == []


# ==================== _clean_semcache（FakeRedis） ====================


class FakeRedis:
    """内存版：只实现 _clean_semcache 用到的原语（scan_keys/smembers/get/srem/delete_many）。"""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.available = True
        self.removed: list[str] = []

    def scan_keys(self, pattern: str) -> list[str]:
        # 简化 glob：只支持 * 通配后缀匹配（测试用固定模式）
        assert pattern.endswith("*:keys")
        prefix = pattern[: -len("*:keys")]
        return [k for k in self.sets if k.startswith(prefix) and k.endswith(":keys")]

    def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def srem(self, key: str, *members: str) -> int:
        s = self.sets.setdefault(key, set())
        n = len(s)
        s.difference_update(members)
        self.removed.extend(members)
        return n - len(s)

    def delete_many(self, keys: list[str]) -> int:
        for k in keys:
            self.store.pop(k, None)
        return len(keys)


def _seed(rc: FakeRedis, version: str, qh: str, entry: dict | None):
    idx = f"scm:semcache:{version}:keys"
    rc.sets.setdefault(idx, set()).add(qh)
    if entry is not None:
        rc.store[f"scm:semcache:{version}:{qh}"] = json.dumps(entry, ensure_ascii=False)


def test_clean_semcache_removes_stale_keeps_valid():
    rc = FakeRedis()
    _seed(rc, "kb-v1", "aaa", None)  # TTL 漏网
    _seed(rc, "kb-v1", "bbb", {"version": "kb-v0"})  # 版本失效
    _seed(rc, "kb-v1", "ccc", {"version": "kb-v1"})  # 正常
    _seed(rc, "kb-v0", "ddd", None)  # 旧版本前缀的过期成员

    result = _clean_semcache(rc, "kb-v1")

    assert result["removed_members"] == 3  # aaa + bbb + ddd
    assert result["removed_entries"] == 3
    # 正常条目保留
    assert rc.get("scm:semcache:kb-v1:ccc") is not None
    assert "ccc" in rc.smembers("scm:semcache:kb-v1:keys")
    # 失效条目清掉
    assert "aaa" not in rc.smembers("scm:semcache:kb-v1:keys")
    assert "bbb" not in rc.smembers("scm:semcache:kb-v1:keys")
    assert "ddd" not in rc.smembers("scm:semcache:kb-v0:keys")


def test_clean_semcache_no_keys_noop():
    rc = FakeRedis()
    result = _clean_semcache(rc, "kb-v1")
    assert result == {"removed_members": 0, "removed_entries": 0}
