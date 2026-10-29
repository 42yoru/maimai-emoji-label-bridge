"""WebUI 配置页的中文展示布局。

只重组 SDK 生成的 Schema（分块、中文标签、提示、页签），**不改配置模型、
不改默认值、也不改 TOML 里的真实路径**：每个展示块的 ``name`` 始终指向原有的配置节。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class _Section:
    """一个虚拟展示块。"""

    source: str
    title: str
    description: str
    fields: Dict[str, str]
    collapsed: bool = False


_SECTIONS: Dict[str, _Section] = {
    "behavior": _Section(
        source="behavior",
        title="假标签怎么处理",
        description="匹配不到表情时怎么办，以及一条消息里最多处理几个标签。",
        fields={
            "miss_policy": "未命中时的处理方式",
            "dedupe_with_existing_emoji": "已带表情时删掉正文标签",
            "max_tags_per_message": "单条消息最多处理几个标签",
        },
    ),
    "punctuation": _Section(
        source="punctuation",
        title="句末标点（附带）",
        description=(
            "附带的小开关：只作用于**没有**假标签的普通文本，去掉结尾的句号。默认关闭。"
        ),
        fields={
            "enabled": "去掉句末标点",
            "strip_trailing_chars": "要去掉的句末字符",
            "keep_if_only": "整条只有标点就保留",
        },
        collapsed=True,
    ),
    "match_core": _Section(
        source="match",
        title="匹配与阈值",
        description="前两档的阈值，以及向量兜底的开关与相似度门槛。",
        fields={
            "local_threshold": "本地标签重叠阈值",
            "enable_vector": "启用嵌入模型兜底",
            "vector_threshold": "向量相似度阈值",
            "vector_timeout_ms": "嵌入请求超时（毫秒）",
        },
    ),
    "match_adv": _Section(
        source="match",
        title="匹配高级",
        description="控制向量兜底的触发范围与候选规模，改大改小都会影响延迟。",
        fields={
            "only_whole_message": "只在纯标签消息上走向量",
            "require_char_overlap": "向量命中还要求字面重叠",
            "candidate_limit": "粗筛候选数",
        },
        collapsed=True,
    ),
    "index_core": _Section(
        source="index",
        title="向量索引",
        description="表情向量索引的开关、所用模型与同步节奏。",
        fields={
            "enabled": "构建并维护向量索引",
            "embedding_task": "嵌入模型任务名",
            "sync_interval_seconds": "增量同步间隔（秒）",
            "startup_delay_seconds": "首次同步延迟（秒）",
        },
    ),
    "index_adv": _Section(
        source="index",
        title="索引调优",
        description="批大小、并发与缓存的细调项；默认值已按单实例调好，一般不用动。",
        fields={
            "bootstrap_interval_seconds": "补齐期的轮询间隔（秒）",
            "batch_size": "每批嵌入条数",
            "max_items_per_sync": "每轮补算上限",
            "max_concurrent": "批量嵌入并发数",
            "description_max_chars": "参与嵌入的描述长度上限",
            "cache_size": "标签缓存条数",
            "cache_ttl_seconds": "标签缓存有效期（秒）",
        },
        collapsed=True,
    ),
    "plugin_info": _Section(
        source="plugin",
        title="插件信息",
        description="用于版本识别，保持现状即可。",
        fields={"enabled": "启用插件", "config_version": "配置版本"},
        collapsed=True,
    ),
}

_TABS = {
    "basic": ("常用设置", ("behavior", "match_core", "index_core")),
    "advanced": ("高级", ("match_adv", "index_adv", "punctuation", "plugin_info")),
}

_HINTS: Dict[str, str] = {
    "punctuation.enabled": (
        "默认关闭。只作用于**没有**假标签的普通文本（带表情标签的消息不做清理），"
        "去掉的是句末标点、句中不动。"
    ),
    "punctuation.strip_trailing_chars": (
        "逐字符匹配的句末字符集合，默认「。」。想要更随意可以加「，」；"
        "别加半角「.」，会误伤 1.5、v0.1.13 这类写在句尾的写法。"
    ),
    "punctuation.keep_if_only": (
        "整条消息去掉标点后什么都不剩（比如只发了一个「。」）时保留原样，避免发出空消息。"
    ),
    "behavior.miss_policy": (
        "drop = 删掉标签、正文照常发；keep = 保留「[表情包: …]」原文；abort = 取消这条消息。"
    ),
    "behavior.dedupe_with_existing_emoji": (
        "本次消息已经带了真表情（规划器用 attach_emoji 发的）时，把正文里的标签直接删掉，避免同一句发两张图。"
    ),
    "behavior.max_tags_per_message": "超出的标签直接丢弃，避免一条消息反复等向量兜底。",
    "match.local_threshold": "0.6×标签覆盖度 + 0.4×包含度；越过即命中，不再调嵌入模型。默认 0.5。",
    "match.enable_vector": "关掉后只走精确与本地两档，完全不碰嵌入模型。",
    "match.vector_threshold": "实测相关命中在 0.71~0.87，无关内容多在 0.65 以下，默认 0.68。",
    "match.vector_timeout_ms": (
        "实测单次嵌入 0.9~3.6 秒抖动；超时按未命中处理。上限受发送层 Hook 的 5000ms 约束，不要超过它。"
    ),
    "match.only_whole_message": "开启后，标签夹在正文里时直接按未命中处理，保护文字发送速度。",
    "match.require_char_overlap": "挡掉「语义分高但字面毫无关系」的胡配。",
    "match.candidate_limit": "向量比对前用字符重叠粗筛的候选数；粗筛为空时退回全量。",
    "index.enabled": "关掉后只走精确与本地两档，不需要嵌入模型。",
    "index.embedding_task": "对应 model_config.toml 的 model_task_config 段，默认 embedding。",
    "index.sync_interval_seconds": "新收录表情后的补向量就在这一轮；默认 10 分钟。",
    "index.startup_delay_seconds": "避开启动高峰，默认 30 秒后开始首次同步。",
    "index.bootstrap_interval_seconds": "索引没补齐时每轮间隔；补齐后改按同步间隔走。",
    "index.batch_size": "每批送去嵌入的文本条数。",
    "index.max_items_per_sync": "单轮最多补算多少条，剩下的下一轮继续。",
    "index.max_concurrent": "批量嵌入的并发数，接口吃紧就调小。",
    "index.description_max_chars": "超长描述截断后再嵌入，避免浪费 token。",
    "index.cache_size": "标签查询结果缓存条数，含未命中结果。",
    "index.cache_ttl_seconds": "到期后重新查询嵌入；0 表示不过期。",
    "plugin.enabled": "关掉后发送链路完全不受影响。",
    "plugin.config_version": "由插件自己维护，不要手改。",
}

_DISABLED_FIELDS = {"plugin.config_version"}

_CHOICES: Dict[str, list[str]] = {
    "behavior.miss_policy": ["drop", "keep", "abort"],
}


def _build_field(path: str, template: Dict[str, Any], label: str, order: int) -> Dict[str, Any]:
    """在 SDK 给的字段定义上补中文标签、提示与顺序。"""

    hint = _HINTS.get(path, template.get("description") or "")
    entry = dict(template)
    entry.update(label=label, hint=hint, description=hint, order=order)
    if path in _DISABLED_FIELDS:
        entry["disabled"] = True
    if path in _CHOICES:
        entry["choices"] = list(_CHOICES[path])
    return entry


def build_config_ui_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """重组 SDK Schema：分出页签、中文标签、常用与高级分离。

    Args:
        schema: SDK ``build_config_schema()`` 生成的原始 Schema。

    Returns:
        Dict[str, Any]: 重组后的 Schema；字段默认值与类型保持原样。
    """

    source_sections: Dict[str, Any] = schema.get("sections") or {}
    sections: Dict[str, Any] = {}
    tabs: list[Dict[str, Any]] = []

    for tab_id, (tab_title, section_ids) in _TABS.items():
        tabs.append({"id": tab_id, "title": tab_title, "sections": list(section_ids)})
        for section_id in section_ids:
            spec = _SECTIONS[section_id]
            template_section = source_sections.get(spec.source)
            if template_section is None:
                continue
            template_fields = template_section.get("fields") or {}
            fields: Dict[str, Any] = {}
            for order, (field_name, label) in enumerate(spec.fields.items()):
                template = template_fields.get(field_name)
                if template is None:
                    continue
                fields[field_name] = _build_field(f"{spec.source}.{field_name}", template, label, order)
            if not fields:
                continue
            # 字典键只标识展示分块，name 才是 WebUI 读写配置的真实节名
            sections[section_id] = {
                **template_section,
                "name": spec.source,
                "title": spec.title,
                "description": spec.description,
                "collapsed": spec.collapsed,
                "order": len(sections),
                "fields": fields,
            }

    return {**schema, "sections": sections, "layout": {"type": "tabs", "tabs": tabs}}


__all__ = ["build_config_ui_schema"]
