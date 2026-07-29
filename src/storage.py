"""聊天应用的 SQLite 持久化层。

该模块统一管理所有数据库访问，让上层（API 与图工作流）只关注业务流程，
无需关心 SQL 与连接细节。

设计说明：
- 单个 SQLite 数据库保存联系人、消息、人格、记忆、情绪、关系状态、
    生成快照以及键值设置。
- JSON 结构字段以 TEXT 形式落库，在读写边界做序列化/反序列化。
- 每个公开方法都通过 connect() 使用短事务，避免连接生命周期泄漏到调用方。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .config import DATABASE_PATH, UPLOAD_DIR


RELATIONSHIP_LEVELS = ["陌生人", "认识", "朋友", "暧昧", "恋人", "长期伴侣"]
MEMORY_CATEGORIES = ["人物事实与偏好", "共同经历与纪念日", "情绪事件与冲突", "承诺和待办事项", "敏感信息"]


def utc_now() -> str:
    """返回统一使用的 UTC ISO-8601 时间戳。"""
    return datetime.now(UTC).isoformat()


def _loads(value: str | None, fallback: Any) -> Any:
    """安全解析 JSON 文本。

    当数据库字段为空或格式异常时，返回 fallback，避免把脏数据异常
    直接抛给上层调用者。
    """
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class ChatStore:
    """SQLite 操作封装（Repository 风格）。

    每类数据操作都提供明确的小方法，SQL 集中在这一层，便于阅读、测试
    与后续维护。
    """

    def __init__(self, db_path: Path | str = DATABASE_PATH, upload_dir: Path | str = UPLOAD_DIR):
        """绑定数据库文件路径与上传目录路径。"""
        self.db_path = Path(db_path)
        self.upload_dir = Path(upload_dir)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """提供一个带事务边界的 SQLite 连接。

        行为说明：
        - 打开连接前确保数据库目录存在。
        - 使用 sqlite3.Row，便于按列名读取。
        - 显式开启外键约束（SQLite 默认关闭）。
        - 通过 with connection 在成功时提交、异常时回滚。
        - 最终始终关闭连接。
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """初始化数据库结构并写入最小基础数据。

        该方法是幂等的，可在每次启动时安全调用。
        会创建缺失的表和索引，并确保：
        - 至少有一个默认联系人；
        - 该联系人存在一条关系记录；
        - personas 表存在 self / other 两个作用域占位记录。
        """
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    image_paths TEXT NOT NULL DEFAULT '[]',
                    source_generation_id INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS personas (
                    scope TEXT PRIMARY KEY,
                    content TEXT NOT NULL DEFAULT '{}',
                    examples TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sensitive INTEGER NOT NULL DEFAULT 0,
                    source_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS emotions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id INTEGER NOT NULL,
                    subject TEXT NOT NULL,
                    emotion TEXT NOT NULL,
                    intensity REAL NOT NULL,
                    confidence REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    confirmed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS relationships (
                    contact_id INTEGER PRIMARY KEY,
                    current_level TEXT NOT NULL,
                    suggested_level TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id INTEGER NOT NULL,
                    input_message_id INTEGER,
                    strategy TEXT NOT NULL DEFAULT '',
                    candidates TEXT NOT NULL DEFAULT '[]',
                    analysis TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_id, id);
                CREATE INDEX IF NOT EXISTS idx_memories_contact ON memories(contact_id, id);
                CREATE INDEX IF NOT EXISTS idx_emotions_contact ON emotions(contact_id, id);
                """
            )
            now = utc_now()
            if connection.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0:
                cursor = connection.execute(
                    "INSERT INTO contacts(name, created_at, updated_at) VALUES (?, ?, ?)",
                    ("聊天对象", now, now),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("创建默认联系人失败")
                contact_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO relationships(contact_id, current_level, updated_at) VALUES (?, ?, ?)",
                    (contact_id, "认识", now),
                )
            for scope in ("self", "other"):
                connection.execute(
                    "INSERT OR IGNORE INTO personas(scope, content, examples, updated_at) VALUES (?, '{}', '[]', ?)",
                    (scope, now),
                )

    def list_contacts(self) -> list[dict[str, Any]]:
        """按 id 升序返回所有联系人。"""
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM contacts ORDER BY id")]

    def update_contact(self, contact_id: int, name: str) -> dict[str, Any]:
        """更新联系人名称并返回更新后的记录。

        异常：
            KeyError: 联系人不存在时抛出。
        """
        with self.connect() as connection:
            connection.execute(
                "UPDATE contacts SET name = ?, updated_at = ? WHERE id = ?",
                (name.strip() or "聊天对象", utc_now(), contact_id),
            )
            row = connection.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if row is None:
            raise KeyError("联系人不存在")
        return dict(row)

    def save_message(
        self,
        contact_id: int,
        role: str,
        content: str,
        image_paths: list[str] | None = None,
        source_generation_id: int | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """插入一条消息并返回可直接给 API 使用的字典。

        image_paths 等 JSON 字段使用 ensure_ascii=False 序列化，
        以保证中文内容可读。
        """
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO messages(contact_id, role, content, image_paths, source_generation_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (contact_id, role, content, json.dumps(image_paths or [], ensure_ascii=False), source_generation_id, created_at or utc_now()),
            )
            row = connection.execute("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._message_dict(row)

    def list_messages(self, contact_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """按时间正序返回某联系人最近消息。"""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE contact_id = ? ORDER BY id DESC LIMIT ?",
                (contact_id, limit),
            ).fetchall()
        return [self._message_dict(row) for row in reversed(rows)]

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        """将消息行标准化，并反序列化 JSON 字段。"""
        result = dict(row)
        result["image_paths"] = _loads(result.get("image_paths"), [])
        return result

    def get_persona(self, scope: str) -> dict[str, Any]:
        """读取 self 或 other 作用域的人格数据。"""
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM personas WHERE scope = ?", (scope,)).fetchone()
        if row is None:
            return {"scope": scope, "content": {}, "examples": []}
        result = dict(row)
        result["content"] = _loads(result["content"], {})
        result["examples"] = _loads(result["examples"], [])
        return result

    def save_persona(self, scope: str, content: dict[str, Any], examples: list[str]) -> dict[str, Any]:
        """按作用域写入人格内容与示例（upsert）。"""
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO personas(scope, content, examples, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(scope) DO UPDATE SET content=excluded.content, examples=excluded.examples, updated_at=excluded.updated_at""",
                (scope, json.dumps(content, ensure_ascii=False), json.dumps(examples, ensure_ascii=False), utc_now()),
            )
        return self.get_persona(scope)

    def list_memories(self, contact_id: int, limit: int = 300) -> list[dict[str, Any]]:
        """列出某联系人的记忆，按 id 倒序（最新在前）。"""
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM memories WHERE contact_id = ? ORDER BY id DESC LIMIT ?", (contact_id, limit)
            )]

    def search_memories(self, contact_id: int, query: str, limit: int = 12) -> list[dict[str, Any]]:
        """轻量级本地记忆检索。

        打分策略保持简单且可预测：
        - 每个命中的分词项 +3 分（空格/标点切分）
        - 每个命中的 query 双字片段 +1 分
        - 同分时按更大的 id（更新）优先

        若无命中，则回退返回少量最近记忆，保证下游仍有上下文可用。
        """
        memories = self.list_memories(contact_id, limit=1000)
        terms = {part.lower() for part in query.replace("，", " ").replace("。", " ").split() if part}
        chars = {query[index:index + 2] for index in range(max(0, len(query) - 1))}

        def score(item: dict[str, Any]) -> tuple[int, int]:
            text = item["content"].lower()
            value = sum(3 for term in terms if term in text) + sum(1 for gram in chars if gram in text)
            return value, int(item["id"])

        ranked = sorted(memories, key=score, reverse=True)
        relevant = [item for item in ranked if score(item)[0] > 0]
        return (relevant or ranked[:4])[:limit]

    def add_memory(self, contact_id: int, category: str, content: str, sensitive: bool = False, source_message_id: int | None = None) -> dict[str, Any]:
        """新增记忆；若同联系人+分类+内容已存在，则直接返回已存在记录。"""
        cleaned = content.strip()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM memories WHERE contact_id = ? AND category = ? AND content = ?",
                (contact_id, category, cleaned),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            now = utc_now()
            cursor = connection.execute(
                """INSERT INTO memories(contact_id, category, content, sensitive, source_message_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (contact_id, category, cleaned, int(sensitive), source_message_id, now, now),
            )
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def update_memory(self, memory_id: int, category: str, content: str, sensitive: bool) -> dict[str, Any]:
        """更新一条记忆并返回更新后的记录。

        异常：
            KeyError: memory_id 不存在时抛出。
        """
        with self.connect() as connection:
            connection.execute(
                "UPDATE memories SET category=?, content=?, sensitive=?, updated_at=? WHERE id=?",
                (category, content.strip(), int(sensitive), utc_now(), memory_id),
            )
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError("记忆不存在")
        return dict(row)

    def delete_memory(self, memory_id: int) -> None:
        """按 id 删除一条记忆（不存在时为无操作）。"""
        with self.connect() as connection:
            connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))

    def add_emotion(self, contact_id: int, item: dict[str, Any]) -> dict[str, Any]:
        """保存一条情绪分析记录，并对数值范围做钳制。

        confidence >= 0.75 时自动标记为 confirmed，减少人工确认量。
        """
        confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        intensity = max(0.0, min(1.0, float(item.get("intensity", 0.5))))
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO emotions(contact_id, subject, emotion, intensity, confidence, reason, confirmed, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    contact_id, str(item.get("subject", "对方")), str(item.get("emotion", "平静")),
                    intensity, confidence, str(item.get("reason", "")), int(confidence >= 0.75), utc_now(),
                ),
            )
            row = connection.execute("SELECT * FROM emotions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def list_emotions(self, contact_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """列出某联系人的近期情绪记录。"""
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM emotions WHERE contact_id=? ORDER BY id DESC LIMIT ?", (contact_id, limit)
            )]

    def confirm_emotion(self, emotion_id: int) -> None:
        """将一条情绪记录标记为人工确认。"""
        with self.connect() as connection:
            connection.execute("UPDATE emotions SET confirmed=1 WHERE id=?", (emotion_id,))

    def get_relationship(self, contact_id: int) -> dict[str, Any]:
        """读取关系记录；若不存在则懒创建默认关系。"""
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM relationships WHERE contact_id=?", (contact_id,)).fetchone()
            if row is None:
                now = utc_now()
                connection.execute(
                    "INSERT INTO relationships(contact_id, current_level, updated_at) VALUES (?, '认识', ?)",
                    (contact_id, now),
                )
                row = connection.execute("SELECT * FROM relationships WHERE contact_id=?", (contact_id,)).fetchone()
        return dict(row)

    def suggest_relationship(self, contact_id: int, level: str | None, reason: str) -> dict[str, Any]:
        """保存“建议关系阶段”供人工确认。

        仅当建议值属于合法阶段且与当前阶段不同才写入；否则清空建议。
        """
        current = self.get_relationship(contact_id)
        suggestion = level if level in RELATIONSHIP_LEVELS and level != current["current_level"] else None
        with self.connect() as connection:
            connection.execute(
                "UPDATE relationships SET suggested_level=?, reason=?, updated_at=? WHERE contact_id=?",
                (suggestion, reason, utc_now(), contact_id),
            )
        return self.get_relationship(contact_id)

    def set_relationship(self, contact_id: int, level: str) -> dict[str, Any]:
        """强制设置当前关系阶段，并清空待确认建议。"""
        if level not in RELATIONSHIP_LEVELS:
            raise ValueError("未知关系阶段")
        with self.connect() as connection:
            connection.execute(
                "UPDATE relationships SET current_level=?, suggested_level=NULL, reason='', updated_at=? WHERE contact_id=?",
                (level, utc_now(), contact_id),
            )
        return self.get_relationship(contact_id)

    def save_generation(self, contact_id: int, input_message_id: int | None, strategy: str, candidates: list[str], analysis: dict[str, Any]) -> int:
        """保存一次生成快照，返回 generation_id。"""
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO generations(contact_id, input_message_id, strategy, candidates, analysis, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (contact_id, input_message_id, strategy, json.dumps(candidates, ensure_ascii=False), json.dumps(analysis, ensure_ascii=False), utc_now()),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("创建生成记录失败")
        return int(cursor.lastrowid)

    def get_setting(self, key: str, default: str = "") -> str:
        """读取一个设置项，不存在时返回默认值。"""
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        """写入一个设置项（upsert）。"""
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, utc_now()),
            )

    def export_all(self) -> dict[str, Any]:
        """导出所有业务表数据，用于备份或排查。"""
        tables = ["contacts", "messages", "personas", "memories", "emotions", "relationships", "generations", "settings"]
        result: dict[str, Any] = {"exported_at": utc_now()}
        with self.connect() as connection:
            for table in tables:
                result[table] = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
        return result


store = ChatStore()
