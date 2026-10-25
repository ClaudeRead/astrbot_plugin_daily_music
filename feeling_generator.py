"""听歌感受生成：单次 LLM 调用批量产出全部歌曲的听后感。

Token 优化（最高原则）：
    - 固定提示词前缀 + 动态歌单后缀：前缀不变以命中供应商端 prompt 缓存
    - 每天所有歌曲合并为一次调用，输出紧凑 JSON，避免逐首歌调用
    - 解析失败只重试一次，重试提示词复用同一次歌单内容
"""

import json
import re

from astrbot.api import logger

VALID_SENTIMENTS = {"positive", "neutral", "negative"}

# 固定前缀： persona 为空时整段不变，缓存命中率最高
SYSTEM_PROMPT = (
    "你是一个热爱音乐的AI伙伴，正在执行「每日一听」计划：按歌单听了几首歌，"
    "请为每首歌写一句真实自然的听后感。要求：\n"
    "1. 第一人称，像随手记下的心情，有画面感，禁止空洞套话\n"
    "2. 每句不超过 {max_chars} 字\n"
    "3. 只输出 JSON 数组，格式："
    '[{"index":1,"feeling":"...","sentiment":"positive"}]，'
    "index 对应歌曲序号，sentiment 只能是 positive、neutral 或 negative，"
    "不要输出任何其他内容\n"
)

RETRY_SUFFIX = "\n\n上一次输出无法解析，请严格只输出符合格式的 JSON 数组。"


def build_prompt(tracks: list, listen_seconds: int, max_chars: int, persona: str) -> str:
    """组装完整提示词：固定前缀在前，歌单数据在后。"""
    head = SYSTEM_PROMPT.replace("{max_chars}", str(max_chars))
    if persona.strip():
        head += f"你的性格设定：{persona.strip()}\n"
    lines = [head, "【今日听歌记录】"]
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
    items = json.loads(text[start : end + 1])
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

    def _resolve_provider(self):
        llm_cfg = self.config.get("llm", {}) or {}
        pid = str(llm_cfg.get("provider_id") or "").strip()
        if pid:
            provider = self.context.get_provider_by_id(pid)
            if provider is None:
                raise RuntimeError(f"未找到服务提供商「{pid}」，请检查插件配置")
            return provider
        provider = self.context.get_using_provider()
        if provider is None:
            raise RuntimeError("AstrBot 尚未配置任何大模型服务提供商")
        return provider

    async def _chat(self, prompt: str) -> str:
        llm_cfg = self.config.get("llm", {}) or {}
        model = str(llm_cfg.get("model") or "").strip() or None
        provider = self._resolve_provider()
        try:
            resp = await provider.text_chat(prompt=prompt, model=model)
        except Exception as e:
            # 指定的模型不属于该提供商（400 模型名错误）时，回退提供商默认模型再试一次
            if model and "model" in str(e).lower():
                logger.warning(f"[每日一听] 模型 {model} 调用失败，回退提供商默认模型: {e}")
                resp = await provider.text_chat(prompt=prompt, model=None)
            else:
                raise
        text = resp.result_chain.get_plain_text() if resp.result_chain else ""
        if not text.strip():
            raise RuntimeError("模型返回了空内容")
        return text

    async def generate(self, tracks: list, listen_seconds: int) -> dict:
        """返回 {序号: (感受, 情绪)}；失败只记录日志，不抛出。"""
        llm_cfg = self.config.get("llm", {}) or {}
        max_chars = max(10, int(llm_cfg.get("feeling_max_chars", 50) or 50))
        persona = str(llm_cfg.get("persona") or "")
        prompt = build_prompt(tracks, listen_seconds, max_chars, persona)
        for attempt in range(2):
            try:
                return parse_feelings(
                    await self._chat(prompt + RETRY_SUFFIX if attempt else prompt),
                    len(tracks),
                    max_chars,
                )
            except Exception as e:
                logger.warning(f"[每日一听] 听后感生成第 {attempt + 1} 次失败: {e}")
        return {}
