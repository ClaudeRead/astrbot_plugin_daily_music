"""每日一听离线自测（不联网、不依赖 AstrBot 运行时）。

运行：在仓库根目录执行 `python tests/test_daily_music.py`
"""

import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN_DIR = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------- AstrBot 桩
class _SilentLogger:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _install_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _SilentLogger()
    api.AstrBotConfig = dict

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.MessageChain = lambda chain=None: None

    class _Filter:
        class PermissionType:
            ADMIN = "admin"

        class EventMessageType:
            ALL = "all"

        @staticmethod
        def command(*a, **k):
            return lambda fn: fn

        @staticmethod
        def permission_type(*a, **k):
            return lambda fn: fn

        @staticmethod
        def event_message_type(*a, **k):
            return lambda fn: fn

    event.filter = _Filter()

    star = types.ModuleType("astrbot.api.star")
    star.Context = object

    class _Star:
        def __init__(self, context):
            self.context = context

    star.Star = _Star
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: Path(".") / name)

    core = types.ModuleType("astrbot.core")
    message = types.ModuleType("astrbot.core.message")
    components = types.ModuleType("astrbot.core.message.components")
    components.Plain = lambda text: text

    # apscheduler（仅运行期依赖，桩掉即可）
    sched = types.ModuleType("apscheduler")
    sched_schedulers = types.ModuleType("apscheduler.schedulers")
    sched_asyncio = types.ModuleType("apscheduler.schedulers.asyncio")
    sched_triggers = types.ModuleType("apscheduler.triggers")
    sched_interval = types.ModuleType("apscheduler.triggers.interval")

    class _Scheduler:
        def __init__(self, **kw):
            self.timezone = kw.get("timezone")
            self.jobs = {}

        def start(self):
            pass

        def shutdown(self, *a, **k):
            pass

        def add_job(self, *a, **k):
            pass

        def get_job(self, job_id):
            return self.jobs.get(job_id)

        def remove_job(self, job_id):
            self.jobs.pop(job_id, None)

    class _IntervalTrigger:
        def __init__(self, **kw):
            self.kw = kw

    sched_asyncio.AsyncIOScheduler = _Scheduler
    sched_interval.IntervalTrigger = _IntervalTrigger

    for name, mod in {
        "apscheduler": sched,
        "apscheduler.schedulers": sched_schedulers,
        "apscheduler.schedulers.asyncio": sched_asyncio,
        "apscheduler.triggers": sched_triggers,
        "apscheduler.triggers.interval": sched_interval,
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.core": core,
        "astrbot.core.message": message,
        "astrbot.core.message.components": components,
    }.items():
        sys.modules[name] = mod


def _install_pkg():
    """把插件目录注册成命名空间包 dm，便于相对导入。"""
    pkg = types.ModuleType("dm")
    pkg.__path__ = [str(PLUGIN_DIR)]
    sys.modules["dm"] = pkg


_install_stubs()
_install_pkg()

from dm import windows  # noqa: E402
from dm import share  # noqa: E402
from dm.feeling_generator import (  # noqa: E402
    SYSTEM_PROMPT as FEELING_SYSTEM, build_user_prompt, parse_feelings,
)
from dm.llm_client import configured_model  # noqa: E402
from dm.main import compute_budget  # noqa: E402
from dm.mood import SYSTEM_PROMPT as MOOD_SYSTEM, build_user_prompt as mood_prompt, parse_theme  # noqa: E402
from dm.music_source import QishuiSource, Track, build_sources  # noqa: E402

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond), detail))


# ---------------------------------------------------------------- 时间段
ws, bad = windows.parse_windows("13:00-14:00\n18:00-21:00")
check("时间段解析两条", len(ws) == 2 and not bad, str(ws))
check("时间段文本", windows.fmt_window(ws[0]) == "13:00-14:00", windows.fmt_window(ws[0]))

ws2, bad2 = windows.parse_windows("25:00-26:00\nabc\n13:00-13:00\n08:00-09:00")
check("非法项被忽略", len(ws2) == 1 and len(bad2) == 3, f"{ws2} {bad2}")

cross, _ = windows.parse_windows("23:00-01:00")
check("跨零点归一化", cross == [(1380, 1500)], str(cross))
early = datetime(2026, 10, 26, 0, 30, tzinfo=TZ)
slot = windows.current_slot(cross, early)
check("跨零点凌晨命中前一天", slot is not None and slot[1].startswith("2026-10-25@"), str(slot and slot[1]))
check("跨零点计划时刻落在窗口内",
      slot is not None and datetime(2026, 10, 25, 23, 0, tzinfo=TZ) <= slot[2] < datetime(2026, 10, 26, 0, 48, tzinfo=TZ),
      str(slot and slot[2]))

midday = datetime(2026, 10, 26, 13, 20, tzinfo=TZ)
slot2 = windows.current_slot(ws, midday)
check("窗口内命中", slot2 is not None and slot2[1] == "2026-10-26@13:00-14:00", str(slot2 and slot2[1]))
check("窗口内计划可复现", windows.current_slot(ws, midday)[2] == slot2[2])
check("窗口外不命中", windows.current_slot(ws, datetime(2026, 10, 26, 15, 0, tzinfo=TZ)) is None)
check("计划时刻早于窗口结束", slot2[2] < datetime(2026, 10, 26, 13, 48, tzinfo=TZ), str(slot2[2]))

nxt = windows.next_plan(ws, datetime(2026, 10, 26, 14, 30, tzinfo=TZ))
check("下一次计划是傍晚窗口", nxt is not None and windows.fmt_window(nxt[1]) == "18:00-21:00", str(nxt and nxt[0]))
check("兼容旧配置", windows.legacy_window("21:00") == [(1260, 1320)], str(windows.legacy_window("21:00")))

# ---------------------------------------------------------------- 主题
theme = parse_theme('好的：\n```json\n{"keyword":"  雨夜·爵士  ","vibe":"松弛","reason":"适合傍晚"}\n```')
check("主题关键词清洗", theme["keyword"] == "雨夜爵士", theme["keyword"])
check("主题字段完整", theme["vibe"] == "松弛" and theme["reason"] == "适合傍晚")
try:
    parse_theme('{"vibe":"x"}')
    check("空关键词抛错", False)
except ValueError:
    check("空关键词抛错", True)
check("主题提示词静态", "{persona}" not in MOOD_SYSTEM and "{time}" not in MOOD_SYSTEM)
mp = mood_prompt("文艺", "13:00-14:00", ["夏日", "雨夜"])
check("主题提示词含人格与时段", "文艺" in mp and "13:00-14:00" in mp and "雨夜" in mp, mp)

# ---------------------------------------------------------------- 听后感
tracks = [
    Track(platform="qishui", platform_name="汽水音乐", name="晴天", artist="周杰伦", album="叶惠美", lyric_line="故事的小黄花"),
    Track(platform="qishui", platform_name="汽水音乐", name="起风了", artist="买辣椒也用券"),
]
p1 = build_user_prompt(tracks, 150, 50, "文艺", "安静")
check("听后感提示词含心情", "【当下心情】安静" in p1)
p2 = build_user_prompt(tracks, 150, 50, "文艺")
check("无心情时不输出该行", "【当下心情】" not in p2, p2)
check("听后感提示词含歌单", "《晴天》-周杰伦" in p1 and "歌词摘句「故事的小黄花」" in p1 and "听了150秒" in p1)
check("听后感系统提示词静态", "{max_chars}" not in FEELING_SYSTEM and "{persona}" not in FEELING_SYSTEM)
p3 = build_user_prompt(tracks, 150, 50, "文艺", heading="【朋友分享的歌单】")
check("听后感提示词支持自定义标题", "【朋友分享的歌单】" in p3 and "【今日听歌记录】" not in p3, p3)
p4 = build_user_prompt(tracks, 150, 50, "文艺")
check("听后感提示词默认标题", "【今日听歌记录】" in p4, p4)

feel = parse_feelings('[{"index":1,"feeling":"像午后","sentiment":"Positive"},{"index":3,"feeling":"越界"}]', 2, 50)
check("情绪值归一化", feel[1] == ("像午后", "positive"), str(feel))
check("越界序号被丢弃", 3 not in feel)
try:
    parse_feelings("没有数组", 2, 50)
    check("无数组抛错", False)
except ValueError:
    check("无数组抛错", True)

# ---------------------------------------------------------------- 听歌量换算
check("按时长 5 分钟 → 2 首 150 秒", compute_budget({"duration_minutes": 5}) == (2, 150), str(compute_budget({"duration_minutes": 5})))
check("按时长 30 分钟 → 9 首 200 秒", compute_budget({"duration_minutes": 30}) == (9, 200), str(compute_budget({"duration_minutes": 30})))
check("按首数 3 首 180 秒", compute_budget({"mode": "count", "songs_per_day": 3, "listen_seconds": 180}) == (3, 180))
check("非法模式回落按时长", compute_budget({"mode": "xxx", "duration_minutes": 10})[0] == 3, str(compute_budget({"mode": "xxx", "duration_minutes": 10})))
check("最多 12 首", compute_budget({"mode": "count", "songs_per_day": 99})[0] == 12)

# ---------------------------------------------------------------- 音乐来源
qishui_cfg = {"qishui": {"song_list": "晴天 - 周杰伦\n起风了 - 买辣椒也用券\n夜曲 - 周杰伦"}, "listen": {"platforms": ["qishui"]}}
src = QishuiSource(qishui_cfg)
check("汽水来源可用", src.enabled())
hits = src._from_text("晴天")
check("汽水按主题过滤", [t.name for t in hits] == ["晴天"], str([t.name for t in hits]))
check("汽水主题不命中则全留", len(src._from_text("赛博朋克")) == 3)
check("来源按配置顺序构建", [s.key for s in build_sources(qishui_cfg)] == ["qishui"])
check("平台未启用则跳过", build_sources({"listen": {"platforms": ["qq", "netease"]}}) == [])
check("模型留空返回 None", configured_model({"llm": {"model": "  "}}) is None)
check("手填模型返回字符串", configured_model({"llm": {"model": "deepseek-flash"}}) == "deepseek-flash")

# ---------------------------------------------------------------- 选歌保底
class _StubSource:
    display = "桩平台"

    async def candidates(self, keyword=""):
        return [Track(platform="qishui", platform_name="汽水音乐", name="一直很安静", artist="迷梦小羊")]

    async def decorate(self, track):
        return None


_collect = __import__("dm.main", fromlist=["DailyMusicPlugin"]).DailyMusicPlugin._collect


class _Fake:
    pass


async def _run_collect(history, allow_repeat):
    return await _collect(_Fake(), [_StubSource()], "", 1, history, allow_repeat=allow_repeat)


import asyncio  # noqa: E402

hist = ["qishui:一直很安静:迷梦小羊"]
check("听过的不再听", asyncio.run(_run_collect(hist, False)) == [])
check("歌单听遍后重播保底", len(asyncio.run(_run_collect(hist, True))) == 1)

# ---------------------------------------------------------------- 分享链接识别
link = share.parse_share_link("听听这首 https://music.163.com/song?id=347230 咋样")
check("网易云单曲链接", link is not None and link.platform == "netease" and link.kind == "song" and link.sid == "347230", str(link))
link = share.parse_share_link("https://music.163.com/#/playlist?id=3778678")
check("网易云歌单链接", link is not None and link.platform == "netease" and link.kind == "playlist" and link.sid == "3778678", str(link))
link = share.parse_share_link("https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV")
check("QQ单曲链接", link is not None and link.platform == "qq" and link.kind == "song" and link.sid == "0039MnYb0qxYhV", str(link))
link = share.parse_share_link("https://y.qq.com/n/ryqq/playlist/123456")
check("QQ歌单链接", link is not None and link.platform == "qq" and link.kind == "playlist" and link.sid == "123456", str(link))
link = share.parse_share_link("https://soda.douyin.com/abcdef")
check("汽水链接", link is not None and link.platform == "qishui", str(link))
check("无链接返回 None", share.parse_share_link("今天天气不错") is None)
check("歌单链接生成", share.playlist_url("netease", "3778678") == "https://music.163.com/playlist?id=3778678")

# ---------------------------------------------------------------- 结果
failed = [c for c in checks if not c[1]]
for name, ok, detail in checks:
    print(f"{'[OK]' if ok else '[FAIL]'} {name}" + (f"  [{detail}]" if detail and not ok else ""))
print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
sys.exit(1 if failed else 0)
