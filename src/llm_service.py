"""Lazy OpenAI-compatible model access and resilient JSON parsing."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from langchain_core.language_models.model_profile import ModelProfile
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .config import ModelProvider, get_agent_model_provider, get_model_providers


class OpenAICompatibleChatModel(ChatOpenAI):
    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if payload.get("tools") == []:
            payload.pop("tools")
            payload.pop("tool_choice", None)
        return payload


@lru_cache(maxsize=16)
def _build_model(
    name: str,
    model: str,
    base_url: str | None,
    api_key: str | None,
    enable_thinking: bool | None,
    temperature: float = 0.7,
) -> ChatOpenAI:
    if not api_key:
        raise RuntimeError(f"模型供应商 {name} 尚未配置 API Key")
    profile: ModelProfile | None = None
    if base_url:
        # OpenAI-compatible endpoints often lack a built-in LangChain profile, so
        # auto structured output falls back to ToolStrategy and sends tool_choice
        # values some providers reject. Declaring structured output support keeps
        # LangChain on the provider-native path.
        profile = {"structured_output": True}
    extra_body: dict[str, Any] | None = None
    if enable_thinking is not None:
        extra_body = {"enable_thinking": enable_thinking}
    model_class = OpenAICompatibleChatModel if base_url else ChatOpenAI
    return model_class(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        profile=profile,
        extra_body=extra_body,
    )


def get_model(provider_name: str = "default", agent_name: str | None = None) -> ChatOpenAI:
    if agent_name:
        provider = get_agent_model_provider(agent_name, provider_name)
        return _build_model(
            provider.name,
            provider.model,
            provider.base_url,
            provider.api_key,
            provider.enable_thinking,
            provider.temperature,
        )

    providers = get_model_providers()
    provider: ModelProvider = providers.get(provider_name) or providers["default"]
    return _build_model(
        provider.name,
        provider.model,
        provider.base_url,
        provider.api_key,
        provider.enable_thinking,
        provider.temperature,
    )


def clear_model_cache() -> None:
    """Invalidate the ChatOpenAI instance cache (call after config changes)."""
    _build_model.cache_clear()


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content)
    return str(content)


def invoke_text(system_prompt: str, user_prompt: str, provider_name: str = "default") -> str:
    model = get_model(provider_name)
    response = model.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
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
