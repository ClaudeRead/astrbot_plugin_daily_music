"""听歌感受生成：单次 LLM 调用批量产出全部歌曲的听后感。

缓存优先（本插件的最高原则）：
    - 固定的系统提示词逐字不变，作为供应商端可缓存的稳定前缀
    - 会变的内容（歌单、字数限制、性格、当下心情）一律放在用户消息里，永远排在稳定前缀之后
    - 因此改 persona / feeling_max_chars 不会让缓存整体失效，只会让尾部未命中
    - 每天所有歌曲合并为一次调用，输出紧凑 JSON，避免逐首歌调用
    - 解析失败只重试一次，重试只在用户消息尾部追加提示，系统前缀保持不变
"""

import json
import re

from astrbot.api import logger

from .llm_client import chat_text

VALID_SENTIMENTS = {"positive", "neutral", "negative"}

# 稳定前缀：这段文字不要随插件配置变化，否则每次改动都会让供应商端的缓存整体失效。
SYSTEM_PROMPT = (
    "你是一个热爱音乐的AI伙伴，正在执行「每日一听」计划：按歌单听了几首歌，"
    "请为每首歌写一句真实自然的听后感。要求：\n"
    "1. 第一人称，像随手记下的心情，有画面感，禁止空洞套话\n"
    "2. 每句字数不超过用户消息中给出的限制\n"
    "3. 只输出 JSON 数组，格式："
    '[{"index":1,"feeling":"...","sentiment":"positive"}]，'
    "index 对应歌曲序号，sentiment 只能是 positive、neutral 或 negative，"
    "不要输出任何其他内容"
)

RETRY_SUFFIX = "\n\n上一次输出无法解析，请严格只输出符合格式的 JSON 数组。"


def build_user_prompt(tracks: list, listen_seconds: int, max_chars: int,
                      persona: str, vibe: str = "", heading: str = "【今日听歌记录】") -> str:
    """组装用户消息：所有可变内容都在这里，按「先限制、后数据」排列。"""
    lines = [f"【输出限制】每句不超过 {max_chars} 字。"]
    if str(persona or "").strip():
        lines.append(f"【性格设定】{str(persona).strip()}")
    if str(vibe or "").strip():
        lines.append(f"【当下心情】{str(vibe).strip()}")
    lines.append(heading)
    for i, t in enumerate(tracks, 1):
        parts = [f"{i}.[{t.platform_name}]《{t.name}》-{t.artist}"]
        if t.album:
            parts.append(f"专辑《{t.album}》")
        if t.lyric_line:
            parts.append(f"歌词摘句「{t.lyric_line}」")
        parts.append(f"听了{listen_seconds}秒")
        lines.append("｜".join(parts))
    lines.append("请按序号为每首歌输出听后感。")
    return "\n".join(lines)


def parse_feelings(text: str, count: int, max_chars: int) -> dict:
    """从模型输出解析 {序号: (感受, 情绪)}；解析失败抛异常由调用方重试。"""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("输出中找不到 JSON 数组")
    items = json.loads(text[start:end + 1])
    feelings = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index", 0))
        except (TypeError, ValueError):
            continue
        if not (1 <= idx <= count):
            continue
        feeling = re.sub(r"\s+", " ", str(item.get("feeling") or "")).strip()[:max_chars]
        sentiment = str(item.get("sentiment") or "neutral").strip().lower()
        if sentiment not in VALID_SENTIMENTS:
            sentiment = "neutral"
        feelings[idx] = (feeling, sentiment)
    if not feelings:
        raise ValueError("JSON 数组中没有有效感受")
    return feelings


class FeelingGenerator:
    """调用 LLM 为一批歌曲生成听后感。"""

    def __init__(self, context, config):
        self.context = context
        self.config = config

    async def generate(self, tracks: list, listen_seconds: int, vibe: str = "",
                       heading: str = "【今日听歌记录】") -> dict:
        """返回 {序号: (感受, 情绪)}；失败只记录日志，不抛出。"""
        llm_cfg = self.config.get("llm", {}) or {}
        max_chars = max(10, int(llm_cfg.get("feeling_max_chars", 50) or 50))
        persona = str(llm_cfg.get("persona") or "")
        user_prompt = build_user_prompt(tracks, listen_seconds, max_chars, persona, vibe, heading)
        for attempt in range(2):
            prompt = user_prompt + RETRY_SUFFIX if attempt else user_prompt
            try:
                text = await chat_text(self.context, self.config, SYSTEM_PROMPT, prompt)
                return parse_feelings(text, len(tracks), max_chars)
            except Exception as e:
                logger.warning(f"[每日一听] 听后感生成第 {attempt + 1} 次失败: {e}")
        return {}
