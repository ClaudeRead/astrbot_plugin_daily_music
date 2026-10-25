"""AstrBot 插件「每日一听」

让 Bot 每天按计划"自己去听歌"，并为每首歌写下听后感：
    1. 从网易云 / QQ音乐（歌单 ID 或关键词搜索）、汽水音乐（歌名列表/自定义接口）选歌
    2. 单次 LLM 调用批量生成听后感（固定前缀命中 prompt 缓存，尽量省 token）
    3. 听歌记忆与感受写入 astrbot_plugin_livingmemory 插件（未安装回退本地 JSONL）
    4. 结果摘要推送到通知会话

模块结构（便于二次开发）：
    main.py              插件入口：指令、定时调度与整体流程编排
    music_source.py      音乐来源：三平台选歌（公开接口，含 SSRF 防护的 http_util）
    feeling_generator.py 听后感生成：缓存友好提示词、单次批量调用、JSON 解析
    living_memory.py     LivingMemory 联动与本地回退
"""

import asyncio
import json
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import Plain

from .feeling_generator import FeelingGenerator
from .living_memory import get_lmem, save_memory
from .music_source import build_sources

JOB_ID = "daily_music_listen"   # 定时任务 ID
DEFAULT_TIME = "21:00"          # listen_time 为空或非法时的回退时间
DEFAULT_TZ = "Asia/Shanghai"    # 容器常为 UTC，默认按北京时间触发
PLUGIN_DIR_NAME = "astrbot_plugin_daily_music"
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _clamp(value, low, high, default) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


class DailyMusicPlugin(Star):
    """每日一听插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.generator = FeelingGenerator(context, config)
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
        """插件加载/重载时注册定时任务（WebUI 保存配置后会自动重载插件）。"""
        if not self.scheduler.running:
            self.scheduler.start()
        self._setup_schedule()
        logger.info("[每日一听] 插件已加载")

    async def terminate(self) -> None:
        """插件停用/卸载时关闭调度器。"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # ------------------------------------------------------------------
    # 定时调度
    # ------------------------------------------------------------------

    def _setup_schedule(self) -> None:
        """按配置注册/移除每日定时任务。"""
        cfg = self.config.get("schedule", {}) or {}
        if not cfg.get("enable", True):
            if self.scheduler.get_job(JOB_ID):
                self.scheduler.remove_job(JOB_ID)
            logger.info("[每日一听] 定时听歌已停用")
            return

        time_str = str(cfg.get("time") or DEFAULT_TIME).strip()
        try:
            hh, mm = time_str.split(":")
            hour, minute = int(hh), int(mm)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            logger.warning(f"[每日一听] 时间格式 {time_str!r} 无效，回退为 {DEFAULT_TIME}")
            hour, minute = 21, 0

        self.scheduler.add_job(
            self._scheduled_job,
            CronTrigger(hour=hour, minute=minute, timezone=self.scheduler.timezone),
            id=JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,  # 进程重启等错过触发点时，1 小时内补跑
            coalesce=True,            # 多次错过只补跑一次
        )
        job = self.scheduler.get_job(JOB_ID)
        next_run = str(job.next_run_time) if job and job.next_run_time else "未知"
        logger.info(
            f"[每日一听] 定时任务已排程：每日 {hour:02d}:{minute:02d}"
            f"（{self.scheduler.timezone}），下次运行：{next_run}"
        )

    async def _scheduled_job(self) -> None:
        """定时任务入口：听歌并把结果摘要推送到通知会话。"""
        summary = await self._run_listen()
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
        """每日一听：无参数立即听一轮；「状态」查看配置；「推送」将当前会话加入/移出结果通知"""

        if action in ("状态", "status"):
            yield event.plain_result(await self._status_text())
        elif action in ("推送", "通知", "notify"):
            yield event.plain_result(self._toggle_notify(event.unified_msg_origin))
        else:
            yield event.plain_result(await self._run_listen())

    # ------------------------------------------------------------------
    # 核心流程
    # ------------------------------------------------------------------

    async def _run_listen(self) -> str:
        """执行一轮完整听歌流程，返回结果摘要文本。

        流程：选歌（三平台轮流派）→ 生成听后感 → 逐首写入记忆 → 更新去重历史。
        """
        if self._lock.locked():
            return "[每日一听] 已有听歌任务正在进行，请稍候。"

        async with self._lock:
            listen_cfg = self.config.get("listen", {}) or {}
            songs_per_day = _clamp(listen_cfg.get("songs_per_day"), 1, 20, 3)
            listen_seconds = _clamp(listen_cfg.get("listen_seconds"), 10, 3600, 180)

            sources = build_sources(self.config)
            if not sources:
                return "[每日一听] 没有已配置歌曲来源的平台，请先在插件配置中填入歌单 ID / 关键词 / 歌名列表"

            history = self._load_history()
            now = datetime.now()
            pretty_date = f"{now.year}年{now.month}月{now.day}日 {WEEKDAYS[now.weekday()]}"
            lines = [f"🎧 每日一听 · {pretty_date}"]

            # 1. 选歌：三平台轮流派歌，凑满数量；某平台失败时由其他平台补位
            picked = []  # [(source, track)]
            for i in range(songs_per_day):
                order = dict.fromkeys(
                    [sources[i % len(sources)]] + list(sources)
                )  # 首选平台失败后按随机顺序补位，去重保持首选优先
                for source in order:
                    track = await source.pick(set(history))
                    if track:
                        picked.append((source, track))
                        history.append(track.key)  # 同一轮内也去重
                        break
            if not picked:
                return "\n".join(lines + ["❌ 今天没选到可听的歌曲，请检查各平台来源配置"])

            # 2. 模拟真实听歌节奏（可选，默认关闭）
            if listen_cfg.get("realtime_delay"):
                for _ in picked:
                    await asyncio.sleep(min(listen_seconds, 15))

            # 3. 单次 LLM 批量生成听后感
            try:
                feelings = await self.generator.generate(
                    [t for _, t in picked], listen_seconds
                )
            except Exception as e:
                logger.error(f"[每日一听] 听后感生成失败: {e}")
                feelings = {}

            # 4. 逐首展示并写入记忆
            memory_cfg = self.config.get("memory", {}) or {}
            memory_on = bool(memory_cfg.get("enable", True))
            topics_base = [
                s.strip()
                for s in str(memory_cfg.get("topics") or "听歌,音乐").replace("，", ",").split(",")
                if s.strip()
            ]
            data_dir = StarTools.get_data_dir(PLUGIN_DIR_NAME)
            store = "LivingMemory" if (memory_on and get_lmem(self.context)) else (
                "本地档案" if memory_on else "已关闭"
            )
            for idx, (source, track) in enumerate(picked, 1):
                feeling, sentiment = feelings.get(idx, ("", "neutral"))
                desc = f"《{track.name}》-{track.artist}：{feeling}" if feeling else \
                       f"《{track.name}》-{track.artist}（感受未生成）"
                lines.append(f"{idx}. [{track.platform_name}] {desc}")
                if not memory_on:
                    continue
                memory_text = f"{pretty_date}，我用{track.platform_name}听了《{track.name}》（{track.artist}）。{feeling}"
                where = await save_memory(
                    self.context, data_dir, memory_cfg, memory_text,
                    topics_base + [track.platform_name], sentiment,
                )
                if where and where != store:
                    store = f"{where}"
            lines.append(f"🧠 记忆落点：{store}")

            # 5. 更新去重历史
            history.extend(t.key for _, t in picked)
            self._save_history(history)
            logger.info("[每日一听] 本次结果：\n" + "\n".join(lines))
            return "\n".join(lines)

    # ------------------------------------------------------------------
    # 去重历史与指令辅助
    # ------------------------------------------------------------------

    def _history_path(self):
        return StarTools.get_data_dir(PLUGIN_DIR_NAME) / "history.json"

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
            self._history_path().write_text(
                json.dumps(history[-limit:], ensure_ascii=False), encoding="utf-8"
            )
        except OSError as e:
            logger.warning(f"[每日一听] 历史保存失败: {e}")

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

        job = self.scheduler.get_job(JOB_ID)
        next_run = str(job.next_run_time) if job and job.next_run_time else "未排程"

        provider_desc = "未配置"
        try:
            meta = self.generator._resolve_provider().meta()
            model = str((self.config.get("llm", {}) or {}).get("model") or "").strip() or meta.model or "默认"
            provider_desc = f"{meta.id}（{model}）"
        except Exception as e:
            provider_desc = f"解析失败：{e}"

        sources = build_sources(self.config)
        source_desc = "、".join(s.display for s in sources) if sources else "无（请配置歌单/关键词/歌名列表）"
        lines = [
            "🎧 每日一听 · 当前配置",
            f"⏰ 定时：{'启用' if schedule.get('enable', True) else '停用'}，"
            f"每日 {schedule.get('time') or DEFAULT_TIME}"
            f"（{self.scheduler.timezone}，下次：{next_run}）",
            f"🎵 听歌：每天 {listen.get('songs_per_day', 3)} 首，每首 {listen.get('listen_seconds', 180)} 秒",
            f"📻 来源：{source_desc}",
            f"🤖 模型：{provider_desc}",
            f"🧠 记忆：{'启用' if memory.get('enable', True) else '停用'}，"
            f"落点 {'LivingMemory 已就绪' if get_lmem(self.context) else '本地档案（LivingMemory 未检测到）'}",
            f"📢 通知会话：{len((self.config.get('notify', {}) or {}).get('sessions') or [])} 个",
        ]
        return "\n".join(lines)
