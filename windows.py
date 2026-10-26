"""听歌时间窗：解析「13:00-14:00」式配置，并算出每次听歌的计划时刻。

约定：
- 一个窗口每天只触发一次；计划时刻在窗口前 80% 内随机，用「日期@窗口」做随机种子，
  同一天多次轮询得到同一结果（无需持久化随机数），进程重启也不会改变已定时刻。
- 支持跨零点窗口（如 23:00-01:00）：凌晨那一截归属前一天的计划。
- 纯函数、不依赖 AstrBot，方便单独测试与复用。
"""

import random
import re
from datetime import datetime, timedelta

_TICK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*[-~—–至]\s*(\d{1,2}):(\d{2})$")
_SPLIT_RE = re.compile(r"[\n\r,，;；、]+")
MAX_RANDOM_RATIO = 0.8  # 计划时刻只在窗口前 80% 内随机，避免贴着窗口末尾才出发


def _to_minutes(hh: str, mm: str):
    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def fmt_window(window) -> str:
    """(780, 840) → '13:00-14:00'。"""
    start, end = window
    return f"{start // 60 % 24:02d}:{start % 60:02d}-{end // 60 % 24:02d}:{end % 60:02d}"


def parse_windows(raw):
    """解析窗口配置（多行文本或列表），返回 (windows, invalid)。

    windows 为 [(start_minutes, end_minutes)]，跨零点时 end 会大于 1440。
    """
    if isinstance(raw, str):
        items = _SPLIT_RE.split(raw)
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = []

    windows, invalid = [], []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        matched = _WINDOW_RE.match(text)
        start = _to_minutes(matched.group(1), matched.group(2)) if matched else None
        end = _to_minutes(matched.group(3), matched.group(4)) if matched else None
        if start is None or end is None or start == end:
            invalid.append(text)
            continue
        if end < start:
            end += 1440  # 跨零点
        windows.append((start, end))
    return windows, invalid


def legacy_window(time_str) -> list:
    """兼容旧配置 schedule.time：'21:00' → [(1260, 1320)]。"""
    matched = _TICK_RE.match(str(time_str or "").strip())
    start = _to_minutes(matched.group(1), matched.group(2)) if matched else None
    return [(start, start + 60)] if start is not None else []


def slot_bounds(now: datetime, window):
    """当前时刻所属窗口的 (start_dt, end_dt)；不在窗口内返回 None。"""
    start, end = window
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    slot_start = day + timedelta(minutes=start)
    slot_end = day + timedelta(minutes=end)
    if now < slot_start:  # 跨零点窗口的凌晨部分：起点在昨天
        slot_start -= timedelta(days=1)
        slot_end -= timedelta(days=1)
    return (slot_start, slot_end) if slot_start <= now < slot_end else None


def slot_key(slot_start: datetime, window) -> str:
    """本次窗口的唯一键，用作「今天这个窗口跑过没有」的标记与随机种子。"""
    return f"{slot_start.date().isoformat()}@{fmt_window(window)}"


def planned_at(slot_start: datetime, slot_end: datetime, key: str) -> datetime:
    """本次窗口的计划触发时刻（当天固定，重启不变）。"""
    span = max(1, int((slot_end - slot_start).total_seconds() // 60))
    rng = random.Random(key)  # 字符串种子用 sha512，跨进程稳定
    return slot_start + timedelta(minutes=rng.randrange(0, max(1, int(span * MAX_RANDOM_RATIO))))


def current_slot(windows_list, now: datetime):
    """返回 (window, slot_key, planned_dt)；当前不在任何窗口内返回 None。"""
    for window in windows_list:
        bounds = slot_bounds(now, window)
        if not bounds:
            continue
        key = slot_key(bounds[0], window)
        return window, key, planned_at(bounds[0], bounds[1], key)
    return None


def next_plan(windows_list, now: datetime):
    """下一次计划时刻 (planned_dt, window)；没有任何窗口时返回 None。"""
    candidates = []
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for window in windows_list:
        for offset in (0, 1):
            day = day0 + timedelta(days=offset)
            slot_start = day + timedelta(minutes=window[0])
            slot_end = day + timedelta(minutes=window[1])
            key = slot_key(slot_start, window)
            planned = planned_at(slot_start, slot_end, key)
            if planned > now:
                candidates.append((planned, window))
    return min(candidates, key=lambda x: x[0]) if candidates else None
