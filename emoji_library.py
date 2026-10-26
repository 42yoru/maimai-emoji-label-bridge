"""表情库记录与本地匹配（精确 / 标签重叠）。

向量兜底在 :mod:`vector_index` 里，这里只负责不依赖网络的快速档位。
设计目标是「一条假标签消息最多花几毫秒」：先做精确匹配，再做标签覆盖度
打分，只有前几名候选才用 ``difflib`` 补一次序列相似度。
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .label_text import normalize_tag_text, split_tags

STAGE_EXACT = "exact"
STAGE_LOCAL = "local"
STAGE_VECTOR = "vector"

# 参与 difflib 复算的本地候选数量；只对第一名附近做精细比较，避免 589 条全算。
_REFINE_TOP_N = 8


@dataclass(frozen=True)
class EmojiRecord:
    """一条可发送的表情包记录。"""

    emoji_hash: str
    description: str
    tags: Tuple[str, ...]
    search_text: str


@dataclass(frozen=True)
class MatchResult:
    """一次匹配的结果。"""

    record: EmojiRecord
    score: float
    stage: str


# VLM 偶尔会把渲染串写回描述（例如以 ``[表情包：`` 开头），或者带上特殊 token；
# 这类描述既不能用于展示，也不该参与相似度比较。
_ANOMALOUS_DESCRIPTION_PREFIXES = ("[", "【")
_ANOMALOUS_DESCRIPTION_MARKERS = ("<|", "begin_of_box", "end_of_box")


def _is_anomalous_description(description: str) -> bool:
    """判断描述是否是模型回吐出来的脏数据。"""

    normalized = description.strip()
    if not normalized:
        return True
    if normalized.startswith(_ANOMALOUS_DESCRIPTION_PREFIXES):
        return True
    return any(marker in normalized for marker in _ANOMALOUS_DESCRIPTION_MARKERS)


def build_records(rows: Iterable[Dict[str, Any]]) -> List[EmojiRecord]:
    """把数据库行转换成表情库记录。

    跳过缺少 hash、描述为空、描述异常，以及文件已丢失（``no_file_flag``）的记录。
    """

    records: List[EmojiRecord] = []
    for row in rows:
        emoji_hash = str(row.get("image_hash") or "").strip()
        description = str(row.get("description") or "").strip()
        if not emoji_hash or not description:
            continue
        if bool(row.get("no_file_flag")):
            continue
        if _is_anomalous_description(description):
            continue
        tags = split_tags(description)
        if not tags:
            continue
        records.append(
            EmojiRecord(
                emoji_hash=emoji_hash,
                description=description,
                tags=tags,
                search_text=normalize_tag_text(description),
            )
        )
    return records


def has_char_overlap(label: str, description: str, min_grams: int = 1) -> bool:
    """判断标签与描述是否存在足够的字符二元组重叠。

    向量分数在中文短标签上区分度有限，用它做硬门槛可以挡掉
    「语义分数很高但字面毫无关系」的胡配。
    """

    if min_grams <= 0:
        return True
    label_text = normalize_tag_text(label)
    record_text = normalize_tag_text(description)
    if min(len(label_text), len(record_text)) < 2:
        # 单字切不出二元组，退化成字符集合比较，否则单字标签永远过不了这道门槛
        return bool(set(label_text) & set(record_text))
    return len(_char_grams(label_text) & _char_grams(record_text)) >= min_grams


def _coverage_score(label_tags: Sequence[str], record_tags: Sequence[str]) -> float:
    """计算标签覆盖度：命中比例为主，双向包含为辅。"""

    if not label_tags or not record_tags:
        return 0.0
    label_set = set(label_tags)
    record_set = set(record_tags)
    overlap = len(label_set & record_set)
    if not overlap:
        return 0.0
    coverage = overlap / len(label_set)
    containment = overlap / min(len(label_set), len(record_set))
    return 0.6 * coverage + 0.4 * containment


class EmojiLibrary:
    """内存里的表情库索引，提供精确与本地标签匹配。"""

    def __init__(self, records: Sequence[EmojiRecord]) -> None:
        self._records: Tuple[EmojiRecord, ...] = tuple(records)
        self._exact_index: Dict[str, EmojiRecord] = {}
        self._tag_index: Dict[str, EmojiRecord] = {}
        for record in self._records:
            self._exact_index.setdefault(record.search_text, record)
            for tag in record.tags:
                self._tag_index.setdefault(tag.casefold(), record)

    @property
    def records(self) -> Tuple[EmojiRecord, ...]:
        """当前索引里的全部记录。"""

        return self._records

    @property
    def size(self) -> int:
        """记录条数。"""

        return len(self._records)

    def find_by_hash(self, emoji_hash: str) -> Optional[EmojiRecord]:
        """按图片 hash 取记录；索引与库不同步时返回 ``None``。"""

        normalized = str(emoji_hash or "").strip()
        if not normalized:
            return None
        for record in self._records:
            if record.emoji_hash == normalized:
                return record
        return None

    def match_exact(self, label: str) -> Optional[MatchResult]:
        """按描述全文或单标签做精确命中。"""

        normalized = normalize_tag_text(label)
        if not normalized:
            return None
        record = self._exact_index.get(normalized)
        if record is not None:
            return MatchResult(record=record, score=1.0, stage=STAGE_EXACT)
        tags = split_tags(label)
        if len(tags) == 1:
            record = self._tag_index.get(tags[0].casefold())
            if record is not None:
                return MatchResult(record=record, score=0.95, stage=STAGE_EXACT)
        return None

    def match_local(self, label: str, threshold: float) -> Optional[MatchResult]:
        """标签覆盖度匹配，返回是否越过阈值的最佳记录。"""

        normalized = normalize_tag_text(label)
        label_tags = split_tags(label)
        if not normalized or not label_tags:
            return None

        scored: List[Tuple[float, EmojiRecord]] = []
        for record in self._records:
            score = _coverage_score(label_tags, record.tags)
            if score > 0.0:
                scored.append((score, record))
        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, best_record = scored[0]
        for _score, record in scored[:_REFINE_TOP_N]:
            ratio = SequenceMatcher(None, normalized, record.search_text).ratio()
            candidate_score = ratio * 0.9
            # 只跟当前最优比：候选按覆盖度降序排列，若跟自己的分数比，
            # 后面的低覆盖候选会凭一点点字面相似把前面更高分的记录顶掉
            if candidate_score > best_score:
                best_score, best_record = candidate_score, record
        if best_score < threshold:
            return None
        return MatchResult(record=best_record, score=best_score, stage=STAGE_LOCAL)

    def coarse_candidates(self, label: str, limit: int) -> List[EmojiRecord]:
        """用字符二元组重叠挑出少量候选，供向量兜底时少算点积。

        返回按重叠度降序排列的候选；完全没有字符重叠时返回空列表，
        由调用方决定是否退回全量比较。
        """

        if limit <= 0 or not self._records:
            return []
        label_grams = _char_grams(normalize_tag_text(label))
        if not label_grams:
            return []
        scored: List[Tuple[int, EmojiRecord]] = []
        for record in self._records:
            overlap = len(label_grams & _char_grams(record.search_text))
            if overlap:
                scored.append((overlap, record))
        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _, record in scored[:limit]]


def _char_grams(text: str, size: int = 2) -> set[str]:
    """把文本切成字符 n-gram 集合。"""

    normalized = str(text or "")
    if len(normalized) < size:
        return {normalized} if normalized else set()
    return {normalized[index : index + size] for index in range(len(normalized) - size + 1)}
