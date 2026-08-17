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
SHORT_TERM_MESSAGE_LIMIT = int(os.getenv("SHORT_TERM_MESSAGE_LIMIT", "100"))


@dataclass(frozen=True)
class ModelProvider:
    """One OpenAI-compatible model endpoint."""

    name: str
    model: str
    base_url: str | None
    api_key: str | None
    enable_thinking: bool | None = None

    def public_dict(self) -> dict[str, str | bool | None]:
        return {
            "name": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "configured": bool(self.api_key),
            "enable_thinking": self.enable_thinking,
        }


AGENT_MODEL_NAMES: tuple[str, ...] = (
    "turn_interpreter",
    "self_emotion",
    "memory_planner",
    "response_composer",
    "critic",
    "user_mem_recognition",
    "proactive",
)

_LEGACY_AGENT_CONFIG_ALIASES = {
    "turn_interpreter": "emotion",
    "response_composer": "prompt_composer",
    "user_mem_recognition": "reflection",
}

# Runtime overrides applied by the admin UI (model name and thinking toggle).
# Populated via apply_agent_overrides(); never written to .env.
_agent_overrides: dict[str, dict] = {}


def apply_agent_overrides(overrides: dict[str, dict]) -> None:
    """Replace the in-memory per-agent overrides (model, enable_thinking).

    Called once at app startup (from DB) and again on each admin save.
    Only keys present in AGENT_MODEL_NAMES are accepted.
    """
    global _agent_overrides
    _agent_overrides = {k: v for k, v in overrides.items() if k in AGENT_MODEL_NAMES}


def _read_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _read_bool_env(name: str, default: bool = False) -> bool:
    value = _read_env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


WORKFLOW_DEBUG = _read_bool_env("WORKFLOW_DEBUG", default=False)
CRITIC_MAX_ATTEMPTS = max(1, int(os.getenv("CRITIC_MAX_ATTEMPTS", "3")))


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
            enable_thinking=(
                _read_bool_env(str(item["enable_thinking_env"]), default=False)
                if item.get("enable_thinking_env")
                else None
            ),
        )
    return providers


def _default_provider_from_agent_env() -> ModelProvider:
    """Build the admin-visible default provider from DEFAULT_* env vars."""
    return ModelProvider(
        name="default",
        model=_read_env("DEFAULT_MODEL") or "not-configured",
        base_url=_read_env("DEFAULT_BASE_URL"),
        api_key=_read_env("DEFAULT_API_KEY"),
        enable_thinking=_read_bool_env("DEFAULT_ENABLE_THINKING", default=False),
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
    legacy_name = _LEGACY_AGENT_CONFIG_ALIASES.get(normalized)
    legacy_prefix = _agent_env_prefix(legacy_name) if legacy_name else ""
    model = _read_env(f"{prefix}_MODEL") or _read_env(f"{legacy_prefix}_MODEL")
    base_url = _read_env(f"{prefix}_BASE_URL") or _read_env(f"{legacy_prefix}_BASE_URL")
    api_key = _read_env(f"{prefix}_API_KEY") or _read_env(f"{legacy_prefix}_API_KEY")
    thinking_value = _read_env(f"{prefix}_ENABLE_THINKING")
    enable_thinking = (
        thinking_value.lower() in {"1", "true", "yes", "on"}
        if thinking_value is not None
        else _read_bool_env(f"{legacy_prefix}_ENABLE_THINKING", default=False)
    )

    # Apply runtime overrides from the admin UI (model name / enable_thinking).
    override = _agent_overrides.get(normalized) or _agent_overrides.get(legacy_name or "", {})
    if override.get("model"):
        model = override["model"]
    if "enable_thinking" in override:
        enable_thinking = bool(override["enable_thinking"])

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
        enable_thinking=enable_thinking,
    )
