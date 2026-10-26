"""LLM 调用共用：解析提供商 + 发起一次「静态 system + 可变 user」的文本请求。

缓存优先：system_prompt 由调用方保证逐字不变，所有随配置/时间变化的内容都放进用户消息，
这样改人格、改字数、换歌单只会让尾部未命中，不会让整段前缀失效。
"""

from astrbot.api import logger


def resolve_provider(context, config):
    """按 llm.provider_id 取提供商；留空则用 AstrBot 全局默认。"""
    llm_cfg = config.get("llm", {}) or {}
    provider_id = str(llm_cfg.get("provider_id") or "").strip()
    if provider_id:
        provider = context.get_provider_by_id(provider_id)
        if provider is None:
            raise RuntimeError(f"未找到服务提供商「{provider_id}」，请检查插件配置")
        return provider
    provider = context.get_using_provider()
    if provider is None:
        raise RuntimeError("AstrBot 尚未配置任何大模型服务提供商")
    return provider


def configured_model(config):
    """插件配置里手填的模型名；留空返回 None（用提供商默认模型）。"""
    return str((config.get("llm", {}) or {}).get("model") or "").strip() or None


async def chat_text(context, config, system_prompt: str, user_prompt: str,
                    retries: int = 1) -> str:
    """调用提供商并返回纯文本；空内容抛异常。"""
    provider = resolve_provider(context, config)
    model = configured_model(config)
    try:
        resp = await provider.text_chat(
            prompt=user_prompt,
            system_prompt=system_prompt,
            model=model,
            request_max_retries=retries,
        )
    except Exception as exc:
        # 手填的模型名不属于该提供商（400 模型名错误）时，回退提供商默认模型再试一次
        if model and "model" in str(exc).lower():
            logger.warning(f"[每日一听] 模型 {model} 调用失败，回退默认模型: {exc}")
            resp = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                model=None,
                request_max_retries=retries,
            )
        else:
            raise
    text = resp.result_chain.get_plain_text() if resp.result_chain else ""
    if not text.strip():
        raise RuntimeError("模型返回了空内容")
    return text


def provider_desc(context, config) -> str:
    """给状态文本用的一行提供商描述。"""
    try:
        meta = resolve_provider(context, config).meta()
        model = configured_model(config) or meta.model or "默认"
        return f"{meta.id}（{model}）"
    except Exception as exc:
        return f"解析失败：{exc}"
