# 表情标签桥接（Emoji Label Bridge）

<p align="center">
  <img src="icon.png" width="200" alt="插件图标">
</p>

<p>
  <a href="https://github.com/42yoru/maimai-emoji-label-bridge/actions/workflows/ci.yml"><img src="https://github.com/42yoru/maimai-emoji-label-bridge/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/Mai-with-u/MaiBot"><img src="https://img.shields.io/badge/MaiBot-1.3.15%2B-orange.svg" alt="MaiBot"></a>
  <a href="https://github.com/Mai-with-u/maibot-plugin-sdk"><img src="https://img.shields.io/badge/SDK-2.16.0%2B-green.svg" alt="SDK"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python">
</p>

把回复正文里**假的表情包标签**接成真表情。

> **先回答三个常见问题**
>
> - **它解决什么？** 模型偶尔会把表情包的渲染格式当正文写出来（`[表情包: 害羞,撒娇]`），对方收到的是一串字面文字，而且这条记录还会留在历史里，让下一轮更容易继续模仿。插件在发送前把这种标签换成真表情。
> - **会不会把正常文字改坏？** 只有 `[表情包: …]` 这一种标记会被处理，其余文字原样保留；匹配不到时按配置删掉标签或保留原文，不会凭空塞图。
> - **要改主程序吗？** 不用。纯插件，用的是宿主已有的发送层 hook 与 SDK 能力。

## 先看效果

她的回复里出现了这样一条：

```
她: 好呀，那就这么说定了
她: [表情包: 害羞,撒娇,亲亲]   ← 假装出来的标签，只是一串文字
```

插件在发送前把它换成真正的表情组件（带图片 hash），对方收到的是一张图，
落库也变成 `is_emoji=1` —— 历史里不再有可供模仿的假标签。

## 三档匹配

| 档位 | 触发条件 | 实测成本 | 说明 |
| --- | --- | --- | --- |
| 精确 | 标签归一化后与某张表情描述完全一致（或单标签命中） | <1ms | 覆盖「模型照抄历史里的真表情描述」这种最常见情况 |
| 本地 | 标签与描述标签的重叠度 ≥ `local_threshold` | 5~20ms | 0.6×覆盖度 + 0.4×包含度，前 8 名再用 difflib 复算 |
| 向量 | 前两档都没命中，且整条消息就是一条标签 | 0.9~3.6s | 对标签做一次文本嵌入，与本地向量索引算余弦，≥ `vector_threshold` 才算命中 |

全部未命中时按 `behavior.miss_policy` 处理：`drop`（默认，删掉标签）/ `keep`（保留文字）/ `abort`（取消整条消息）。整条消息被删空时自动取消发送，不会发出空消息。

### 阈值是实测校准的

在 570 张表情、`nemotron-3-embed-1b`（2048 维）的库上跑出来：

| 标签 | 余弦 | 结果 |
| --- | --- | --- |
| 傲娇脸红到不行 | 0.785 | 命中「害羞,可爱,开心,脸红,卖萌」✓ |
| 摸摸头 | 0.724 | 命中「摸头杀前置帧…」✓ |
| 生气 | 0.869 | 命中「…即将生气请适可而止…」✓ |
| 今天天气真好啊 | 0.655 | 低于阈值，按未命中处理 ✓ |

所以默认阈值取 `0.68`：相关命中落在 0.71~0.87，无关内容大约 0.65 以下。
另外向量命中还要求标签与描述有字符二元组重叠（`require_char_overlap`），
挡住「语义分高但字面毫无关系」的胡配。

## 向量索引怎么维护

- 索引文件：`<插件数据目录>/emoji_vectors.json`，每条向量是单位化 float32 + base64，570 张表情约 6.0MB；
- 缓存文件：`<插件数据目录>/label_cache.json`，标签 → 命中结果（含未命中），重复标签零网络开销；
- 数据源：只查宿主的 `images` 表（emoji、已注册、未封禁、文件存在），只取 hash 与描述，不拉图片；
- 脏描述会被过滤：以 `[`/`【` 开头或带 `<|` 之类特殊 token 的描述不参与匹配；
- 首次同步：加载 `startup_delay_seconds` 秒后开始，每批 `batch_size` 条、并发 `max_concurrent`，每轮最多补 `max_items_per_sync` 条；没补齐时按 `bootstrap_interval_seconds`（默认 5 秒）连续推进，570 条约 2.5 分钟建完；
- 增量补录：每 `sync_interval_seconds`（默认 10 分钟）对一次清单，新增/移除/描述变化按 diff 处理——**新收录表情后的补向量就在这一轮**；
- 就绪状态落盘（`complete` 字段），重启直接恢复；
- 换嵌入模型或维度变化时整库重建；
- 模型接口不可用时只打 warning 并跳过本轮，前两档照常工作，不阻塞发送。

## 为什么对回复速度影响很小

- 普通文字消息：一次子串判断直接返回；
- 有标签且精确命中：查内存索引，微秒级；
- 需要向量兜底：`only_whole_message = true` 时只对「整条消息就是一条标签」生效，而标签通常是分段发送的最后一段，正文早就发出去了；
- 嵌入请求默认 3000ms 硬超时，超时按未命中处理，不会挂住发送。

## 安装

**从插件市场安装**：在插件中心搜索「表情标签桥接」，或按插件 ID `github.42yoru.maimai-emoji-label-bridge` 查找。

**手动安装**：

1. 把本目录放到 MaiBot 的 `plugins/` 下（目录名随意，ID 由 `_manifest.json` 决定）；
2. 重启 MaiBot，或在插件页触发热重载；
3. 首次加载会自动生成 `config.toml`（`config.toml.example` 只是带注释的参考，不会被自动读取）；
4. 在 WebUI 插件页确认已启用。首次启动约 30 秒后开始建向量索引，期间精确/本地两档已可用。

> **嵌入模型依赖**：向量兜底需要宿主在 `model_config.toml` 里配好 `[model_task_config.embedding]`。没配或调用失败时，插件自动只走「精确 + 本地」两档，不影响发送，也不会输出错误的表情。

## 配置

见 `config.toml(.example)`，全部字段在 WebUI 配置页可改。关键几项：

| 配置项 | 默认 | 作用 |
| --- | --- | --- |
| `behavior.miss_policy` | `drop` | 没命中时删标签 / 保留文字 / 取消整条消息 |
| `behavior.max_tags_per_message` | `3` | 单条消息最多处理几个标签 |
| `behavior.dedupe_with_existing_emoji` | `true` | 本次已带真表情时删掉正文标签 |
| `match.local_threshold` | `0.5` | 本地重叠度阈值 |
| `match.enable_vector` | `true` | 是否启用嵌入兜底 |
| `match.vector_threshold` | `0.68` | 向量余弦阈值 |
| `match.vector_timeout_ms` | `3000` | 单次嵌入硬超时 |
| `match.only_whole_message` | `true` | 只在纯标签消息上走向量 |
| `match.require_char_overlap` | `true` | 向量命中还要求字面重叠 |
| `index.enabled` | `true` | 关掉就完全不碰嵌入模型 |
| `index.sync_interval_seconds` | `600` | 增量同步间隔 |

## 兼容性

- 只依赖 `send_service.after_build_message`（允许 abort、允许改 kwargs），旧 `reply` 工具、被动式 replyer、Focus 都走同一个发送层，一视同仁；
- 不改主程序，不写全局状态；宿主访问全部走 SDK 能力（`database.query`、`llm.embed`）；
- Runner 把插件跑在独立进程里，插件异常按 `error_policy=skip` 处理，不会影响消息发送。

## 已知边界

- 嵌入接口延迟抖动较大（实测 0.9~3.6s），抖到超过 `vector_timeout_ms` 时这张表情会被放弃，而不是塞一张错的图；
- 只做「文字标签 → 表情组件」，不会把已经被拆成多条的文字重新合并；
- 同一消息多个标签逐个匹配，受 `max_tags_per_message` 限制；
- 文件缺失的表情记录不进索引，避免发送时读文件失败。

## 插件信息

| 项 | 值 |
| --- | --- |
| 插件 ID | `github.42yoru.maimai-emoji-label-bridge` |
| 版本 | 0.1.2 |
| 仓库 | <https://github.com/42yoru/maimai-emoji-label-bridge> |
| 许可证 | MIT |
