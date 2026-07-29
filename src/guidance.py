"""Readable Markdown guidance retrieval using local keyword scoring."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import GUIDANCE_DIR


class GuidanceRetriever:
    def __init__(self, root: Path | str = GUIDANCE_DIR):
        self.root = Path(root)

    def list_documents(self) -> list[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True)
        documents = []
        for path in sorted(self.root.rglob("*.md")):
            documents.append({
                "name": path.relative_to(self.root).as_posix(),
                "size": path.stat().st_size,
                "updated_at": path.stat().st_mtime,
            })
        return documents

    @staticmethod
    def _tokens(text: str) -> set[str]:
        lowered = text.lower()
        words = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", lowered))
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
        words.update(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
        return words

    def search(self, query: str, limit: int = 5, chunk_size: int = 900) -> list[dict[str, str | int]]:
        query_tokens = self._tokens(query)
        chunks: list[dict[str, str | int]] = []
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.rglob("*.md"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            sections = [section.strip() for section in re.split(r"(?=^#{1,4}\s)", text, flags=re.MULTILINE) if section.strip()]
            for section in sections or [text]:
                for start in range(0, len(section), chunk_size):
                    content = section[start:start + chunk_size]
                    score = len(query_tokens & self._tokens(content))
                    if score:
                        chunks.append({
                            "document": path.relative_to(self.root).as_posix(),
                            "content": content,
                            "score": score,
                        })
        return sorted(chunks, key=lambda item: int(item["score"]), reverse=True)[:limit]


guidance_retriever = GuidanceRetriever()
