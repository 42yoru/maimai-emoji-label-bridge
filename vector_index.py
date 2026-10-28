"""表情向量索引：落盘、增量同步与余弦检索。

索引文件保存每张表情描述的单位向量（float32 + base64），因此查询时
只需要对输入标签做一次嵌入请求，其余全是本地点积。

同步策略：
- 每次同步从宿主数据库拉取当前表情清单（hash + 描述）；
- 清单里消失的表情直接移出索引；
- 新增或描述变化的表情分批补算向量，每轮只补 ``max_items_per_sync`` 条，
  剩下的下一轮继续，避免一次请求把模型接口打满；
- 模型名或维度变化时整库重建。
"""

from __future__ import annotations

from array import array
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import asyncio
import base64
import json
import logging
import math
import operator
import time

logger = logging.getLogger("plugin.emoji_label_bridge.vector_index")

EmbedTextsFn = Callable[[List[str], int], Awaitable[Tuple[List[List[float]], str]]]
EmbedTextFn = Callable[[str], Awaitable[List[float]]]

INDEX_VERSION = 1
CACHE_VERSION = 1


@dataclass(frozen=True)
class SyncStats:
    """一次同步的结果摘要。"""

    added: int = 0
    removed: int = 0
    pending: int = 0
    failed: bool = False
    ready: bool = False


@dataclass(frozen=True)
class VectorHit:
    """向量检索命中。"""

    emoji_hash: str
    score: float


def _normalize_vector(values: Sequence[float]) -> List[float]:
    """把向量归一化成单位长度；零向量原样返回。"""

    norm = math.sqrt(math.fsum(value * value for value in values))
    if norm <= 0.0:
        return list(values)
    return [value / norm for value in values]


def _pack_vector(values: Sequence[float]) -> str:
    """序列化向量：float32 小端 + base64。"""

    return base64.b64encode(array("f", values).tobytes()).decode("ascii")


def _unpack_vector(raw: str) -> Tuple[float, ...]:
    """反序列化向量。"""

    payload = base64.b64decode(raw.encode("ascii"))
    values = array("f")
    values.frombytes(payload)
    return tuple(values)


class VectorIndex:
    """表情描述向量索引。"""

    def __init__(
        self,
        index_path: Path,
        cache_path: Path,
        description_max_chars: int = 256,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._index_path = index_path
        self._cache_path = cache_path
        self._description_max_chars = max(16, description_max_chars)
        self._log = logger_ or logger
        self._items: Dict[str, Dict[str, Any]] = {}
        self._model: str = ""
        self._dim: int = 0
        self._ready: bool = False
        self._cache: "OrderedDict[str, Tuple[Optional[str], float, float]]" = OrderedDict()
        self._cache_dirty: bool = False

    # ──── 属性 ────

    @property
    def ready(self) -> bool:
        """是否至少完成过一次覆盖当前清单的同步。"""

        return self._ready

    @property
    def size(self) -> int:
        """索引里已缓存的向量条数。"""

        return len(self._items)

    @property
    def model(self) -> str:
        """索引所用的嵌入模型名。"""

        return self._model

    # ──── 落盘 ────

    def load(self) -> None:
        """读取索引与查询缓存；文件损坏或缺字段时按空索引处理。"""

        self._items, self._model, self._dim, self._ready = self._load_index_file()
        self._cache = self._load_cache_file()

    def _load_index_file(self) -> Tuple[Dict[str, Dict[str, Any]], str, int, bool]:
        """读取索引文件；损坏或缺字段时按空索引处理。"""

        if not self._index_path.exists():
            return {}, "", 0, False
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._log.warning("表情向量索引读取失败，将重建: %s", exc)
            return {}, "", 0, False
        if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
            self._quarantine_index("版本不符或顶层不是对象")
            return {}, "", 0, False
        raw_items = payload.get("items")
        if not isinstance(raw_items, dict):
            self._quarantine_index("items 字段缺失或不是对象")
            return {}, "", 0, False
        items: Dict[str, Dict[str, Any]] = {}
        for emoji_hash, item in raw_items.items():
            if not isinstance(item, dict):
                continue
            vector = item.get("v")
            if not isinstance(vector, str) or not vector:
                continue
            items[str(emoji_hash)] = item
        complete = bool(payload.get("complete")) and bool(items)
        return items, str(payload.get("model") or ""), int(payload.get("dim") or 0), complete

    def _quarantine_index(self, reason: str) -> None:
        """把损坏/版本不符的索引挪到 .bad 并留日志，别静默丢掉。"""

        self._log.warning("表情向量索引不可用（%s），已重建；旧文件备份为 %s.bad", reason, self._index_path)
        try:
            backup = self._index_path.with_suffix(self._index_path.suffix + ".bad")
            self._index_path.replace(backup)
        except OSError as exc:
            self._log.warning("备份损坏索引失败，直接重建: %s", exc)

    def _load_cache_file(self) -> "OrderedDict[str, Tuple[Optional[str], float, float]]":
        if not self._cache_path.exists():
            return OrderedDict()
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._log.warning("表情标签缓存读取失败，将忽略: %s", exc)
            return OrderedDict()
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            return OrderedDict()
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, dict):
            return OrderedDict()
        cache: "OrderedDict[str, Tuple[Optional[str], float, float]]" = OrderedDict()
        for label, entry in raw_entries.items():
            if not isinstance(entry, dict):
                continue
            # 缓存文件是本地可手改的，坏值只丢这一条，不能让整个 load 抛错
            try:
                ts = float(entry.get("ts") or 0.0)
                score = float(entry.get("score") or 0.0)
            except (TypeError, ValueError):
                self._log.warning("表情标签缓存条目损坏，已丢弃: %r", label)
                continue
            emoji_hash = entry.get("hash")
            cache[str(label)] = (str(emoji_hash) if emoji_hash else None, score, ts)
        return cache

    def _dump_index_payload(self) -> Dict[str, Any]:
        return {
            "version": INDEX_VERSION,
            "model": self._model,
            "dim": self._dim,
            "complete": self._ready,
            "items": self._items,
        }

    def _dump_cache_payload(self) -> Dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "entries": {
                label: {"hash": emoji_hash or "", "score": score, "ts": ts}
                for label, (emoji_hash, score, ts) in self._cache.items()
            },
        }

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(path)

    async def flush(self) -> None:
        """把索引与缓存写盘（阻塞 IO 放到线程里）。"""

        await asyncio.to_thread(self._write_json, self._index_path, self._dump_index_payload())
        if self._cache_dirty:
            self._cache_dirty = False
            await asyncio.to_thread(self._write_json, self._cache_path, self._dump_cache_payload())

    # ──── 同步 ────

    def _invalidate_cache(self) -> None:
        """表情库变了就清查询缓存，否则新表情对查过的标签一直不可见。"""

        if not self._cache:
            return
        self._cache.clear()
        self._cache_dirty = True

    async def sync(
        self,
        records: Sequence[Any],
        embed_texts: EmbedTextsFn,
        *,
        batch_size: int,
        max_items: int,
        max_concurrent: int,
        cache_ttl_seconds: float,
        cache_size: int,
    ) -> SyncStats:
        """按当前表情清单增量补算向量。"""

        self._prune_cache(cache_ttl_seconds, cache_size)

        target: Dict[str, str] = {}
        for record in records:
            text = str(getattr(record, "search_text", "") or "").strip()
            if not text:
                continue
            target[str(record.emoji_hash)] = text[: self._description_max_chars]

        removed = [emoji_hash for emoji_hash in self._items if emoji_hash not in target]
        for emoji_hash in removed:
            del self._items[emoji_hash]
        if removed:
            # 表情被删掉，缓存里可能还留着指向它的命中
            self._invalidate_cache()

        pending = [
            (emoji_hash, text)
            for emoji_hash, text in target.items()
            if emoji_hash not in self._items or str(self._items[emoji_hash].get("d") or "") != text
        ]
        if not pending:
            was_ready = self._ready
            self._ready = True
            # 就绪状态要立刻落盘：否则重启后得再等一轮同步才敢用向量兜底
            if removed or not was_ready or self._cache_dirty:
                await self.flush()
            return SyncStats(added=0, removed=len(removed), pending=0, ready=True)

        budget = max(0, max_items)
        failed = False
        added = 0
        while pending and budget > 0:
            chunk = pending[: min(batch_size, budget)]
            pending = pending[len(chunk) :]
            budget -= len(chunk)
            try:
                vectors, model_name = await embed_texts([text for _, text in chunk], max_concurrent)
            except Exception as exc:  # 网络或配置问题：本轮放弃，下一轮重试
                self._log.warning("表情向量补算失败，本轮放弃: %s", exc)
                failed = True
                break
            if len(vectors) != len(chunk):
                self._log.warning("嵌入结果数量不匹配：请求 %s 条，返回 %s 条", len(chunk), len(vectors))
                failed = True
                break
            dim = len(vectors[0]) if vectors else 0
            if dim <= 0:
                failed = True
                break
            if (self._model and model_name and model_name != self._model) or (self._dim and dim != self._dim):
                self._log.info("嵌入模型或维度变化（%s/%s -> %s/%s），重建表情向量索引", self._model, self._dim, model_name, dim)
                self._items.clear()
                self._cache.clear()
                self._ready = False
                self._cache_dirty = True
            self._model = model_name or self._model
            self._dim = dim
            for (emoji_hash, text), vector in zip(chunk, vectors, strict=True):
                self._items[emoji_hash] = {"d": text, "v": _pack_vector(_normalize_vector(vector))}
            added += len(chunk)

        if added:
            # 新补了向量，之前查过但没命中的标签要重新有机会命中
            self._invalidate_cache()
        if not pending and not failed:
            self._ready = True
        await self.flush()
        return SyncStats(added=added, removed=len(removed), pending=len(pending), failed=failed, ready=self._ready)

    # ──── 查询 ────

    async def query(
        self,
        label: str,
        candidates: Sequence[Any],
        embed_text: EmbedTextFn,
        *,
        threshold: float,
        timeout_ms: int,
        cache_ttl_seconds: float,
        cache_size: int,
    ) -> Optional[VectorHit]:
        """对候选表情做向量检索，返回越过阈值的最佳命中。"""

        if not self._items or not candidates:
            return None
        self._prune_cache(cache_ttl_seconds, cache_size)

        cached = self._cache.get(label)
        if cached is not None:
            emoji_hash, score, _ = cached
            self._cache.move_to_end(label)
            # 缓存里存的是"当时的最佳候选 + 分数"，命中与否用**当前**阈值判断：
            # 以前未命中会存成 (None, 0)，用户把 vector_threshold 调低后仍然取不到，
            # 得等缓存 TTL（默认 7 天）过期才生效。
            if not emoji_hash or score < threshold:
                return None
            return VectorHit(emoji_hash=emoji_hash, score=score)

        try:
            vector = await asyncio.wait_for(embed_text(label), timeout=max(0.05, timeout_ms / 1000.0))
        except asyncio.TimeoutError:
            self._log.warning("表情标签嵌入超时（%sms），本次按未命中处理: %s", timeout_ms, label)
            return None
        except Exception as exc:
            self._log.warning("表情标签嵌入失败，本次按未命中处理: %s", exc)
            return None
        if not vector:
            return None

        query_vector = _normalize_vector(vector)
        if self._dim and len(query_vector) != self._dim:
            self._log.warning(
                "查询向量维度与索引不一致（查询=%s 索引=%s），本次按未命中处理",
                len(query_vector),
                self._dim,
            )
            return None
        best_hash = ""
        best_score = -1.0
        for record in candidates:
            item = self._items.get(str(record.emoji_hash))
            if item is None:
                continue
            stored_vector = _unpack_vector(str(item.get("v") or ""))
            if len(stored_vector) != len(query_vector):
                continue
            score = math.fsum(map(operator.mul, query_vector, stored_vector))
            if score > best_score:
                best_score = score
                best_hash = str(record.emoji_hash)

        if not best_hash:
            self._remember(label, None, 0.0, cache_size)
            return None
        if best_score < threshold:
            # 分数低于阈值也把最佳候选记下来，阈值门槛在读取缓存时再判
            self._remember(label, best_hash, best_score, cache_size)
            return None
        self._remember(label, best_hash, best_score, cache_size)
        return VectorHit(emoji_hash=best_hash, score=best_score)

    def _remember(self, label: str, emoji_hash: Optional[str], score: float, cache_size: int) -> None:
        """记录查询结果（含未命中），并按容量淘汰。"""

        self._cache[label] = (emoji_hash, score, time.time())
        self._cache.move_to_end(label)
        self._cache_dirty = True
        limit = max(1, cache_size)
        while len(self._cache) > limit:
            self._cache.popitem(last=False)

    def _prune_cache(self, cache_ttl_seconds: float, cache_size: int) -> None:
        """清理过期缓存并限制容量。"""

        now = time.time()
        ttl = max(0.0, cache_ttl_seconds)
        if ttl > 0:
            expired = [label for label, (_, _, ts) in self._cache.items() if now - ts > ttl]
            for label in expired:
                del self._cache[label]
                self._cache_dirty = True
        limit = max(1, cache_size)
        while len(self._cache) > limit:
            self._cache.popitem(last=False)
            self._cache_dirty = True
