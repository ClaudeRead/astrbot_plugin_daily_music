# astrbot_plugin_daily_music 每日一听

让 Bot 每天在你自己设定的时间段里"自己去听歌"：先按性格设定挑一个歌单主题，再到指定平台找歌，
听完为每首歌写一句听后感，并把听歌记忆与感受存入
[LivingMemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)
长期记忆插件，让 Bot 逐渐积累自己的音乐品味。

## 功能

- **时间段听歌**：默认每天 `13:00-14:00`、`18:00-21:00` 各听一次（可自定义多段，支持跨零点）。
  每次在时间段内随机挑一个时刻出发，避免每天同一秒惊醒；重启后若时间段还没过完，仍会补听。
- **Bot 自己选歌单**：按时段与人格设定让模型挑一个主题（如"雨夜爵士"），再用主题去平台搜歌；
  也可以关掉 `listen.bot_choose`，改用固定歌单/关键词。
- **三平台来源**：汽水音乐（默认，歌名列表或自定义 JSON 接口）、QQ音乐、网易云音乐
  （后两者支持歌单 ID 或关键词搜索，走公开接口无需凭据）。按 `listen.platforms` 的顺序尝试，
  前一个平台没歌就自动换下一个。
- **听歌量两种口径**：按时长（默认 5 分钟）或按首数（默认 3 首），插件自动换算要听几首、每首多久。
- **听后感生成**：每轮只调用模型两次（一次选主题、一次批量写听后感），输出紧凑 JSON。
- **记忆落库**：听歌记录与感受写入 LivingMemory（复用其存储格式，零额外 token）；
  未安装时自动回退保存到插件数据目录 `memories_fallback.jsonl`。
- **结果推送**：每轮结束后把摘要推送到配置的通知会话。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/listen` | 立即听一轮（管理员） |
| `/listen 状态` | 查看时间段、下一次计划、模式与来源状态 |
| `/listen 推送` | 把当前会话加入/移出结果通知列表 |

别名：`每日一听`。

## 时间段的运作方式

插件每 5 分钟检查一次"现在是否落在某个时间段里"，并对每个时间段每天只执行一次：

1. 计划时刻 = 时间段起点 + 随机偏移（只用时间段前 80%，避免贴着末尾才出发）。
   随机种子是「日期@时间段」字符串，所以同一天多次检查得到的是**同一个**时刻，进程重启也不会变。
2. 到了计划时刻且该时间段今天还没跑过 → 开始听歌。
3. 跑过的记录写在插件数据目录的 `state.json`（`runs` 保留最近 7 天），跨零点时间段
   （如 `23:00-01:00`）的凌晨部分归属前一天。

## Token 与缓存优化设计（最高原则）

1. **静态系统提示词**：两处调用（选主题、写听后感）的 system 提示词**逐字不变**，随配置/时间/
   歌单变化的内容（人格、字数要求、时段、歌单）全部放在用户消息里 —— 改配置只会让尾部未命中，
   不会让整段前缀失效。
2. **前缀极短且稳定**：system 提示词不含日期、不含随机量，供应商端 prompt 缓存可长期复用。
   实测 3 首歌时 `cache_hit=128 / cache_miss=148`（v1.0.4 数据）。
3. **每轮两次调用**：选主题一次 + 批量写听后感一次，不逐首调用。
4. **快速失败**：`request_max_retries=1`，模型超时/不可用时立刻降级，不占用队列。
5. **候选只拉一次**：每个平台一次网络请求取回候选列表，本地去重/随机，不逐首请求。
6. **记忆直写**：写入 LivingMemory 走 `memory_engine.add_memory`，不经过模型函数调用。

## 安全设计

- 所有外发请求经 `http_util.validate_url` 校验：仅允许 http/https，目标主机解析结果
  落在环回/私有/保留网段时拒绝（SSRF 防护）；请求禁用重定向。
- 全程使用公开只读接口，插件不存储任何凭据。
- 模型输出（主题词、听后感）做长度与字符清洗，主题词只保留汉字/字母/数字，避免污染搜索与记忆。
- 歌词仅摘取一句用于生成情境，不整篇存储。

## 配置速览

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `schedule.enable` | 开 | 是否启用定时听歌 |
| `schedule.windows` | `13:00-14:00`<br>`18:00-21:00` | 每天听歌的时间段，一行一段，可写多段、可跨零点 |
| `schedule.time` | 空 | 旧版单点配置，仅在 `windows` 为空时兼容使用（前后各 1 小时） |
| `schedule.timezone` | Asia/Shanghai | 时间段使用的时区 |
| `listen.platforms` | `["qishui"]` | 平台及其顺序（默认汽水音乐），前一个没歌自动换下一个 |
| `listen.bot_choose` | 开 | 由 Bot 按人格挑选歌单主题；关闭则用各平台的歌单/关键词 |
| `listen.mode` | 按时长 | `按时长`：由 `duration_minutes` 换算；`按首数`：用 `songs_per_day` |
| `listen.duration_minutes` | 5 | 每轮想听多少分钟（按时长模式） |
| `listen.songs_per_day` | 3 | 每轮听几首（按首数模式） |
| `listen.listen_seconds` | 180 | 每首听多久（按首数模式，仅写入情境，不阻塞运行） |
| `netease.playlist_id` / `keywords` | 空 | 歌单 ID 优先；留空则用主题词/关键词搜索 |
| `qq.playlist_id` / `keywords` | 空 | 同上（歌单 ID 为网页链接中的 disstid） |
| `qishui.song_list` / `api_url` | 空 | 每行一首「歌名 - 歌手」；`api_url` 可含 `{keyword}` 占位，返回 `{"songs":[{"name","artist"}]}` |
| `llm.provider_id` / `model` | 默认 / 空 | 从下拉里选服务提供商；模型名一般留空，用提供商默认模型 |
| `llm.persona` | 空 | 人格设定，用于选歌主题与听后感口吻（放在用户消息里，不影响缓存） |
| `llm.feeling_max_chars` | 50 | 每首歌听后感的字数上限 |
| `memory.enable` / `importance` / `scope` | 开 / 0.6 / global | 记忆写入开关、重要性与作用域 |
| `notify.sessions` | 空 | 通知会话 unified_msg_origin 列表 |

## 二次开发指引

```
main.py              入口：指令、5 分钟轮询调度、听歌流程编排
windows.py           时间段解析与计划时刻（纯函数，无第三方依赖，可直接单测）
mood.py              按人格挑选歌单主题（提示词 + JSON 解析）
music_source.py      平台来源：实现 list_tracks(keyword) 即可接入新平台
feeling_generator.py 听后感生成：提示词组装、JSON 解析
llm_client.py        提供商解析与单次调用（system/user 分离，缓存友好）
living_memory.py     LivingMemory 直写与本地 JSONL 回退
http_util.py         SSRF 防护的异步 HTTP
```

接入新平台：在 `music_source.py` 继承 `BaseSource`，实现 `list_tracks(keyword)` 与
（可选）`decorate(track)` 补全歌词，加入 `SOURCE_CLASSES`，并在配置项
`listen.platforms` 的 `options` 里登记即可。

离线自测：`python tests/test_daily_music.py`（用桩替代 AstrBot 运行时，覆盖时间段、主题清洗、
提示词缓存结构、听歌量换算与来源过滤）。

## 安装

在 AstrBot WebUI 插件市场搜索 `astrbot_plugin_daily_music`，或从仓库安装：

```
https://github.com/ClaudeRead/astrbot_plugin_daily_music
```

依赖：`aiohttp`、`apscheduler`（AstrBot 自带，安装插件时自动处理）。

## 更新日志

### v2.0.0

- 新增：时间段听歌（默认 13:00-14:00、18:00-21:00，可自定义多段、支持跨零点）。
- 新增：Bot 按人格自己挑歌单主题，再用主题去平台找歌（可关闭）。
- 新增：听歌量可按时长（默认 5 分钟）或按首数（默认 3 首）设定。
- 新增：默认平台改为汽水音乐，平台按配置顺序尝试、失败自动换下一个。
- 新增：重复时间段的去重与跨天补听（`state.json`）。
- 优化：候选歌曲每平台只拉一次；记忆文案带日期/时段/主题；状态指令信息更完整。
- 优化：歌单被听遍时自动从听过的歌里重播，不会出现「没歌可听」。
- 优化：`windows.py` / `llm_client.py` / `mood.py` 拆分为独立模块，便于二次开发与单测。

### v1.0.4

- 优化：提示词改为「静态 system + 可变 user」，最大化供应商端缓存命中。
- 修复：手填模型名不属于该提供商时（400）自动回退默认模型。
- 优化：QQ音乐 `musicu.fcg` 搜索重试退避（0s/3s/8s）。

### v1.0.3

- 固定系统提示词前缀，降低缓存未命中；补全 `llm.persona` 说明。
