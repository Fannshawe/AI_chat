"""Lazy OpenAI-compatible model access and resilient JSON parsing."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .config import ModelProvider, get_agent_model_provider, get_model_providers


@lru_cache(maxsize=16)
def _build_model(name: str, model: str, base_url: str | None, api_key: str | None) -> ChatOpenAI:
    if not api_key:
        raise RuntimeError(f"模型供应商 {name} 尚未配置 API Key")
    return ChatOpenAI(model=model, api_key=api_key, base_url=base_url, temperature=0.7)


def get_model(provider_name: str = "default", agent_name: str | None = None) -> ChatOpenAI:
    if agent_name:
        provider = get_agent_model_provider(agent_name, provider_name)
        return _build_model(provider.name, provider.model, provider.base_url, provider.api_key)

    providers = get_model_providers()
    provider: ModelProvider = providers.get(provider_name) or providers["default"]
    return _build_model(provider.name, provider.model, provider.base_url, provider.api_key)


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content)
    return str(content)


def invoke_text(system_prompt: str, user_prompt: str, provider_name: str = "default", images: list[str] | None = None) -> str:
    model = get_model(provider_name)
    human_content: str | list[dict[str, Any]]
    if images:
        human_content = [{"type": "text", "text": user_prompt}]
        human_content.extend({"type": "image_url", "image_url": {"url": image}} for image in images)
    else:
        human_content = user_prompt
    response = model.invoke([SystemMessage(content=system_prompt), HumanMessage(content=human_content)])
    return content_to_text(response.content).strip()


def parse_json_response(text: str, fallback: Any) -> Any:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    return fallback
