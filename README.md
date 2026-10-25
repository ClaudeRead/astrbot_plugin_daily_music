# astrbot_plugin_daily_music 每日一听

让 Bot 每天按计划"自己去听歌"，并为每首歌写下听后感，把听歌记忆与感受存入
[LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)
长期记忆插件，让 Bot 逐渐积累自己的音乐品味。

## 功能

- **三平台选歌**：网易云、QQ音乐（均支持歌单 ID 或关键词搜索，走公开接口无需凭据）；
  汽水音乐（无公开 API，通过歌名列表或自定义 JSON 接口提供歌曲）。
- **行为可设定**：每天听几首、每首听多久、平台顺序、去重历史容量。
- **听后感生成**：每天只调用一次 LLM，批量产出每首歌的一句感受（情绪 + 文本）。
- **记忆落库**：听歌记忆与感受直接写入 LivingMemory（复用其存储格式，零额外 token）；
  未安装时自动回退保存到插件数据目录 `memories_fallback.jsonl`。
- **结果推送**：定时听歌完成后把摘要推送到配置的通知会话。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/listen` | 立即听一轮（管理员） |
| `/listen 状态` | 查看当前配置与各来源/记忆状态 |
| `/listen 推送` | 把当前会话加入/移出结果通知列表 |

别名：`每日一听`。

## Token 优化设计（最高原则）

1. **单次批量调用**：一天所有歌曲合并进一次 LLM 请求，输出紧凑 JSON，而不是逐首歌调用。
2. **缓存友好提示词**：固定指令前缀（含性格设定）在前、当日歌单数据在后，前缀逐日不变，
   命中供应商端 prompt 缓存；修改 `llm.persona` 会破坏前缀，非必要不改动。
3. **记忆直写**：写入 LivingMemory 走其 `memory_engine.add_memory` 接口，不经过
   LLM 函数调用，不产生任何推理 token。

## 安全设计

- 所有外发请求经 `http_util.validate_url` 校验：仅允许 http/https，目标主机解析结果
  落在环回/私有/保留网段时拒绝（SSRF 防护）；请求禁用重定向。
- 全程使用公开只读接口，插件不存储任何凭据。
- 歌词仅摘取一句用于生成情境，不整篇存储。

## 配置速览

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `schedule.time` | 21:00 | 每日触发时间（时区默认 Asia/Shanghai） |
| `listen.songs_per_day` | 3 | 每天听几首 |
| `listen.listen_seconds` | 180 | 每首听多久（秒，写入情境不阻塞运行） |
| `netease.playlist_id` / `keywords` | 关键词 | 歌单 ID 优先，留空用关键词搜索 |
| `qq.playlist_id` / `keywords` | 关键词 | 同上（歌单 ID 为网页链接中 disstid） |
| `qishui.song_list` / `api_url` | 空 | 每行一首「歌名 - 歌手」；api_url 返回 `{"songs":[{"name","artist"}]}` |
| `llm.provider_id` / `model` | 默认 | 听后感生成所用模型 |
| `memory.enable` / `importance` / `scope` | 开 / 0.6 / global | 记忆写入开关、重要性与作用域 |
| `notify.sessions` | 空 | 通知会话 unified_msg_origin 列表 |

## 二次开发指引

```
main.py              入口：指令、APScheduler 定时、听歌流程编排
music_source.py      平台来源：实现 BaseSource（list_tracks / decorate）即可接入新平台
feeling_generator.py 听后感生成：提示词组装、JSON 解析、单次重试
living_memory.py     LivingMemory 直写与本地 JSONL 回退
http_util.py         SSRF 防护的异步 HTTP
```

接入新平台：在 `music_source.py` 继承 `BaseSource` 实现两个方法，加入 `SOURCE_CLASSES`，
并在配置 `listen.platforms` 中登记即可。

## 安装

在 AstrBot WebUI 插件市场搜索 `astrbot_plugin_daily_music`，或从仓库安装：

```
https://github.com/ClaudeRead/astrbot_plugin_daily_music
```

依赖：`aiohttp`、`apscheduler`（AstrBot 自带，安装插件时自动处理）。
