"""表情标签桥接（emoji-label-bridge）插件入口。

模型偶尔会把「表情包渲染格式」当正文写出来，例如::

    [表情包: 亲亲,卖萌,害羞,撒娇,喜爱]

这条文本会被当成普通消息发到聊天里，既没有图片，又会被写进历史，
让下一轮更容易继续模仿。本插件在**发送层**（``send_service.after_build_message``）
把这种假标签接成真表情：

1. 精确命中：标签与某张表情的描述一致（<1ms）；
2. 本地命中：标签与表情描述标签的重叠度越过阈值（几毫秒）；
3. 向量兜底：前两档都没命中时，用嵌入模型在本地向量索引里找最相似的表情
   （只在「整条消息就是一条标签」时启用，避免拖慢正文发送）；
4. 全部未命中或超时：按 ``miss_policy`` 删除标签 / 保留原文 / 取消整条消息。

插件不改主程序，也不依赖 planner 用的是旧 ``reply`` 工具还是被动式 replyer：
发送层看到的只是出站消息，两种逻辑一视同仁。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

import asyncio
import time

from .config_ui import build_config_ui_schema
from .emoji_library import EmojiLibrary, EmojiRecord, build_records, has_char_overlap
from .label_text import (
    TAG_KIND,
    TEXT_KIND,
    build_tag_text,
    normalize_tag_text,
    parse_segments,
)
from .vector_index import SyncStats, VectorIndex

HOOK_NAME = "send_service.after_build_message"
EMOJI_INDEX_DB_MODEL = "Images"
EMOJI_INDEX_FILTERS: Dict[str, Any] = {
    "image_type": "emoji",
    "is_registered": True,
    "is_banned": False,
}
RAW_KIND = "raw"

VECTOR_TOTAL_BUDGET_MS = 4000
"""一次 Hook 调用里所有标签共用的向量兜底总预算（毫秒）。

Hook 自身超时是 5000ms，超时后宿主会丢弃整个 modified_kwargs（假标签原样发出）并计入熔断；
`match.vector_timeout_ms` 只管单次嵌入，多个标签串行时会叠加，所以必须再加一层总预算。
"""
TEXT_COMPONENT = "text"
EMOJI_COMPONENT = "emoji"
AT_COMPONENT = "at"
IMAGE_COMPONENT = "image"
REPLY_COMPONENT = "reply"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件；关掉后发送链路完全不受影响")
    config_version: str = Field(default="0.1.13", description="配置版本")


class BehaviorConfig(PluginConfigBase):
    """命中失败时的行为配置。"""

    __ui_label__ = "未命中处理"
    __ui_icon__ = "shield-question"
    __ui_order__ = 1

    miss_policy: Literal["drop", "keep", "abort"] = Field(
        default="drop",
        description="匹配不到表情时：drop=删掉标签（默认），keep=保留文字标签，abort=取消整条消息",
    )
    max_tags_per_message: int = Field(
        default=3,
        ge=1,
        le=10,
        description="单条消息最多处理几个标签，超出的直接丢弃，避免一条消息反复等兜底",
    )
    dedupe_with_existing_emoji: bool = Field(
        default=True,
        description="本次消息已经带了真表情时，把正文标签直接删除，避免同一句发两张表情",
    )


class MatchConfig(PluginConfigBase):
    """匹配档位配置。"""

    __ui_label__ = "匹配"
    __ui_icon__ = "scan-search"
    __ui_order__ = 2

    local_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="本地标签重叠度阈值；越过即命中，不再走向量兜底",
    )
    enable_vector: bool = Field(default=True, description="是否启用嵌入模型兜底")
    vector_threshold: float = Field(
        default=0.68,
        ge=0.0,
        le=1.0,
        description="向量余弦相似度阈值；低于它就当没命中（实测该模型相关命中多在 0.71~0.87）",
    )
    vector_timeout_ms: int = Field(
        default=3000,
        ge=100,
        le=4000,
        description="单次嵌入请求的硬超时；超时按未命中处理，绝不挂住发送（实测单次 0.9~3.6s）",
    )
    require_char_overlap: bool = Field(
        default=True,
        description="向量命中还要求标签与描述有字符重叠，挡掉「语义分数高但字面无关」的胡配",
    )
    only_whole_message: bool = Field(
        default=True,
        description="只在整条消息就是一条标签时才走向量兜底；正文里夹标签时直接按未命中处理，保护文字发送速度",
    )
    candidate_limit: int = Field(
        default=120,
        ge=1,
        le=600,
        description="向量比对前用字符重叠粗筛的候选数，越小越快；粗筛为空时退回全量",
    )


class IndexConfig(PluginConfigBase):
    """向量索引配置。"""

    __ui_label__ = "向量索引"
    __ui_icon__ = "database"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="是否构建并维护表情向量索引；关掉后只走精确与本地两档")
    embedding_task: str = Field(default="embedding", description="使用的模型任务名（model_config.toml 的 model_task_config 段）")
    sync_interval_seconds: int = Field(
        default=600,
        ge=60,
        le=86400,
        description="增量同步间隔（秒）：新收录的表情会在下一轮同步里补向量",
    )
    library_refresh_seconds: int = Field(
        default=60,
        ge=10,
        le=3600,
        description="表情清单刷新间隔（秒）；与向量索引无关，index.enabled=false 时也照常刷新",
    )
    startup_delay_seconds: int = Field(
        default=30,
        ge=0,
        le=600,
        description="插件加载后延迟多久开始首次同步，避开启动高峰",
    )
    bootstrap_interval_seconds: int = Field(
        default=5,
        ge=1,
        le=120,
        description="索引尚未补齐时的轮询间隔（秒）；补齐后改按 sync_interval_seconds 周期同步",
    )
    batch_size: int = Field(default=32, ge=1, le=256, description="每批嵌入的文本条数")
    max_items_per_sync: int = Field(
        default=64,
        ge=1,
        le=512,
        description="每轮同步最多补算多少条向量，剩余的下轮继续，避免打满模型接口",
    )
    max_concurrent: int = Field(default=4, ge=1, le=16, description="批量嵌入的并发数")
    description_max_chars: int = Field(
        default=256,
        ge=16,
        le=2048,
        description="参与嵌入的表情描述最大字符数，超长描述会截断",
    )
    cache_size: int = Field(default=512, ge=1, le=8192, description="标签查询结果缓存条数")
    cache_ttl_seconds: int = Field(
        default=604800,
        ge=0,
        description="标签查询缓存有效期（秒），0 表示不过期；表情库变动时缓存会随索引重建清空",
    )


class BridgeConfig(PluginConfigBase):
    """表情标签桥接插件配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    match: MatchConfig = Field(default_factory=MatchConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)


@dataclass
class _Piece:
    """待处理的正文片段。"""

    kind: str
    text: str = ""
    payload: Optional[Dict[str, Any]] = None


class EmojiLabelBridgePlugin(MaiBotPlugin):
    """把回复正文里的假表情标签接成真表情的发送层插件。"""

    config_model = BridgeConfig

    @classmethod
    def build_config_schema(cls, **plugin_info: Any) -> Dict[str, Any]:
        """只重组 WebUI 的展示结构，配置模型与 TOML 路径保持不变。"""

        return build_config_ui_schema(super().build_config_schema(**plugin_info))

    def __init__(self) -> None:
        super().__init__()
        self._library: EmojiLibrary = EmojiLibrary([])
        self._index: Optional[VectorIndex] = None
        self._sync_task: Optional[asyncio.Task[None]] = None
        self._library_task: Optional[asyncio.Task[None]] = None
        # 首次同步失败时给一条带排查方向的长提示，之后只留一行短日志
        self._sync_failure_explained = False

    # ──── 生命周期 ────

    async def on_load(self) -> None:
        """加载插件：读取索引、拉取表情清单并启动后台增量同步。"""

        await self._rebuild_runtime()
        loaded = await self._refresh_library()
        self._start_library_task()
        self._start_sync_task()
        self.ctx.logger.info(
            "表情标签桥接已加载: 表情库=%s 条, 向量=%s 条, 已就绪=%s, 向量兜底=%s, 未命中策略=%s",
            self._library.size,
            self._index.size if self._index is not None else 0,
            self._index.ready if self._index is not None else False,
            self.config.match.enable_vector,
            self.config.behavior.miss_policy,
        )
        if not loaded:
            self.ctx.logger.warning("表情库首次读取失败，插件先只做标签清理，等后台同步重试")

    async def on_unload(self) -> None:
        """卸载插件：停掉同步任务并落盘索引。"""

        await self._stop_library_task()
        await self._stop_sync_task()
        if self._index is not None:
            await self._index.flush()

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新：重建索引对象并重启同步任务。"""

        del config_data
        if scope != "self":
            return
        await self._stop_library_task()
        await self._stop_sync_task()
        # 重建索引对象前先把旧的脏缓存落盘，否则每次改配置都会白丢最近的标签缓存
        if self._index is not None:
            await self._index.flush()
        await self._rebuild_runtime()
        await self._refresh_library()
        self._start_library_task()
        self._start_sync_task()
        self.ctx.logger.info("表情标签桥接配置已更新: version=%s", version)

    # ──── 发送层 Hook ────

    @HookHandler(
        HOOK_NAME,
        name="emoji_label_bridge",
        description="把出站正文里的「[表情包: 标签]」假标签接成真表情，未命中时按配置处理。",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=5000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def bridge_after_build_message(self, **kwargs: Any) -> Dict[str, Any]:
        """检查并改写即将发送的消息。"""

        if not self.config.plugin.enabled:
            return {"action": "continue"}
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return {"action": "continue"}
        components = message.get("raw_message")
        if not isinstance(components, list) or not components:
            return {"action": "continue"}
        if not self._has_text_tag(components):
            return {"action": "continue"}

        new_components = await self._process_components(components)
        if new_components is None:
            return {"action": "continue"}
        if not new_components:
            self.ctx.logger.info("出站消息只含未命中标签，已取消发送: session_id=%s", kwargs.get("stream_id"))
            return {"action": "abort"}

        modified_kwargs = dict(kwargs)
        new_message = dict(message)
        new_message["raw_message"] = new_components
        plain_text = _render_plain_text(new_components)
        if plain_text is not None:
            # 宿主只在「引用回复」或该字段为空时重算，不主动写的话历史里会留下假标签。
            # 顶层参数也同步一份：排在后面的 blocking Hook 读到的是 kwargs 里的那份。
            new_message["processed_plain_text"] = plain_text
            modified_kwargs["processed_plain_text"] = plain_text
        new_message["is_emoji"] = _is_single_emoji(new_components)
        modified_kwargs["message"] = new_message
        return {"action": "continue", "modified_kwargs": modified_kwargs}

    @staticmethod
    def _has_text_tag(components: Sequence[Any]) -> bool:
        """快速判断是否需要进入处理逻辑（绝大多数消息在这里就返回）。"""

        for component in components:
            if not isinstance(component, dict):
                continue
            if component.get("type") != TEXT_COMPONENT:
                continue
            text = str(component.get("data") or "")
            if "表情包" in text and ("[" in text or "［" in text):
                return True
        return False

    async def _process_components(self, components: Sequence[Any]) -> Optional[List[Dict[str, Any]]]:
        """按标签解析结果重建组件列表；没有变化时返回 ``None``。"""

        pieces = self._scan_components(components)
        if not any(piece.kind == TAG_KIND for piece in pieces):
            return None

        text_piece_count = sum(1 for piece in pieces if piece.kind == TEXT_KIND and piece.text.strip())
        tag_piece_count = sum(1 for piece in pieces if piece.kind == TAG_KIND)
        has_raw_piece = any(piece.kind == RAW_KIND for piece in pieces)
        # 只有「整条消息就一个标签、没有别的组件」才允许走向量兜底
        whole_message = text_piece_count == 0 and tag_piece_count == 1 and not has_raw_piece
        has_existing_emoji = any(
            isinstance(component, dict) and component.get("type") == EMOJI_COMPONENT for component in components
        )
        settings = self.config.behavior
        miss_policy = settings.miss_policy

        result: List[Dict[str, Any]] = []
        pending_text: List[str] = []
        handled_tags = 0
        # 整轮向量预算：Hook 超时（5000ms）一到，宿主会丢弃整份 modified_kwargs 并计入熔断，
        # 所以宁可让后面的标签按未命中处理，也不能让串行的多次嵌入把 Hook 拖爆。
        deadline = time.monotonic() + VECTOR_TOTAL_BUDGET_MS / 1000

        def flush_text() -> None:
            """把累计的文字片段合成一个组件。"""

            if not pending_text:
                return
            merged = "".join(pending_text)
            pending_text.clear()
            if merged.strip():
                result.append({"type": TEXT_COMPONENT, "data": merged})

        for piece in pieces:
            if piece.kind == RAW_KIND:
                flush_text()
                if piece.payload is not None:
                    result.append(piece.payload)
                continue
            if piece.kind == TEXT_KIND:
                pending_text.append(piece.text)
                continue

            handled_tags += 1
            if handled_tags > settings.max_tags_per_message:
                continue
            if has_existing_emoji and settings.dedupe_with_existing_emoji:
                continue

            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                self.ctx.logger.warning(
                    "向量兜底总预算 %sms 已用完，余下标签按未命中处理: label=%s",
                    VECTOR_TOTAL_BUDGET_MS,
                    piece.text[:40],
                )
                record = None
            else:
                record = await self._resolve_tag(
                    piece.text,
                    whole_message=whole_message,
                    timeout_ms=remaining_ms,
                )
            if record is not None:
                flush_text()
                result.append(
                    {
                        "type": EMOJI_COMPONENT,
                        "data": build_tag_text(record.description),
                        "hash": record.emoji_hash,
                    }
                )
                self.ctx.logger.info("假表情标签已接成真表情: label=%s hash=%s", piece.text[:40], record.emoji_hash[:12])
                continue

            if miss_policy == "keep":
                pending_text.append(build_tag_text(piece.text))
            elif miss_policy == "abort":
                return []
            # drop：什么都不加
            self.ctx.logger.info("表情标签未命中，按 %s 处理: label=%s", miss_policy, piece.text[:40])

        flush_text()
        return result

    @staticmethod
    def _scan_components(components: Sequence[Any]) -> List[_Piece]:
        """把组件列表展开成文字 / 标签 / 原样保留三类片段。"""

        pieces: List[_Piece] = []
        for component in components:
            if not isinstance(component, dict):
                pieces.append(_Piece(kind=RAW_KIND, payload=component))
                continue
            if component.get("type") != TEXT_COMPONENT:
                pieces.append(_Piece(kind=RAW_KIND, payload=component))
                continue
            text = str(component.get("data") or "")
            for segment in parse_segments(text):
                if segment.is_tag:
                    pieces.append(_Piece(kind=TAG_KIND, text=segment.text))
                elif segment.text:
                    pieces.append(_Piece(kind=TEXT_KIND, text=segment.text))
        return pieces

    # ──── 匹配 ────

    async def _resolve_tag(
        self,
        label: str,
        *,
        whole_message: bool,
        timeout_ms: Optional[int] = None,
    ) -> Optional[EmojiRecord]:
        """按精确 → 本地 → 向量三档顺序解析标签。

        Args:
            label: 标签文本。
            whole_message: 整条消息是否只有一个标签（决定能否走向量兜底）。
            timeout_ms: 本次嵌入允许的时间；调用方按整轮总预算逐标签扣减后传入。
        """

        normalized = normalize_tag_text(label)
        if not normalized:
            return None

        exact = self._library.match_exact(label)
        if exact is not None:
            return exact.record

        local = self._library.match_local(label, self.config.match.local_threshold)
        if local is not None:
            return local.record

        if not self._can_use_vector(whole_message):
            return None
        index = self._index
        if index is None:
            return None
        candidates = self._library.coarse_candidates(label, self.config.match.candidate_limit)
        if not candidates:
            if self.config.match.require_char_overlap:
                # 一个字符都对不上，字面门槛必然过不了，没必要再花一次嵌入
                return None
            candidates = list(self._library.records)
        hit = await index.query(
            normalized,
            candidates,
            self._embed_text,
            threshold=self.config.match.vector_threshold,
            timeout_ms=min(timeout_ms, self.config.match.vector_timeout_ms)
            if timeout_ms is not None
            else self.config.match.vector_timeout_ms,
            cache_ttl_seconds=self.config.index.cache_ttl_seconds,
            cache_size=self.config.index.cache_size,
        )
        if hit is None:
            return None
        record = self._library.find_by_hash(hit.emoji_hash)
        if record is None:
            self.ctx.logger.warning("向量命中但表情库已无该记录，按未命中处理: hash=%s", hit.emoji_hash[:12])
            return None
        if self.config.match.require_char_overlap and not has_char_overlap(label, record.description):
            self.ctx.logger.info(
                "向量命中但字面毫无重叠，按未命中处理: label=%s score=%.3f 描述=%s",
                label[:40],
                hit.score,
                record.description[:40],
            )
            return None
        return record

    def _can_use_vector(self, whole_message: bool) -> bool:
        """判断向量兜底档位此刻是否可用。"""

        if not self.config.index.enabled or not self.config.match.enable_vector:
            return False
        if self.config.match.only_whole_message and not whole_message:
            return False
        index = self._index
        return index is not None and index.ready and index.size > 0

    # ──── 索引与嵌入 ────

    async def _rebuild_runtime(self) -> None:
        """按当前配置重建索引对象与表情库（不触发网络）。"""

        data_dir = self.ctx.paths.data_dir
        self._index = VectorIndex(
            data_dir / "emoji_vectors.json",
            data_dir / "label_cache.json",
            description_max_chars=self.config.index.description_max_chars,
            logger_=self.ctx.logger,
        )
        # 索引是 ~6MB JSON，同步解析会直接卡住事件循环；加载/热更新都在 async 上下文里，
        # 所以放进线程跑
        await asyncio.to_thread(self._index.load)

    async def _refresh_library(self) -> bool:
        """从宿主数据库重新读取表情清单。"""

        try:
            # order_by 固定下来：描述完全相同的两张表情由 SQLite 返回顺序决定谁赢，
            # 不排序的话同一标签在重启后可能换成另一张
            rows = await self.ctx.db.query(
                EMOJI_INDEX_DB_MODEL, filters=EMOJI_INDEX_FILTERS, order_by=["id"]
            )
        except Exception as exc:
            self.ctx.logger.warning("表情库读取失败: %s", exc)
            return False
        if not isinstance(rows, list):
            self.ctx.logger.warning("表情库读取返回异常: %r", rows)
            return False
        records = build_records(rows)
        if len(records) != len(rows):
            self.ctx.logger.info("表情库过滤掉 %s 条脏记录（空描述/异常描述/文件缺失）", len(rows) - len(records))
        if not records:
            # 库为空时插件仍会按 miss_policy 处理标签，这里必须让人看见，不能安静地删
            self.ctx.logger.warning(
                "表情库读到 0 条可用表情：未命中的标签会按 miss_policy 处理；"
                "请确认 images 表里有已注册表情，或把 behavior.miss_policy 改成 keep 作为退路。"
            )
        self._library = EmojiLibrary(records)
        return True

    def _start_sync_task(self) -> None:
        """启动后台增量同步任务。"""

        if not self.config.index.enabled:
            return
        self._sync_task = asyncio.create_task(self._sync_loop(), name="emoji_label_bridge_sync")

    def _start_library_task(self) -> None:
        """启动表情清单刷新任务。

        这件事必须独立于向量同步：`index.enabled=false` 是 README 推荐的「没有嵌入模型也能用」
        模式，那种模式下如果清单只读一次，新收录的表情永远匹配不到、已删除的表情还留在内存里，
        命中已删表情时宿主 `load_emoji_binary` 会抛 ValueError，整条回复发不出去。
        """

        self._library_task = asyncio.create_task(self._library_loop(), name="emoji_label_bridge_library")

    async def _stop_library_task(self) -> None:
        """停止表情清单刷新任务。"""

        task = self._library_task
        self._library_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                # 是我们自己被取消，而不是我们取消的那个任务结束，照常往上抛
                raise

    async def _library_loop(self) -> None:
        """按 ``index.library_refresh_seconds`` 周期重读表情清单（与向量索引无关）。"""

        while True:
            await asyncio.sleep(self.config.index.library_refresh_seconds)
            try:
                await self._refresh_library()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 清单长期不更新会让新表情接不上、旧表情指向已删文件，所以用 error 而不是 warning
                self.ctx.logger.error("表情清单刷新失败，沿用上一次的 %s 条: %s", self._library.size, exc)

    async def _stop_sync_task(self) -> None:
        """停止后台同步任务。"""

        task = self._sync_task
        self._sync_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise

    async def _sync_loop(self) -> None:
        """按自适应间隔补算表情向量。

        索引还没补齐时用较短的 ``bootstrap_interval_seconds`` 连续推进，
        补齐后才回到 ``sync_interval_seconds`` 的常规周期；失败则直接退回
        常规周期，避免接口异常时反复重试刷日志。
        """

        await asyncio.sleep(self.config.index.startup_delay_seconds)
        while True:
            interval = self.config.index.sync_interval_seconds
            try:
                stats = await self._sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._sync_failure_explained:
                    self._sync_failure_explained = True
                    self.ctx.logger.warning(
                        "表情向量补算失败，向量兜底这一档暂时用不了：%s。"
                        "请确认 model_config.toml 里给嵌入任务 %r 配了模型（对应 index.embedding_task），"
                        "或者把 match.enable_vector / index.enabled 关掉，只用精确和本地两档。",
                        exc,
                        self.config.index.embedding_task,
                    )
                else:
                    self.ctx.logger.warning("表情向量同步异常，将在下一轮重试: %s", exc)
            else:
                if stats is not None and not stats.failed and (not stats.ready or stats.pending):
                    interval = self.config.index.bootstrap_interval_seconds
            await asyncio.sleep(interval)

    async def _sync_once(self) -> Optional[SyncStats]:
        """执行一次「拉清单 → 删旧 → 补新」的增量同步。"""

        index = self._index
        if index is None:
            return None
        if self._library.size == 0:
            # 清单正常情况下由 _library_loop 刷新；这里是首轮读取失败后的兜底补读
            if not await self._refresh_library():
                return None
        stats = await index.sync(
            self._library.records,
            self._embed_texts,
            batch_size=self.config.index.batch_size,
            max_items=self.config.index.max_items_per_sync,
            max_concurrent=self.config.index.max_concurrent,
            cache_ttl_seconds=self.config.index.cache_ttl_seconds,
            cache_size=self.config.index.cache_size,
        )
        if stats.added or stats.removed or stats.pending or stats.failed:
            self.ctx.logger.info(
                "表情向量同步: 新增=%s 移除=%s 待补=%s 失败=%s 就绪=%s 索引=%s",
                stats.added,
                stats.removed,
                stats.pending,
                stats.failed,
                stats.ready,
                index.size,
            )
        return stats

    async def _embed_text(self, text: str) -> List[float]:
        """对单条标签做嵌入。"""

        result = await self.ctx.llm.embed(text=text, task_name=self.config.index.embedding_task)
        if not isinstance(result, dict) or not result.get("success"):
            error_message = result.get("error") if isinstance(result, dict) else result
            raise RuntimeError(f"嵌入请求失败: {error_message}")
        # 宿主成功时必带 embedding；缺字段只可能是契约漂移，不能降级成空向量当"未命中"处理
        raw = result.get("embedding")
        if not isinstance(raw, list) or not raw:
            raise RuntimeError(f"嵌入返回缺少 embedding 字段: keys={sorted(result)}")
        return [float(value) for value in raw]

    async def _embed_texts(self, texts: List[str], max_concurrent: int) -> Tuple[List[List[float]], str]:
        """批量嵌入，返回向量列表与模型名。"""

        result = await self.ctx.llm.embed(
            texts=texts,
            task_name=self.config.index.embedding_task,
            max_concurrent=max_concurrent,
        )
        if not isinstance(result, dict) or not result.get("success"):
            error_message = result.get("error") if isinstance(result, dict) else result
            raise RuntimeError(f"批量嵌入请求失败: {error_message}")
        raw_results = result.get("results")
        if not isinstance(raw_results, list):
            raise RuntimeError("批量嵌入返回缺少 results")
        vectors: List[List[float]] = []
        for index, item in enumerate(raw_results):
            if not isinstance(item, dict):
                raise RuntimeError(f"批量嵌入第 {index} 条结果不是对象: {type(item).__name__}")
            # 与单条路径同一口径：缺字段是硬错误，不能变成空向量让 dim<=0 静默 break
            embedding = item.get("embedding")
            if not isinstance(embedding, list) or not embedding:
                raise RuntimeError(f"批量嵌入第 {index} 条缺少 embedding: keys={sorted(item)}")
            vectors.append([float(value) for value in embedding])
        model_name = str((raw_results[0].get("model_name") if raw_results else "") or "")
        return vectors, model_name


def _render_plain_text(components: Sequence[Any]) -> Optional[str]:
    """按重建后的组件拼可见纯文本，让历史里不再留假标签。

    拼法与宿主 `send_service._build_processed_plain_text` 对齐（同样的组件类型、同样的 " "
    分隔），这样消息里夹着 at / reply / image 时也能算出正确文本 —— 以前这里遇到这几种组件
    就整体放弃，`processed_plain_text` 于是保留带假标签的原文并随 `to_db_instance` 落库，
    下一轮 prompt 继续模仿，正是插件要断掉的回路。

    真正无法识别的组件才返回 ``None``，交给宿主按它自己的规则处理。
    """

    parts: List[str] = []
    for component in components:
        if not isinstance(component, dict):
            return None
        component_type = component.get("type")
        data = component.get("data")
        if component_type in (TEXT_COMPONENT, EMOJI_COMPONENT):
            parts.append(str(data or ""))
        elif component_type == AT_COMPONENT:
            payload = data if isinstance(data, dict) else {}
            target = (
                payload.get("target_user_cardname")
                or payload.get("target_user_nickname")
                or payload.get("target_user_id")
                or ""
            )
            parts.append(f"@{target}")
        elif component_type == REPLY_COMPONENT:
            payload = data if isinstance(data, dict) else {}
            parts.append(str(payload.get("target_message_content") or "[回复消息]"))
        elif component_type == IMAGE_COMPONENT:
            # 宿主用 ImageComponent.content，取不到就给占位符；绝不要把 dict 印成字符串
            parts.append(str(data).strip() if isinstance(data, str) and data.strip() else "[图片]")
        else:
            return None
    text = " ".join(part for part in parts if part).strip()
    return text or None


def _is_single_emoji(components: Sequence[Any]) -> bool:
    """整条消息只有一个表情组件时，宿主才会把它当表情消息落库。"""

    return (
        len(components) == 1
        and isinstance(components[0], dict)
        and components[0].get("type") == EMOJI_COMPONENT
    )


def create_plugin() -> EmojiLabelBridgePlugin:
    """创建插件实例。"""

    return EmojiLabelBridgePlugin()
