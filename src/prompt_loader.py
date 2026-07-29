"""Load node prompts from disk so they can be reviewed without code changes."""

from functools import lru_cache

from .config import PROMPT_DIR


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"缺少提示词文件: {path}")
    return path.read_text(encoding="utf-8").strip()


def clear_prompt_cache() -> None:
    load_prompt.cache_clear()
