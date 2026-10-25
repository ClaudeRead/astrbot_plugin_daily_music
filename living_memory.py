"""LivingMemory 联动：把听歌记忆直接写入 astrbot_plugin_livingmemory。

复用其 memory_processor 标准化存储格式、memory_engine.add_memory 落库，
不经过 LLM 工具调用，零额外 token。未安装/未就绪时回退写本地 JSONL。
"""

import json
import time
from pathlib import Path

from astrbot.api import logger

LMEM_PLUGIN_NAME = "astrbot_plugin_livingmemory"
GLOBAL_SCOPE = "livingmemory:global"


def get_lmem(context):
    """获取 LivingMemory 插件实例（需已初始化 memory_engine），否则返回 None。"""
    try:
        metadata = context.get_registered_star(LMEM_PLUGIN_NAME)
        star = getattr(metadata, "star_cls", None) if metadata else None
        initializer = getattr(star, "initializer", None) if star else None
        if (
            initializer
            and getattr(initializer, "memory_engine", None)
            and getattr(initializer, "memory_processor", None)
        ):
            return star
    except Exception as e:
        logger.debug(f"[每日一听] 检测 LivingMemory 失败: {e}")
    return None


async def save_memory(context, data_dir: Path, config: dict, memory_text: str,
                      topics: list, sentiment: str) -> str:
    """写入一条听歌记忆。返回落点说明，供结果摘要展示。"""
    if not memory_text.strip():
        return ""
    importance = max(0.0, min(1.0, float(config.get("importance", 0.6) or 0.6)))
    scope = str(config.get("scope") or "").strip() or GLOBAL_SCOPE

    star = get_lmem(context) if config.get("enable", True) else None
    if star is None:
        return _save_local(data_dir, memory_text, topics, sentiment, importance)

    try:
        engine = star.initializer.memory_engine
        processor = star.initializer.memory_processor
        content, metadata, normalized = processor.build_memory_from_structured_data(
            structured_data={
                "summary": memory_text,
                "topics": topics[:5],
                "key_facts": [],
                "sentiment": sentiment,
                "importance": importance,
            },
            is_group_chat=False,
            fallback_excerpt=memory_text,
        )
        metadata["memory_origin"] = "daily_music"
        metadata["source_session_id"] = scope
        await engine.add_memory(
            content=content,
            session_id=scope,
            persona_id=None,
            importance=normalized,
            metadata=metadata,
        )
        return "LivingMemory"
    except Exception as e:
        logger.error(f"[每日一听] 写入 LivingMemory 失败，回退本地: {e}")
        return _save_local(data_dir, memory_text, topics, sentiment, importance)


def _save_local(data_dir: Path, memory_text: str, topics: list,
                sentiment: str, importance: float) -> str:
    """本地回退：追加到 memories_fallback.jsonl。"""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "memory": memory_text,
            "topics": topics,
            "sentiment": sentiment,
            "importance": importance,
        }
        with open(data_dir / "memories_fallback.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return "本地档案"
    except OSError as e:
        logger.error(f"[每日一听] 本地记忆保存失败: {e}")
        return ""
