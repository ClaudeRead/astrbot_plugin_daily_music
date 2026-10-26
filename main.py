"""AstrBot 插件「每日一听」

让 Bot 每天在用户设定的时间段里「自己去听歌」：
    1. 到点后按自己的人格设定挑一个歌单主题（mood.py）
    2. 从用户指定的平台（默认汽水音乐）按主题找歌，挑几首开始听（music_source.py）
    3. 单次 LLM 调用批量写听后感（feeling_generator.py，固定前缀命中缓存）
    4. 听歌记录与心情写入 LivingMemory，未安装时回退本地档案（living_memory.py）
    5. 结果摘要推送到通知会话

模块结构（便于二次开发）：
    main.py              插件入口：指令、时间窗调度与流程编排
    windows.py           时间窗解析与计划时刻（纯函数，可单测）
    mood.py              按人格选歌单主题（缓存友好的提示词）
    music_source.py      音乐来源：网易云 / QQ音乐 / 汽水音乐
    feeling_generator.py 听后感生成
    llm_client.py        提供商解析与单次 LLM 调用
    living_memory.py     LivingMemory 联动与本地回退
    http_util.py         带 SSRF 防护的 HTTP 请求
"""

import asyncio
import json
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import Plain

from . import share
from . import windows
from .feeling_generator import FeelingGenerator
from .living_memory import get_lmem, save_memory
from .llm_client import provider_desc
from .mood import MoodPicker
from .music_source import build_sources

JOB_ID = "daily_music_tick"      # 轮询任务 ID
TICK_MINUTES = 5                 # 轮询间隔：窗口内每 5 分钟检查一次是否到点
BOOT_DELAY_SECONDS = 45          # 启动后尽早检查一次，便于重启后补跑未结束的时间段
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_WINDOWS = ("13:00-14:00", "18:00-21:00")
PLUGIN_DIR_NAME = "astrbot_plugin_daily_music"
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
AVG_TRACK_MINUTES = 3.5          # 按时长听时，估算每首可听多久
MAX_TRACKS_PER_RUN = 12
STATE_KEEP_DAYS = 7              # 运行记录保留天数
THEME_KEEP = 10                  # 记住最近多少个主题，用于"换点新鲜的"


def _clamp(value, low, high, default) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def compute_budget(listen_cfg: dict) -> tuple:
    """按「时长」或「首数」换算本次要听几首、每首多少秒。"""
    mode = str(listen_cfg.get("mode") or "duration").strip().lower()
    if mode == "count":
        need = _clamp(listen_cfg.get("songs_per_day"), 1, MAX_TRACKS_PER_RUN, 3)
        seconds = _clamp(listen_cfg.get("listen_seconds"), 10, 3600, 180)
        return need, seconds

    minutes = _clamp(listen_cfg.get("duration_minutes"), 1, 180, 5)
    need = max(1, min(MAX_TRACKS_PER_RUN, -(-int(minutes * 60) // int(AVG_TRACK_MINUTES * 60))))
    return need, max(10, int(minutes * 60 / need))


class DailyMusicPlugin(Star):
    """每日一听插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.generator = FeelingGenerator(context, config)
        self.mood = MoodPicker(context, config)
        # 定时器显式指定时区，避免跟随容器系统时区（多为 UTC）导致触发时间偏移
        self.scheduler = AsyncIOScheduler(timezone=self._resolve_timezone())
        self._lock = asyncio.Lock()  # 防止定时任务与手动触发同时听歌

    def _resolve_timezone(self) -> ZoneInfo:
        """解析配置的时区；非法时依次回退到 Asia/Shanghai 与系统本地时区。"""
        tz_name = str((self.config.get("schedule", {}) or {}).get("timezone") or DEFAULT_TZ).strip()
        for name in (tz_name, DEFAULT_TZ):
            try:
                return ZoneInfo(name)
            except Exception:
                logger.warning(f"[每日一听] 未知时区 {name!r}，尝试回退")
        return None  # None = APScheduler 跟随系统本地时区

    async def initialize(self) -> None:
        """插件加载/重载时注册轮询任务（WebUI 保存配置后会自动重载插件）。"""
        if not self.scheduler.running:
            self.scheduler.start()
        self._setup_schedule()
        logger.info("[每日一听] 插件已加载")

    async def terminate(self) -> None:
        """插件停用/卸载时关闭调度器。"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # 时间窗与调度
    # ------------------------------------------------------------------

    def _windows(self) -> list:
        """解析听歌时间段：优先 schedule.windows，其次兼容旧的 schedule.time。"""
        cfg = self.config.get("schedule", {}) or {}
        parsed, invalid = windows.parse_windows(cfg.get("windows"))
        if invalid:
            logger.warning(f"[每日一听] 忽略无法解析的时间段：{'、'.join(invalid)}")
        if not parsed:
            parsed = windows.legacy_window(cfg.get("time"))
        if not parsed:
            parsed, _ = windows.parse_windows("\n".join(DEFAULT_WINDOWS))
        return parsed

    def _setup_schedule(self) -> None:
        """注册窗口轮询任务；停用时移除。"""
        cfg = self.config.get("schedule", {}) or {}
        if not cfg.get("enable", True):
            if self.scheduler.get_job(JOB_ID):
                self.scheduler.remove_job(JOB_ID)
            logger.info("[每日一听] 定时听歌已停用")
            return

        windows_list = self._windows()
        self.scheduler.add_job(
            self._tick,
            IntervalTrigger(minutes=TICK_MINUTES, timezone=self.scheduler.timezone),
            id=JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
            next_run_time=datetime.now(self.scheduler.timezone) + timedelta(seconds=BOOT_DELAY_SECONDS),
        )
        labels = "、".join(windows.fmt_window(w) for w in windows_list)
        upcoming = windows.next_plan(windows_list, datetime.now(self.scheduler.timezone))
        next_desc = f"{upcoming[0]:%m-%d %H:%M}（{windows.fmt_window(upcoming[1])}）" if upcoming else "无"
        logger.info(
            f"[每日一听] 定时听歌：每天 {labels}，每 {TICK_MINUTES} 分钟检查一次，"
            f"下一次 {next_desc}"
        )

    async def _tick(self) -> None:
        """窗口轮询：到点且今天该窗口还没听过，就开始听歌。"""
        if self._lock.locked():
            return
        now = datetime.now(self.scheduler.timezone)
        slot = windows.current_slot(self._windows(), now)
        if slot is None:
            return
        window, key, planned = slot
        if now < planned:
            logger.debug(f"[每日一听] 当前窗口 {windows.fmt_window(window)} 计划 {planned:%H:%M} 开始")
            return
        if self._load_state().get("runs", {}).get(key):
            return

        logger.info(f"[每日一听] 时间段 {windows.fmt_window(window)} 到点（计划 {planned:%H:%M}），开始听歌")
        summary = await self._run_listen(key, windows.fmt_window(window))
        await self._push(summary)

    async def _push(self, summary: str) -> None:
        for umo in (self.config.get("notify", {}) or {}).get("sessions") or []:
            try:
                await self.context.send_message(str(umo), MessageChain(chain=[Plain(summary)]))
            except Exception as e:
                logger.warning(f"[每日一听] 推送到 {umo} 失败: {e}")

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("listen", alias={"每日一听"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def listen(self, event: AstrMessageEvent, action: str = ""):
        """每日一听：无参数立即听一轮；「状态」查看配置；「推送」把当前会话加入/移出结果通知"""

        if action in ("状态", "status"):
            yield event.plain_result(await self._status_text())
        elif action in ("推送", "通知", "notify"):
            yield event.plain_result(self._toggle_notify(event.unified_msg_origin))
        else:
            yield event.plain_result(await self._run_listen())

    @filter.command("share", alias={"分享"})
    async def share(self, event: AstrMessageEvent):
        """主动分享一首歌/歌单链接"""
        yield event.plain_result(await self._share_random())

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """识别私聊/群聊里发给 Bot 的音乐链接，去听后按人格给出评价。

        非链接消息不产出回复，也不阻止事件，让 AstrBot 主流程/LLM 正常处理。
        """
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text or text.startswith("/") or self._is_self_message(event):
            return
        link = share.parse_share_link(text)
        if link is None:
            return
        reply = await self._handle_share(link)
        if reply:
            yield event.plain_result(reply)
            event.stop_event()  # 阻止 LLM 对这条链接重复回复

    # ------------------------------------------------------------------
    # 分享
    # ------------------------------------------------------------------

    @staticmethod
    def _is_self_message(event) -> bool:
        """判断是否为 Bot 自己发出的消息，避免回声触发自我回复。"""
        try:
            msg = getattr(event, "message_obj", None)
            sender = getattr(msg, "sender", None)
            sender_id = str(getattr(sender, "user_id", "") or "")
            self_id = str(getattr(msg, "self_id", "") or "")
            return bool(sender_id) and sender_id == self_id
        except Exception:
            return False

    async def _handle_share(self, link) -> str:
        """处理识别到的音乐链接：解析详情 → 生成评价 → 组装回复。"""
        if link.platform == "qishui":
            return (
                "🎧 我识别到这是汽水音乐（抖音）的分享链接，但汽水音乐没有公开的歌曲信息接口，"
                "我暂时查不到具体是哪首歌。\n你可以把「歌名 - 歌手」一起发我，我马上去听一听给你评价～"
            )
        try:
            tracks = await share.resolve_tracks(link)
        except Exception as e:
            logger.warning(f"[每日一听] 解析分享链接失败: {e}")
            return "❌ [每日一听] 没能解析这个链接的歌曲信息，请确认链接可访问后重试。"
        if not tracks:
            return "❌ [每日一听] 这个链接里没找到歌曲信息（可能已失效或版权受限）。"

        kind_label = "歌单" if link.kind == "playlist" else "歌曲"
        heading = "【朋友分享的歌单】" if link.kind == "playlist" else "【朋友分享的歌曲】"
        try:
            feelings = await self.generator.generate(
                tracks, share.SHARE_LISTEN_SECONDS, "", heading=heading
            )
        except Exception as e:
            logger.error(f"[每日一听] 分享评价生成失败: {e}")
            feelings = {}

        lines = [f"🎧 收到你分享的{kind_label}，我来听一听～"]
        for idx, track in enumerate(tracks, 1):
            feeling = feelings.get(idx, ("", "neutral"))[0]
            desc = f"《{track.name}》-{track.artist}"
            if track.album:
                desc += f"（专辑《{track.album}》）"
            if feeling:
                desc += f"：{feeling}"
            lines.append(f"{idx}. [{track.platform_name}] {desc}")
        if link.kind == "playlist" and len(tracks) >= share.MAX_SHARE_TRACKS:
            lines.append(f"（歌单很长，我只听了其中 {len(tracks)} 首～）")
        return "\n".join(lines)

    async def _share_random(self) -> str:
        """主动分享：配置了歌单的平台直接分享歌单链接，否则随机挑一首带链接的歌。"""
        sources = build_sources(self.config)
        if not sources:
            return "❌ [每日一听] 没有可用的音乐来源，请先在插件配置里填歌单/关键词/歌名列表。"
        source = random.choice(sources)
        if getattr(source, "playlist_id", ""):
            url = share.playlist_url(source.key, source.playlist_id)
            if url:
                return f"🎵 [每日一听] 分享一个 {source.display} 歌单给你：\n🔗 {url}"
        candidates = await source.candidates("")
        if not candidates:
            return f"❌ [每日一听] 从 {source.display} 没找到可分享的歌，请检查来源配置。"
        track = random.choice(candidates)
        try:
            await source.decorate(track)
        except Exception as e:
            logger.debug(f"[每日一听] {source.display} 补全歌词失败: {e}")
        review = ""
        try:
            feelings = await self.generator.generate(
                [track], share.SHARE_LISTEN_SECONDS, "", heading="【准备分享的歌】"
            )
            review = feelings.get(1, ("", "neutral"))[0]
        except Exception as e:
            logger.warning(f"[每日一听] 分享推荐语生成失败: {e}")
        lines = [f"🎵 [每日一听] 分享一首歌给你：", f"《{track.name}》- {track.artist}"]
        if track.album:
            lines.append(f"专辑《{track.album}》")
        if review:
            lines.append(review)
        if track.url:
            lines.append(f"🔗 {track.url}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 核心流程
    # ------------------------------------------------------------------

    async def _run_listen(self, slot_key: str = "", window_label: str = "") -> str:
        """执行一轮听歌；slot_key 非空表示这是定时任务，完成后标记该窗口已跑。"""
        if self._lock.locked():
            return "[每日一听] 已有听歌任务正在进行，请稍候。"
        async with self._lock:
            try:
                return await self._listen_once(slot_key, window_label)
            except Exception as e:
                logger.exception(f"[每日一听] 听歌流程异常: {e}")
                return f"❌ [每日一听] 听歌流程异常：{e}"

    async def _listen_once(self, slot_key: str, window_label: str) -> str:
        listen_cfg = self.config.get("listen", {}) or {}
        need, listen_seconds = self._budget(listen_cfg)

        sources = build_sources(self.config)
        if not sources:
            return ("❌ [每日一听] 没有可用的音乐来源：请在插件配置里选平台，"
                    "并填好歌单 ID / 关键词 / 歌名列表（汽水音乐需歌名列表或接口）")

        now = datetime.now(self.scheduler.timezone)
        pretty_date = f"{now.year}年{now.month}月{now.day}日 {WEEKDAYS[now.weekday()]}"
        state = self._load_state()

        # 1. 让 Bot 自己挑主题（关闭时用配置的关键词/歌单）
        theme = await self._pick_theme(listen_cfg, window_label or f"{now:%H:%M}", state)

        # 2. 选歌：按配置的平台顺序，每个平台只拉一次候选
        history = self._load_history()
        picked = await self._collect(sources, theme["keyword"], need, history)
        if not picked and theme["keyword"]:
            logger.warning("[每日一听] 主题词没搜到歌，回退到配置的关键词/歌单")
            picked = await self._collect(sources, "", need, history)
        if not picked and history:
            # 歌单被听遍了：宁可重听也不空手，重播时忽略历史去重
            logger.info("[每日一听] 没有新歌可听，从听过的歌里随机重播")
            picked = await self._collect(sources, "", need, history, allow_repeat=True)

        lines = [f"🎧 每日一听 · {pretty_date}" + (f" {window_label}" if window_label else "")]
        if not picked:
            lines.append("❌ 没选到可听的歌，请检查各平台的来源配置")
            return "\n".join(lines)
        if theme["keyword"]:
            vibe = f"——{theme['vibe']}" if theme["vibe"] else ""
            lines.append(f"🎵 今天的歌单主题：{theme['keyword']}{vibe}")

        # 3. 模拟真实听歌节奏（可选，默认关闭）
        if listen_cfg.get("realtime_delay"):
            for _ in picked:
                await asyncio.sleep(min(listen_seconds, 15))

        # 4. 单次 LLM 批量生成听后感
        try:
            feelings = await self.generator.generate(
                [t for _, t in picked], listen_seconds, theme["vibe"]
            )
        except Exception as e:
            logger.error(f"[每日一听] 听后感生成失败: {e}")
            feelings = {}

        # 5. 逐首展示并写入记忆
        store = await self._save_memories(picked, feelings, pretty_date, theme, listen_cfg)
        for idx, (_, track) in enumerate(picked, 1):
            feeling = feelings.get(idx, ("", "neutral"))[0]
            desc = f"《{track.name}》-{track.artist}：{feeling}" if feeling else \
                   f"《{track.name}》-{track.artist}（感受未生成）"
            lines.append(f"{idx}. [{track.platform_name}] {desc}")
        lines.append(f"🧠 记忆落点：{store}")

        # 6. 更新历史与状态
        self._save_history(history + [t.key for _, t in picked])
        self._save_state(slot_key, theme["keyword"])
        logger.info("[每日一听] 本次结果：\n" + "\n".join(lines))
        return "\n".join(lines)

    def _budget(self, listen_cfg: dict) -> tuple:
        """按「时长」或「首数」换算本次要听几首、每首多少秒。"""
        return compute_budget(listen_cfg)

    async def _pick_theme(self, listen_cfg: dict, time_label: str, state: dict) -> dict:
        if not listen_cfg.get("bot_choose", True):
            return {"keyword": "", "vibe": "", "reason": ""}
        recent = [str(t) for t in (state.get("themes") or []) if str(t).strip()]
        return await self.mood.pick(time_label, recent)

    async def _collect(self, sources: list, keyword: str, need: int, history: list,
                       allow_repeat: bool = False) -> list:
        """按平台顺序取歌：每个平台只拉一次候选，集中去重后随机挑，凑满 need 首。

        allow_repeat=True 时忽略历史去重（歌单听遍后的保底重播）。
        """
        picked, seen = [], set() if allow_repeat else set(history)
        for source in sources:
            if len(picked) >= need:
                break
            candidates = await source.candidates(keyword)
            fresh = [t for t in candidates if t.key not in seen]
            if not fresh:
                continue
            random.shuffle(fresh)
            for track in fresh[: need - len(picked)]:
                seen.add(track.key)
                try:
                    await source.decorate(track)  # 补歌词摘句，失败不影响
                except Exception as e:
                    logger.debug(f"[每日一听] {source.display} 补全歌词失败: {e}")
                picked.append((source, track))
        return picked

    async def _save_memories(self, picked: list, feelings: dict, pretty_date: str,
                             theme: dict, listen_cfg: dict) -> str:
        """逐首写入记忆，返回记忆落点描述。"""
        memory_cfg = self.config.get("memory", {}) or {}
        if not memory_cfg.get("enable", True):
            return "已关闭"
        topics_base = [
            s.strip()
            for s in str(memory_cfg.get("topics") or "听歌,音乐").replace("，", ",").split(",")
            if s.strip()
        ]
        data_dir = StarTools.get_data_dir(PLUGIN_DIR_NAME)
        store = "LivingMemory" if get_lmem(self.context) else "本地档案"
        for idx, (_, track) in enumerate(picked, 1):
            feeling, sentiment = feelings.get(idx, ("", "neutral"))
            theme_desc = f"，主题是「{theme['keyword']}」" if theme["keyword"] else ""
            memory_text = (
                f"{pretty_date}，我在{track.platform_name}听了《{track.name}》"
                f"（{track.artist}）{theme_desc}。{feeling}"
            )
            topics = topics_base + [track.platform_name] + ([theme["keyword"]] if theme["keyword"] else [])
            where = await save_memory(self.context, data_dir, memory_cfg, memory_text, topics, sentiment)
            if where and where != store:
                store = where
        return store

    # ------------------------------------------------------------------
    # 数据文件：去重历史、状态
    # ------------------------------------------------------------------

    def _data_dir(self):
        return StarTools.get_data_dir(PLUGIN_DIR_NAME)

    def _history_path(self):
        return self._data_dir() / "history.json"

    def _load_history(self) -> list:
        """读取去重历史（旧歌放前面，超容量的先淘汰）。"""
        limit = _clamp((self.config.get("listen", {}) or {}).get("history_size"), 10, 1000, 200)
        try:
            history = json.loads(self._history_path().read_text(encoding="utf-8"))
            return [str(k) for k in history if k][-limit:]
        except (OSError, json.JSONDecodeError):
            return []

    def _save_history(self, history: list) -> None:
        limit = _clamp((self.config.get("listen", {}) or {}).get("history_size"), 10, 1000, 200)
        try:
            self._data_dir().mkdir(parents=True, exist_ok=True)
            self._history_path().write_text(
                json.dumps(history[-limit:], ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            logger.warning(f"[每日一听] 历史保存失败: {e}")

    def _state_path(self):
        return self._data_dir() / "state.json"

    def _load_state(self) -> dict:
        """运行记录：{"runs": {slot_key: ts}, "themes": [近期主题]}。"""
        try:
            state = json.loads(self._state_path().read_text(encoding="utf-8"))
            if isinstance(state, dict):
                state.setdefault("runs", {})
                state.setdefault("themes", [])
                return state
        except (OSError, json.JSONDecodeError):
            pass
        return {"runs": {}, "themes": []}

    def _save_state(self, slot_key: str, theme_keyword: str) -> None:
        state = self._load_state()
        runs = {str(k): v for k, v in (state.get("runs") or {}).items()}
        if slot_key:
            runs[slot_key] = int(time.time())
        cutoff = (datetime.now(self.scheduler.timezone) - timedelta(days=STATE_KEEP_DAYS)).date()
        runs = {k: v for k, v in runs.items() if k[:10] >= cutoff.isoformat()}
        themes = [str(t) for t in (state.get("themes") or []) if str(t).strip()]
        if theme_keyword:
            themes = (themes + [theme_keyword])[-THEME_KEEP:]
        try:
            self._data_dir().mkdir(parents=True, exist_ok=True)
            self._state_path().write_text(
                json.dumps({"runs": runs, "themes": themes}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"[每日一听] 状态保存失败: {e}")

    # ------------------------------------------------------------------
    # 指令辅助
    # ------------------------------------------------------------------

    def _toggle_notify(self, umo: str) -> str:
        """把会话加入/移出通知列表，并持久化到插件配置。"""
        cfg = self.config.setdefault("notify", {})
        sessions = set(cfg.get("sessions") or [])
        if umo in sessions:
            sessions.remove(umo)
            action = "已移出"
        else:
            sessions.add(umo)
            action = "已加入"
        cfg["sessions"] = list(sessions)
        self.config.save_config()
        return f"[每日一听] 当前会话{action}通知列表（当前共 {len(sessions)} 个）"

    async def _status_text(self) -> str:
        """汇总当前配置为一段状态文本。"""
        schedule = self.config.get("schedule", {}) or {}
        listen = self.config.get("listen", {}) or {}
        memory = self.config.get("memory", {}) or {}
        now = datetime.now(self.scheduler.timezone)

        windows_list = self._windows()
        labels = "、".join(windows.fmt_window(w) for w in windows_list) or "未配置"
        upcoming = windows.next_plan(windows_list, now)
        next_desc = f"{upcoming[0]:%m-%d %H:%M}（{windows.fmt_window(upcoming[1])}）" if upcoming else "无"
        current = windows.current_slot(windows_list, now)

        need, seconds = self._budget(listen)
        mode_desc = "按时长" if str(listen.get("mode") or "duration") == "duration" else "按首数"
        sources = build_sources(self.config)
        source_desc = "、".join(s.display for s in sources) if sources else "无（请配置歌单/关键词/歌名列表）"
        state = self._load_state()
        recent = "、".join((state.get("themes") or [])[-3:]) or "无"

        lines = [
            "🎧 每日一听 · 当前配置",
            f"⏰ 定时：{'启用' if schedule.get('enable', True) else '停用'}，"
            f"每天 {labels}（{self.scheduler.timezone}）",
            f"🕒 下一次：{next_desc}" + (
                f"；当前窗口 {windows.fmt_window(current[0])} 计划 {current[2]:%H:%M} 开始"
                if current else ""
            ),
            f"🎵 听歌：{mode_desc}，本次约 {need} 首、每首 {seconds} 秒",
            f"📻 来源（按顺序）：{source_desc}",
            f"🧠 记忆：{'启用' if memory.get('enable', True) else '停用'}，"
            f"落点 {'LivingMemory 已就绪' if get_lmem(self.context) else '本地档案（LivingMemory 未检测到）'}",
            f"🎼 最近主题：{recent}",
            f"🤖 模型：{provider_desc(self.context, self.config)}",
            f"📢 通知会话：{len((self.config.get('notify', {}) or {}).get('sessions') or [])} 个",
        ]
        return "\n".join(lines)
