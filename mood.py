"""歌单主题：让 Bot 按自己的人格设定决定这段时间听什么。

缓存优先：SYSTEM_PROMPT 逐字不变；人格、时段、最近听过的主题都放在用户消息里，
所以改人格或换配置只会影响尾部，不会让整段前缀失效。
"""

import json
import re

from astrbot.api import logger

from .llm_client import chat_text

RETRY_SUFFIX = "\n（上次输出格式不对，请只输出一个 JSON 对象，不要任何解释或代码块标记。）"

SYSTEM_PROMPT = (
    "你在扮演一个真实的人，正准备戴上耳机听一会儿歌。请根据你的性格设定和当前时段，"
    "为这段时间挑一个歌单主题。要求：\n"
    "1. 主题要贴合你的性格和当下的时段氛围，像是你自己主动想听的东西，而不是随机堆词\n"
    "2. 避开最近已经听过的主题，换点新鲜的\n"
    "3. 只输出一个 JSON 对象，格式："
    '{"keyword": "音乐搜索关键词", "vibe": "一句不超过 20 字的心情", '
    '"reason": "不超过 30 字的选择理由"}\n'
    "其中 keyword 是能直接在音乐平台搜到歌的词，2-8 个汉字，不带标点、不带引号、"
    "不要出现「歌单」「音乐」这类空泛的词；不要输出任何多余内容。"
)

EMPTY_THEME = {"keyword": "", "vibe": "", "reason": ""}


def build_user_prompt(persona: str, time_label: str, recent: list) -> str:
    """用户消息：所有会变的内容都放这里。"""
    lines = []
    if str(persona or "").strip():
        lines.append(f"【性格设定】{str(persona).strip()}")
    else:
        lines.append("【性格设定】没有特别设定，就按一个温和、好奇心强、喜欢尝试新东西的人来")
    lines.append(f"【当前时段】{time_label}")
    if recent:
        lines.append("【最近听过】" + "、".join(str(x) for x in recent[-5:]))
    lines.append("现在给我这次听歌的歌单主题。")
    return "\n".join(lines)


def parse_theme(text: str) -> dict:
    """解析模型输出，返回 {"keyword","vibe","reason"}；格式非法抛 ValueError。"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("输出中找不到 JSON 对象")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("输出不是 JSON 对象")

    # 搜索词只保留汉字/字母/数字：模型爱加标点、间隔号、书名号，留着会搜不到歌
    keyword = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(data.get("keyword") or ""))[:12]
    if not keyword:
        raise ValueError("主题关键词为空")
    return {
        "keyword": keyword,
        "vibe": str(data.get("vibe") or "").strip()[:40],
        "reason": str(data.get("reason") or "").strip()[:60],
    }


class MoodPicker:
    """按人格挑主题；失败时返回空 keyword，由调用方回退到配置的关键词/歌单。"""

    def __init__(self, context, config):
        self.context = context
        self.config = config

    async def pick(self, time_label: str, recent: list) -> dict:
        persona = str((self.config.get("llm", {}) or {}).get("persona") or "")
        prompt = build_user_prompt(persona, time_label, recent)
        for attempt in range(2):
            try:
                text = await chat_text(
                    self.context, self.config, SYSTEM_PROMPT,
                    prompt + (RETRY_SUFFIX if attempt else ""),
                )
                theme = parse_theme(text)
                logger.info(f"[每日一听] 今天的歌单主题：{theme['keyword']}（{theme['vibe'] or '无心情描述'}）")
                return theme
            except Exception as exc:
                logger.warning(f"[每日一听] 选主题第 {attempt + 1} 次失败: {exc}")
        return dict(EMPTY_THEME)
