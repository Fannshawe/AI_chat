"""Application paths and model-provider configuration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent  # project root (one level above src/)
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("CHAT_DATA_DIR", BASE_DIR / "data")).resolve()
DATABASE_PATH = DATA_DIR / "chat.db"
UPLOAD_DIR = DATA_DIR / "uploads"
PROMPT_DIR = BASE_DIR / "prompt"
GUIDANCE_DIR = BASE_DIR / "guidance_docs"
PERSONA_PATH = Path(os.getenv("PERSONA_PATH", DATA_DIR / "persona.yaml")).resolve()
STATIC_DIR = BASE_DIR / "static"
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))
SHORT_TERM_MESSAGE_LIMIT = int(os.getenv("SHORT_TERM_MESSAGE_LIMIT", "100"))


@dataclass(frozen=True)
class ModelProvider:
    """One OpenAI-compatible model endpoint."""

    name: str
    model: str
    base_url: str | None
    api_key: str | None

    def public_dict(self) -> dict[str, str | bool | None]:
        return {
            "name": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "configured": bool(self.api_key),
        }


AGENT_MODEL_NAMES: tuple[str, ...] = (
    "emotion",
    "ai_emotion",
    "relationship",
    "memory_planner",
    "dialogue_composer",
    "prompt_composer",
    "critic",
    "reflection",
    "proactive",
)


def _read_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _agent_env_prefix(agent_name: str) -> str:
    return f"AGENT_{agent_name.upper()}"


def _extra_providers() -> dict[str, ModelProvider]:
    """Load optional providers without persisting secrets in SQLite.

    MODEL_PROVIDERS_JSON example:
    {"deepseek":{"model":"deepseek-chat","base_url":"https://.../v1",
    "api_key_env":"DEEPSEEK_API_KEY"}}
    """
    raw = os.getenv("MODEL_PROVIDERS_JSON", "").strip()
    if not raw:
        return {}
    try:
        definitions = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    providers: dict[str, ModelProvider] = {}
    if not isinstance(definitions, dict):
        return providers
    for name, item in definitions.items():
        if not isinstance(item, dict) or not item.get("model"):
            continue
        api_key_env = str(item.get("api_key_env", "")).strip()
        providers[str(name)] = ModelProvider(
            name=str(name),
            model=str(item["model"]),
            base_url=str(item["base_url"]) if item.get("base_url") else None,
            api_key=os.getenv(api_key_env) if api_key_env else None,
        )
    return providers


def _default_provider_from_agent_env() -> ModelProvider:
    """Build the admin-visible default provider from per-agent settings."""
    return ModelProvider(
        name="default",
        model=_read_env("AGENT_PROMPT_COMPOSER_MODEL") or "not-configured",
        base_url=_read_env("AGENT_PROMPT_COMPOSER_BASE_URL"),
        api_key=_read_env("AGENT_PROMPT_COMPOSER_API_KEY"),
    )


def get_model_providers() -> dict[str, ModelProvider]:
    providers = {"default": _default_provider_from_agent_env()}
    providers.update(_extra_providers())
    return providers


def get_agent_model_provider(agent_name: str, provider_name: str = "default") -> ModelProvider:
    """Resolve one agent's dedicated model config from .env.

    Required env vars per agent:
    - AGENT_<NAME>_MODEL
    - AGENT_<NAME>_BASE_URL
    - AGENT_<NAME>_API_KEY
    """
    normalized = agent_name.strip()
    if normalized not in AGENT_MODEL_NAMES:
        raise KeyError(f"未知 Agent: {agent_name}")

    prefix = _agent_env_prefix(normalized)
    model = _read_env(f"{prefix}_MODEL")
    base_url = _read_env(f"{prefix}_BASE_URL")
    api_key = _read_env(f"{prefix}_API_KEY")

    missing = [
        key
        for key, value in (
            (f"{prefix}_MODEL", model),
            (f"{prefix}_BASE_URL", base_url),
            (f"{prefix}_API_KEY", api_key),
        )
        if value is None
    ]
    if missing:
        raise RuntimeError(f"{normalized} 的独立模型配置缺失: {', '.join(missing)}")

    return ModelProvider(
        name=f"{provider_name}:{normalized}",
        model=model,
        base_url=base_url,
        api_key=api_key,
    )
