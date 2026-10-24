"""回复正文里的「[表情包: 标签]」标记解析。

只做纯文本层面的拆分与归一化，不依赖宿主能力，便于单测。
模型偶尔会把表情包渲染格式当正文写出来（例如
``[表情包: 亲亲,卖萌,害羞,撒娇,喜爱]``），这里负责把它从正文里
识别出来并切成「普通文字 / 表情标签」两类片段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

import re

TAG_KIND = "tag"
TEXT_KIND = "text"

# 支持中英文冒号、无描述的 [表情包]、以及标签里带空格的情况
_TAG_PATTERN = re.compile(r"\[\s*表情包\s*(?:[:：]\s*([^\[\]]*?))?\s*\]")
_TAG_SPLIT_PATTERN = re.compile(r"[,，、/|]+")
_WHITESPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True)
class ParsedSegment:
    """正文拆分后的一个片段。"""

    kind: str
    text: str

    @property
    def is_tag(self) -> bool:
        """是否为表情标签片段。"""

        return self.kind == TAG_KIND


def normalize_tag_text(raw: str) -> str:
    """把标签串归一化成可比较的形式。

    统一全角逗号与空白、转小写；只用于匹配，不改变实际发送内容。
    """

    normalized = str(raw or "").replace("，", ",").replace("：", ":")
    normalized = _WHITESPACE_PATTERN.sub(" ", normalized).strip()
    return normalized.casefold()


def split_tags(raw: str) -> Tuple[str, ...]:
    """按常见分隔符切分标签，去空、去重并保持原顺序。"""

    seen: List[str] = []
    for item in _TAG_SPLIT_PATTERN.split(str(raw or "")):
        tag = item.strip()
        if tag and tag not in seen:
            seen.append(tag)
    return tuple(seen)


def parse_segments(text: str) -> List[ParsedSegment]:
    """把一段正文拆成文字片段与表情标签片段。

    相邻文字合并，空片段丢弃；标签片段保留原始标签串（可能为空）。
    """

    raw_text = str(text or "")
    if not raw_text:
        return []

    segments: List[ParsedSegment] = []
    cursor = 0
    for match in _TAG_PATTERN.finditer(raw_text):
        leading = raw_text[cursor : match.start()]
        if leading:
            segments.append(ParsedSegment(kind=TEXT_KIND, text=leading))
        segments.append(ParsedSegment(kind=TAG_KIND, text=(match.group(1) or "").strip()))
        cursor = match.end()

    trailing = raw_text[cursor:]
    if trailing:
        segments.append(ParsedSegment(kind=TEXT_KIND, text=trailing))
    return segments


def has_tag(text: str) -> bool:
    """判断一段正文里是否包含表情标签。"""

    return bool(_TAG_PATTERN.search(str(text or "")))



def build_tag_text(description: str) -> str:
    """按宿主入站表情的渲染格式构造表情组件的可见文本。"""

    normalized = str(description or "").strip()
    return f"[表情包: {normalized}]" if normalized else "[表情包]"
