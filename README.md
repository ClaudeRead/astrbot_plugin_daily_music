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

## 缓存优先设计（最高原则）

插件的第一原则是「提高缓存命中、降低未命中」，提示词结构完全围绕它设计：

1. **稳定前缀**：`SYSTEM_PROMPT` 逐字固定，作为 `system_prompt` 单独传入，不掺任何配置变量，
   供应商端 prompt cache 会把这 128 tokens 的前缀缓存住。
2. **可变内容全部后置**：歌单、字数限制、性格设定都放在用户消息里，按「限制 → 设定 → 数据」
   排序，永远排在稳定前缀之后 —— 修改 `llm.persona` / `llm.feeling_max_chars` 只让尾部
   未命中，不会让整段前缀失效。
3. **单次批量调用**：一天所有歌曲合并进一次 LLM 请求，输出紧凑 JSON，而不是逐首歌调用。
4. **重试只动尾部**：解析失败时的重试提示追加在用户消息末尾，系统前缀一字不改。
5. **记忆直写**：写入 LivingMemory 走其 `memory_engine.add_memory` 接口，不经过
   LLM 函数调用，不产生任何推理 token。

实测（DeepSeek 官方接口，一次 3 首歌的听后感生成）：`prompt_tokens=280`、
`cache_hit_tokens=128`、`cache_miss_tokens=152` —— 稳定前缀命中，只有歌单尾部未命中。

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
| `llm.provider_id` | 空 | 听后感所用大模型，**从下拉列表选择**；留空用 AstrBot 全局默认提供商 |
| `llm.model` | 空 | 一般留空，使用所选提供商自己的默认模型；填了不属于该提供商的模型名会触发 400 |
| `llm.feeling_max_chars` | 50 | 每句听后感字数上限（放在用户消息里，改动不影响缓存前缀） |
| `memory.enable` / `importance` / `scope` | 开 / 0.6 / global | 记忆写入开关、重要性与作用域 |
| `notify.sessions` | 空 | 通知会话 unified_msg_origin 列表 |

## 二次开发指引

```
main.py              入口：指令、APScheduler 定时、听歌流程编排
music_source.py      平台来源：实现 BaseSource（list_tracks / decorate）即可接入新平台
feeling_generator.py 听后感生成：静态系统前缀 + 可变用户消息、JSON 解析、单次重试
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

## 更新日志

- **v1.0.4**
  - 听后感提示词改为「静态系统前缀 + 可变用户消息」：改性格设定 / 字数上限不再让供应商端
    缓存整体失效，重试只追加用户消息尾部。
  - 配置项 `llm.provider_id` 改为**下拉选择**（`_special: select_provider`），不再手填提供商 ID。
  - `llm.model` 改为「一般留空」，避免手填模型名导致 400。
  - QQ音乐 `musicu.fcg` 备用搜索改为 3 次尝试（0/3/8 秒退避），缓解频控导致的选歌失败。
- **v1.0.3** QQ音乐 musicu.fcg 空结果时带 code 重试一次
- **v1.0.2** QQ音乐增加 musicu.fcg 备用接口；单平台选歌失败时其他平台自动补位
- **v1.0.1** 指定模型不属于当前提供商时回退提供商默认模型
- **v1.0.0** 首个版本
