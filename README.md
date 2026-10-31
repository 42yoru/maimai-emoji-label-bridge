# 表情标签桥接（Emoji Label Bridge）

<p>
  <a href="https://github.com/42yoru/maimai-emoji-label-bridge/actions/workflows/ci.yml"><img src="https://github.com/42yoru/maimai-emoji-label-bridge/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/Mai-with-u/MaiBot"><img src="https://img.shields.io/badge/MaiBot-1.3.15%2B-orange.svg" alt="MaiBot"></a>
  <a href="https://github.com/Mai-with-u/maibot-plugin-sdk"><img src="https://img.shields.io/badge/SDK-2.12%2B-green.svg" alt="SDK"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python">
</p>

> **一句话定位：** 在发送层把模型写出的 `[表情包: …]` 文字标签转换为真实表情组件，并同步清理历史里的假标签。

## 适用与不适用

**适用：**

- 模型把表情包渲染格式误写进回复正文；
- 希望在不改 MaiBot 主程序的情况下，让旧 `reply` 工具、被动式 replyer 和 Focus 统一经过同一发送层处理；
- 需要精确匹配、本地标签重叠匹配，以及可选的向量兜底。

**不适用：**

- 不负责生成表情描述或修改主程序；
- 只处理文字标签，不会把已经拆成多条的文字重新合并；
- 向量兜底只在 `match.only_whole_message=true` 时处理整条纯标签消息。

## 快速安装

### 插件市场

在插件中心搜索“表情标签桥接”，或按插件 ID `github.42yoru.maimai-emoji-label-bridge` 查找。

### 手动安装

```bash
cd MaiBot/plugins
git clone https://github.com/42yoru/maimai-emoji-label-bridge.git
```

然后重启 MaiBot 或触发插件热重载。宿主首次加载时会生成 `config.toml`；`config.toml.example` 仅供参考，不会自动读取。

## 最小配置

插件默认即可使用精确匹配和本地匹配。若需要向量兜底，在 `model_config.toml` 配置 `[model_task_config.embedding]`，并确认插件配置中启用索引：

```toml
[plugin]
enabled = true

[index]
enabled = true

[match]
enable_vector = true
```

没有可用嵌入模型时，插件会保留精确和本地两档，不会因此阻塞发送。没有嵌入需求时，可将 `index.enabled=false`，完全关闭这条出站数据流。

## 工作流程

1. 发送层检查文本中是否存在 `[表情包: …]` 标签。
2. 依次尝试：精确匹配 → 本地标签重叠匹配 →（符合条件时）向量匹配。
3. 命中后替换为带图片 hash 的真实表情组件，并重算 `processed_plain_text`；整条消息只剩一个表情时，按宿主口径设置 `is_emoji=true`。
4. 未命中时按 `behavior.miss_policy` 执行：删除标签、保留文字，或取消整条消息。
5. 后台任务按清单增量维护向量索引；模型或维度变化会重建索引。

匹配说明：精确匹配通常低于 1ms；本地匹配约 5–20ms；向量兜底实测约 0.9–3.6 秒，并受 `vector_timeout_ms` 和整轮预算限制。

## 配置表

| 配置项 | 默认值 | 作用 |
| --- | --- | --- |
| `behavior.miss_policy` | `drop` | 未命中时删除标签、保留文字，或取消整条消息 |
| `behavior.max_tags_per_message` | `3` | 单条消息最多处理的标签数 |
| `behavior.dedupe_with_existing_emoji` | `true` | 已有真实表情时是否删除正文标签 |
| `match.local_threshold` | `0.5` | 本地标签重叠阈值 |
| `match.enable_vector` | `true` | 是否启用向量兜底 |
| `match.vector_threshold` | `0.68` | 向量余弦阈值 |
| `match.vector_timeout_ms` | `3000` | 单次嵌入请求超时（毫秒） |
| `match.only_whole_message` | `true` | 仅对整条纯标签消息使用向量兜底 |
| `match.require_char_overlap` | `true` | 向量命中还必须有字符二元组重叠 |
| `index.enabled` | `true` | 是否维护和使用向量索引 |
| `index.sync_interval_seconds` | `600` | 增量同步间隔 |
| `punctuation.enabled` | `false` | 是否清理无假标签普通文本末尾的句号 |

`punctuation.strip_trailing_chars` 默认只有中文句号“。”；不建议加入半角句点，以免误伤 `v0.1.13` 等文本。

## 边界与隐私

- 数据源只读取宿主 `images` 表中的 hash 和描述，不读取图片二进制。
- 向量同步会把表情描述发送给 `model_task_config.embedding` 指向的模型；向量兜底会把标签文本发送给同一任务，不发送聊天记录。请按模型服务商的隐私策略配置，或关闭 `index.enabled`。
- 向量索引位于 `<插件数据目录>/emoji_vectors.json`；标签缓存位于 `<插件数据目录>/label_cache.json`。缓存会保存标签及命中结果，默认 TTL 为 604800 秒。
- 脏描述、文件缺失的表情不会进入索引；嵌入超时或模型不可用时按未命中处理。
- 文字中夹有 at、引用、图片等组件时，插件尽量重算可见纯文本；不认识的组件交给宿主处理。

## 测试

在插件目录运行：

```bash
python -m pytest tests -q
```

测试覆盖标签解析、三档匹配、发送层改写、配置、向量缓存和超时行为。

## 插件信息

| 项目 | 值 |
| --- | --- |
| 插件 ID | `github.42yoru.maimai-emoji-label-bridge` |
| 版本 | `0.2.0` |
| 仓库 | <https://github.com/42yoru/maimai-emoji-label-bridge> |
| 许可证 | MIT |
