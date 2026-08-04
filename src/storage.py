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
import threading
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator

import yaml

from .config import DATABASE_PATH, PERSONA_PATH, UPLOAD_DIR


RELATIONSHIP_LEVELS = ["陌生人", "熟人", "朋友", "亲密朋友", "暧昧", "恋人"]
RELATIONSHIP_DIMENSIONS = ("trust", "familiarity", "intimacy", "attraction", "attachment", "comfort")
MEMORY_CATEGORIES = ["episodic", "semantic", "emotional", "shared_experience"]
USER_MEMORY_CATEGORIES = ["episodic", "semantic", "emotional"]
MEMORY_OWNER_AI = "ai"
MEMORY_OWNER_CONTACT = "contact"
DEFAULT_EMOTION_PROFILE = {
    "sensitivity": 0.5,
    "warmth": 0.5,
    "jealousy": 0.2,
    "expressiveness": 0.5,
    "forgiveness": 0.5,
    "humor": 0.5,
}
LEGACY_MEMORY_CATEGORY_MAP = {
    "人物事实与偏好": "semantic",
    "共同经历与纪念日": "shared_experience",
    "情绪事件与冲突": "emotional",
    "承诺和待办事项": "episodic",
    "敏感信息": "semantic",
}


def utc_now() -> str:
    """返回统一使用的本地时区 ISO-8601 时间戳。"""
    return datetime.now().astimezone().isoformat()


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
        self.persona_path = PERSONA_PATH
        self.ai_memory_path = self.db_path.parent / "ai_memory.yaml"
        self.export_dir = self.db_path.parent / "export"
        self._persona_lock = threading.Lock()
        self._transaction_state = threading.local()
        self._export_hashes: dict[str, str] = {}

    EXPORT_FILE_TABLES = {
        "01_contacts.yaml": frozenset({"contacts"}),
        "02_personas.yaml": frozenset({"persona_update_proposals", "persona_profile"}),
        "03_messages.yaml": frozenset({"messages"}),
        "04_contact_memories.yaml": frozenset({"memories"}),
        "05_ai_memory.yaml": frozenset({"ai_memory_profile"}),
        "06_emotions.yaml": frozenset({"emotions", "ai_emotion_states", "conversation_events"}),
        "07_relationships.yaml": frozenset({"relationships", "relationship_events"}),
        "08_generations.yaml": frozenset({"generations"}),
        "09_settings.yaml": frozenset({"settings"}),
    }

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
        total_changes_before = connection.total_changes
        touched_exports: set[str] = set()
        try:
            with connection:
                self._transaction_state.touched_exports = touched_exports
                yield connection
        finally:
            if connection.in_transaction is False and connection.total_changes > total_changes_before:
                self.refresh_yaml_snapshots(touched_exports)
            self._transaction_state.touched_exports = set()
            connection.close()

    def _mark_exports(self, connection: sqlite3.Connection, *filenames: str) -> None:
        del connection
        touched = getattr(self._transaction_state, "touched_exports", None)
        if isinstance(touched, set):
            touched.update(filenames)

    def initialize(self) -> None:
        """初始化数据库结构并写入最小基础数据。

        该方法是幂等的，可在每次启动时安全调用。
        会创建缺失的表和索引，并确保：
        - 至少有一个默认联系人；
        - 该联系人存在一条关系记录；
        - 各联系人存在对应的 AI emotion 初始状态。
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
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id INTEGER NOT NULL,
                    owner_type TEXT NOT NULL DEFAULT 'contact',
                    owner_id INTEGER,
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
                    trust INTEGER NOT NULL DEFAULT 20,
                    familiarity INTEGER NOT NULL DEFAULT 10,
                    intimacy INTEGER NOT NULL DEFAULT 5,
                    attraction INTEGER NOT NULL DEFAULT 0,
                    attachment INTEGER NOT NULL DEFAULT 5,
                    comfort INTEGER NOT NULL DEFAULT 20,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS ai_emotion_states (
                    contact_id INTEGER PRIMARY KEY,
                    mood TEXT NOT NULL DEFAULT '放松',
                    energy INTEGER NOT NULL DEFAULT 60,
                    dimensions TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS conversation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    impact REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS relationship_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id INTEGER NOT NULL,
                    dimension TEXT NOT NULL,
                    delta INTEGER NOT NULL,
                    previous_value INTEGER NOT NULL,
                    current_value INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS persona_update_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id INTEGER NOT NULL,
                    section TEXT NOT NULL,
                    field_key TEXT NOT NULL,
                    proposed_value TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
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
                    selected_candidate_index INTEGER,
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
                CREATE INDEX IF NOT EXISTS idx_conversation_events_contact ON conversation_events(contact_id, id);
                CREATE INDEX IF NOT EXISTS idx_relationship_events_contact ON relationship_events(contact_id, id);
                CREATE INDEX IF NOT EXISTS idx_persona_proposals_contact ON persona_update_proposals(contact_id, id);
                """
            )
            generation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(generations)").fetchall()
            }
            if "selected_candidate_index" not in generation_columns:
                connection.execute("ALTER TABLE generations ADD COLUMN selected_candidate_index INTEGER")
            relationship_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(relationships)").fetchall()
            }
            defaults = {
                "trust": 20, "familiarity": 10, "intimacy": 5,
                "attraction": 0, "attachment": 5, "comfort": 20,
            }
            memory_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(memories)").fetchall()
            }
            if "owner_type" not in memory_columns:
                connection.execute(
                    "ALTER TABLE memories ADD COLUMN owner_type TEXT NOT NULL DEFAULT 'contact'"
                )
            if "owner_id" not in memory_columns:
                connection.execute("ALTER TABLE memories ADD COLUMN owner_id INTEGER")
            connection.execute(
                "UPDATE memories SET owner_type='contact', owner_id=contact_id WHERE owner_type IS NULL OR owner_type=''"
            )
            for column, default in defaults.items():
                if column not in relationship_columns:
                    connection.execute(
                        f"ALTER TABLE relationships ADD COLUMN {column} INTEGER NOT NULL DEFAULT {default}"
                    )
            connection.execute("UPDATE relationships SET current_level='熟人' WHERE current_level='认识'")
            connection.execute("UPDATE relationships SET current_level='恋人' WHERE current_level='长期伴侣'")
            for old_category, new_category in LEGACY_MEMORY_CATEGORY_MAP.items():
                connection.execute(
                    "UPDATE memories SET category=? WHERE category=?", (new_category, old_category)
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
                    (contact_id, "熟人", now),
                )
                connection.execute(
                    """INSERT INTO ai_emotion_states(contact_id, mood, energy, dimensions, updated_at)
                       VALUES (?, '放松', 60, ?, ?)""",
                    (contact_id, json.dumps({}, ensure_ascii=False), now),
                )
            contacts = [dict(row) for row in connection.execute("SELECT id FROM contacts")]
            for contact in contacts:
                connection.execute(
                    """INSERT OR IGNORE INTO ai_emotion_states(contact_id, mood, energy, dimensions, updated_at)
                       VALUES (?, '放松', 60, ?, ?)""",
                    (int(contact["id"]), json.dumps({}, ensure_ascii=False), now),
                )
            connection.execute("DROP TABLE IF EXISTS personas")

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
            self._mark_exports(connection, "01_contacts.yaml")
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
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """插入一条消息并返回可直接给 API 使用的字典。

        image_paths 等 JSON 字段使用 ensure_ascii=False 序列化，
        以保证中文内容可读。
        """
        with self.connect() as connection:
            cursor = connection.execute(
                     """INSERT INTO messages(contact_id, role, content, image_paths, created_at)
                         VALUES (?, ?, ?, ?, ?)""",
                     (contact_id, role, content, json.dumps(image_paths or [], ensure_ascii=False), created_at or utc_now()),
            )
            self._mark_exports(connection, "03_messages.yaml")
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
        result.pop("source_generation_id", None)
        return result

    def get_persona_profile(self) -> dict[str, Any]:
        """Load the manually maintained Core, Personality, and Habits YAML profile."""
        if not self.persona_path.exists():
            return {"core": {}, "personality": {}, "habits": {}, "expression": {"examples": []}}
        try:
            loaded = yaml.safe_load(self.persona_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {"core": {}, "personality": {}, "habits": {}, "expression": {"examples": []}}
        return loaded if isinstance(loaded, dict) else {}

    def save_persona_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        """Persist the editable YAML persona profile used by the main workflow."""
        normalized = {
            "core": dict(profile.get("core") or {}),
            "personality": dict(profile.get("personality") or {}),
            "habits": dict(profile.get("habits") or {}),
            "expression": dict(profile.get("expression") or {}),
        }
        with self._persona_lock:
            self.persona_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.persona_path.with_suffix(self.persona_path.suffix + ".tmp")
            temporary.write_text(
                yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            temporary.replace(self.persona_path)
        self.refresh_yaml_snapshots({"02_personas.yaml"})
        return self.get_persona_profile()

    def get_ai_memory_profile(self) -> dict[str, Any]:
        if not self.ai_memory_path.exists():
            return {category: [] for category in MEMORY_CATEGORIES}
        try:
            loaded = yaml.safe_load(self.ai_memory_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {category: [] for category in MEMORY_CATEGORIES}
        profile = loaded if isinstance(loaded, dict) else {}
        normalized: dict[str, list[Any]] = {}
        for category in MEMORY_CATEGORIES:
            value = profile.get(category, [])
            normalized[category] = value if isinstance(value, list) else []
        return normalized

    def save_ai_memory_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        normalized = {
            category: list(profile.get(category) or [])
            for category in MEMORY_CATEGORIES
        }
        with self._persona_lock:
            self.ai_memory_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.ai_memory_path.with_suffix(self.ai_memory_path.suffix + ".tmp")
            temporary.write_text(
                yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            temporary.replace(self.ai_memory_path)
        self.refresh_yaml_snapshots({"05_ai_memory.yaml"})
        return self.get_ai_memory_profile()

    def get_emotion_profile(self) -> dict[str, float]:
        """Return persona-level emotion tuning with sane defaults."""
        profile = self.get_persona_profile()
        raw = profile.get("emotionProfile", {})
        if not isinstance(raw, dict):
            return dict(DEFAULT_EMOTION_PROFILE)
        merged = dict(DEFAULT_EMOTION_PROFILE)
        for key, default in DEFAULT_EMOTION_PROFILE.items():
            try:
                merged[key] = max(0.0, min(1.0, float(raw.get(key, default))))
            except (TypeError, ValueError):
                merged[key] = default
        return merged

    def update_persona_habits(self, updates: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply Reflection habit updates to YAML without changing Core or Personality."""
        with self._persona_lock:
            profile = self.get_persona_profile()
            habits = profile.setdefault("habits", {})
            if not isinstance(habits, dict):
                habits = {}
                profile["habits"] = habits
            for update in updates[:6]:
                key = str(update.get("key", "")).strip()
                value = str(update.get("value", "")).strip()
                if key and value:
                    habits[key] = value
            self.persona_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.persona_path.with_suffix(self.persona_path.suffix + ".tmp")
            temporary.write_text(
                yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            temporary.replace(self.persona_path)
        self.refresh_yaml_snapshots({"02_personas.yaml"})
        return profile

    def add_core_update_proposals(
        self, contact_id: int, proposals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Persist Reflection Core changes for later human confirmation."""
        created: list[dict[str, Any]] = []
        with self.connect() as connection:
            for proposal in proposals[:4]:
                key = str(proposal.get("key", "")).strip()
                value = str(proposal.get("value", "")).strip()
                if not key or not value:
                    continue
                now = utc_now()
                cursor = connection.execute(
                    """INSERT INTO persona_update_proposals(
                           contact_id, section, field_key, proposed_value, reason, created_at, updated_at
                       ) VALUES (?, 'core', ?, ?, ?, ?, ?)""",
                    (contact_id, key, value, str(proposal.get("reason", "")), now, now),
                )
                row = connection.execute(
                    "SELECT * FROM persona_update_proposals WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                created.append(dict(row))
            if created:
                self._mark_exports(connection, "02_personas.yaml")
        return created

    def list_core_update_proposals(self, contact_id: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT * FROM persona_update_proposals
                   WHERE contact_id=? AND status='pending' ORDER BY id DESC""",
                (contact_id,),
            )]

    def resolve_core_update_proposal(self, proposal_id: int, approved: bool) -> dict[str, Any]:
        """Approve a pending Core proposal into YAML or reject it while retaining audit history."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM persona_update_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Persona 更新提案不存在")
        proposal = dict(row)
        if proposal["status"] != "pending":
            raise ValueError("Persona 更新提案已经处理")

        if approved:
            with self._persona_lock:
                profile = self.get_persona_profile()
                core = profile.setdefault("core", {})
                if not isinstance(core, dict):
                    core = {}
                    profile["core"] = core
                core[str(proposal["field_key"])] = str(proposal["proposed_value"])
                temporary = self.persona_path.with_suffix(self.persona_path.suffix + ".tmp")
                temporary.write_text(
                    yaml.safe_dump(profile, allow_unicode=True, sort_keys=False), encoding="utf-8"
                )
                temporary.replace(self.persona_path)

        status = "approved" if approved else "rejected"
        with self.connect() as connection:
            connection.execute(
                "UPDATE persona_update_proposals SET status=?, updated_at=? WHERE id=?",
                (status, utc_now(), proposal_id),
            )
            self._mark_exports(connection, "02_personas.yaml")
            resolved = connection.execute(
                "SELECT * FROM persona_update_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        return dict(resolved)

    def list_memories(
        self,
        contact_id: int,
        limit: int = 300,
        owner_type: str = MEMORY_OWNER_CONTACT,
    ) -> list[dict[str, Any]]:
        """列出某联系人的记忆，按 id 倒序（最新在前）。"""
        with self.connect() as connection:
            items = [dict(row) for row in connection.execute(
                """SELECT * FROM memories
                   WHERE contact_id = ? AND owner_type = ?
                   ORDER BY id DESC LIMIT ?""",
                (contact_id, owner_type, limit),
            )]
        return items

    def search_memories(
        self, contact_id: int, query: str, limit: int = 12,
        category: str | None = None, time_range: str = "all",
    ) -> list[dict[str, Any]]:
        """轻量级本地记忆检索。

        打分策略保持简单且可预测：
        - 每个命中的分词项 +3 分（空格/标点切分）
        - 每个命中的 query 双字片段 +1 分
        - 同分时按更大的 id（更新）优先

        若无命中，则回退返回少量最近记忆，保证下游仍有上下文可用。
        """
        memories = self.list_memories(contact_id, limit=1000, owner_type=MEMORY_OWNER_CONTACT)
        if category in MEMORY_CATEGORIES:
            memories = [item for item in memories if item["category"] == category]
        if time_range != "all":
            try:
                days = max(1, int(time_range.removesuffix("d")))
                cutoff = datetime.now().astimezone().timestamp() - days * 86400
                memories = [
                    item for item in memories
                    if datetime.fromisoformat(str(item["created_at"])).timestamp() >= cutoff
                ]
            except (TypeError, ValueError):
                pass
        terms = {part.lower() for part in query.replace("，", " ").replace("。", " ").split() if part}
        chars = {query[index:index + 2] for index in range(max(0, len(query) - 1))}

        def score(item: dict[str, Any]) -> tuple[int, int]:
            text = item["content"].lower()
            value = sum(3 for term in terms if term in text) + sum(1 for gram in chars if gram in text)
            return value, int(item["id"])

        ranked = sorted(memories, key=score, reverse=True)
        relevant = [item for item in ranked if score(item)[0] > 0]
        return (relevant or ranked[:4])[:limit]

    def search_memories_by_plan(
        self, contact_id: int, requests: list[dict[str, Any]], total_limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Execute a validated Memory Planner result and deduplicate ranked results."""
        selected: dict[int, tuple[int, dict[str, Any]]] = {}
        ordered = sorted(requests, key=lambda item: int(item.get("priority", 1)), reverse=True)
        for request in ordered:
            query_parts = [str(request.get("query", "")), *map(str, request.get("keywords", []))]
            items = self.search_memories(
                contact_id,
                " ".join(query_parts),
                limit=max(1, min(20, int(request.get("top_k", 4)))),
                category=str(request.get("memory_type", "")),
                time_range=str(request.get("time_range", "all")),
            )
            priority = max(1, min(5, int(request.get("priority", 1))))
            for item in items:
                memory_id = int(item["id"])
                if memory_id not in selected or priority > selected[memory_id][0]:
                    enriched = {**item, "retrieval_priority": priority}
                    selected[memory_id] = (priority, enriched)
        ranked = sorted(selected.values(), key=lambda pair: (pair[0], int(pair[1]["id"])), reverse=True)
        return [item for _, item in ranked[:total_limit]]

    def add_memory(
        self,
        contact_id: int,
        category: str,
        content: str,
        sensitive: bool = False,
        source_message_id: int | None = None,
        owner_type: str = MEMORY_OWNER_CONTACT,
        owner_id: int | None = None,
    ) -> dict[str, Any]:
        """新增记忆；若同联系人+分类+内容已存在，则直接返回已存在记录。"""
        cleaned = content.strip()
        if owner_type == MEMORY_OWNER_CONTACT and category not in USER_MEMORY_CATEGORIES:
            raise ValueError("用户 memory 只允许 episodic / semantic / emotional")
        resolved_owner_id = contact_id if owner_type == MEMORY_OWNER_CONTACT else owner_id
        with self.connect() as connection:
            existing = connection.execute(
                """SELECT * FROM memories
                   WHERE contact_id = ? AND owner_type = ? AND category = ? AND content = ?""",
                (contact_id, owner_type, category, cleaned),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            now = utc_now()
            cursor = connection.execute(
                """INSERT INTO memories(
                       contact_id, owner_type, owner_id, category, content, sensitive,
                       source_message_id, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    contact_id, owner_type, resolved_owner_id, category, cleaned, int(sensitive),
                    source_message_id, now, now,
                ),
            )
            self._mark_exports(connection, "04_contact_memories.yaml")
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def update_memory(
        self,
        memory_id: int,
        category: str,
        content: str,
        sensitive: bool,
    ) -> dict[str, Any]:
        """更新一条记忆并返回更新后的记录。

        异常：
            KeyError: memory_id 不存在时抛出。
        """
        with self.connect() as connection:
            existing = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if existing is None:
                raise KeyError("记忆不存在")
            connection.execute(
                "UPDATE memories SET category=?, content=?, sensitive=?, updated_at=? WHERE id=?",
                (category, content.strip(), int(sensitive), utc_now(), memory_id),
            )
            self._mark_exports(connection, "04_contact_memories.yaml")
            row = connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return dict(row)

    def delete_memory(self, memory_id: int) -> None:
        """按 id 删除一条记忆（不存在时为无操作）。"""
        with self.connect() as connection:
            connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._mark_exports(connection, "04_contact_memories.yaml")

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
            self._mark_exports(connection, "06_emotions.yaml")
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
            self._mark_exports(connection, "06_emotions.yaml")

    def get_relationship(self, contact_id: int) -> dict[str, Any]:
        """读取关系记录；若不存在则懒创建默认关系。"""
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM relationships WHERE contact_id=?", (contact_id,)).fetchone()
            if row is None:
                now = utc_now()
                connection.execute(
                    "INSERT INTO relationships(contact_id, current_level, updated_at) VALUES (?, '熟人', ?)",
                    (contact_id, now),
                )
                row = connection.execute("SELECT * FROM relationships WHERE contact_id=?", (contact_id,)).fetchone()
        return dict(row)

    def get_ai_emotion_state(self, contact_id: int) -> dict[str, Any]:
        """Read persistent AI emotion state, lazily creating a default row."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_emotion_states WHERE contact_id=?", (contact_id,)
            ).fetchone()
            if row is None:
                now = utc_now()
                connection.execute(
                    """INSERT INTO ai_emotion_states(contact_id, mood, energy, dimensions, updated_at)
                       VALUES (?, '放松', 60, ?, ?)""",
                    (contact_id, json.dumps({}, ensure_ascii=False), now),
                )
                row = connection.execute(
                    "SELECT * FROM ai_emotion_states WHERE contact_id=?", (contact_id,)
                ).fetchone()
        result = dict(row)
        result["dimensions"] = _loads(result.get("dimensions"), {})
        return result

    def save_ai_emotion_state(self, contact_id: int, state: dict[str, Any]) -> dict[str, Any]:
        """Persist AI mood, energy, and multi-dimensional short emotion scores."""
        mood = str(state.get("mood", "放松")).strip() or "放松"
        energy = max(0, min(100, int(state.get("energy", 60))))
        raw_dimensions = state.get("dimensions", {})
        dimensions: dict[str, int] = {}
        if isinstance(raw_dimensions, list):
            for item in raw_dimensions[:8]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                dimensions[name] = max(0, min(100, int(item.get("value", 0))))
        elif isinstance(raw_dimensions, dict):
            for key, value in raw_dimensions.items():
                name = str(key).strip()
                if not name:
                    continue
                dimensions[name] = max(0, min(100, int(value)))
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO ai_emotion_states(contact_id, mood, energy, dimensions, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(contact_id) DO UPDATE SET
                       mood=excluded.mood,
                       energy=excluded.energy,
                       dimensions=excluded.dimensions,
                       updated_at=excluded.updated_at""",
                (contact_id, mood, energy, json.dumps(dimensions, ensure_ascii=False), now),
            )
            self._mark_exports(connection, "06_emotions.yaml")
        return self.get_ai_emotion_state(contact_id)

    def add_conversation_events(self, contact_id: int, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Persist extracted conversation events that influenced AI emotion updates."""
        created: list[dict[str, Any]] = []
        now = utc_now()
        with self.connect() as connection:
            for event in events[:4]:
                cursor = connection.execute(
                    """INSERT INTO conversation_events(contact_id, event, impact, reason, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        contact_id,
                        str(event.get("event", "其他")),
                        max(0.0, min(1.0, float(event.get("impact", 0.0)))),
                        str(event.get("reason", "")),
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM conversation_events WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                created.append(dict(row))
            if created:
                self._mark_exports(connection, "06_emotions.yaml")
        return created

    def list_conversation_events(self, contact_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM conversation_events WHERE contact_id=? ORDER BY id DESC LIMIT ?",
                (contact_id, limit),
            )]

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
            self._mark_exports(connection, "07_relationships.yaml")
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
            self._mark_exports(connection, "07_relationships.yaml")
        return self.get_relationship(contact_id)

    def apply_relationship_analysis(
        self, contact_id: int, analysis: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply bounded score events and the LLM stage judgment in one transaction."""
        current = self.get_relationship(contact_id)
        now = utc_now()
        with self.connect() as connection:
            for change in analysis.get("changes", [])[:6]:
                dimension = str(change.get("dimension", ""))
                if dimension not in RELATIONSHIP_DIMENSIONS:
                    continue
                delta = max(-10, min(10, int(change.get("delta", 0))))
                previous = int(current[dimension])
                updated = max(0, min(100, previous + delta))
                if updated == previous:
                    continue
                connection.execute(
                    f"UPDATE relationships SET {dimension}=?, updated_at=? WHERE contact_id=?",
                    (updated, now, contact_id),
                )
                connection.execute(
                    """INSERT INTO relationship_events(
                           contact_id, dimension, delta, previous_value, current_value, reason, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (contact_id, dimension, updated - previous, previous, updated, str(change.get("reason", "")), now),
                )
                current[dimension] = updated

            current_level = str(current["current_level"])
            suggested = str(analysis.get("suggested_level", current_level))
            if suggested in RELATIONSHIP_LEVELS and suggested != current_level:
                is_downgrade = RELATIONSHIP_LEVELS.index(suggested) < RELATIONSHIP_LEVELS.index(current_level)
                if not is_downgrade or bool(analysis.get("serious_event", False)):
                    current_level = suggested
            connection.execute(
                """UPDATE relationships
                   SET current_level=?, suggested_level=NULL, reason=?, updated_at=?
                   WHERE contact_id=?""",
                (current_level, str(analysis.get("stage_reason", "")), now, contact_id),
            )
            self._mark_exports(connection, "07_relationships.yaml")
        return self.get_relationship(contact_id)

    def list_relationship_events(self, contact_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM relationship_events WHERE contact_id=? ORDER BY id DESC LIMIT ?",
                (contact_id, limit),
            )]

    def save_generation(self, contact_id: int, input_message_id: int | None, strategy: str, candidates: list[str], analysis: dict[str, Any]) -> int:
        """保存一次生成快照，返回 generation_id。"""
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO generations(contact_id, input_message_id, strategy, candidates, analysis, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (contact_id, input_message_id, strategy, json.dumps(candidates, ensure_ascii=False), json.dumps(analysis, ensure_ascii=False), utc_now()),
            )
            self._mark_exports(connection, "08_generations.yaml")
        if cursor.lastrowid is None:
            raise RuntimeError("创建生成记录失败")
        return int(cursor.lastrowid)

    def select_generation_candidate(self, generation_id: int, contact_id: int, candidate_index: int | None) -> None:
        """Record only which generated candidate was selected; edited text is not copied here."""
        if candidate_index is not None and candidate_index not in {0, 1, 2}:
            raise ValueError("候选索引只能是 0、1 或 2")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE generations SET selected_candidate_index=? WHERE id=? AND contact_id=?",
                (candidate_index, generation_id, contact_id),
            )
            self._mark_exports(connection, "08_generations.yaml")
        if cursor.rowcount == 0:
            raise KeyError("生成记录不存在")

    def list_generations(self, contact_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """列出某联系人的生成记录，按最新优先返回。"""
        with self.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM generations WHERE contact_id=? ORDER BY id DESC LIMIT ?",
                    (contact_id, limit),
                )
            ]
        return [self._deserialize_generation_row(row) for row in rows]

    def trim_generations(self, contact_id: int, keep: int) -> None:
        """仅保留最近 keep 条生成记录，避免调试快照无限增长。"""
        keep = max(1, int(keep))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM generations WHERE contact_id=? ORDER BY id DESC LIMIT -1 OFFSET ?",
                (contact_id, keep),
            ).fetchall()
            stale_ids = [int(row[0]) for row in rows]
            if not stale_ids:
                return
            placeholders = ",".join("?" for _ in stale_ids)
            connection.execute(
                f"DELETE FROM generations WHERE id IN ({placeholders})",
                stale_ids,
            )
            self._mark_exports(connection, "08_generations.yaml")

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
            self._mark_exports(connection, "09_settings.yaml")

    def export_all(self) -> dict[str, Any]:
        """导出所有业务表数据，用于备份或排查。"""
        tables = [
            "contacts",
            "messages",
            "memories",
            "emotions",
            "relationships",
            "ai_emotion_states",
            "conversation_events",
            "relationship_events",
            "persona_update_proposals",
            "generations",
            "settings",
        ]
        result: dict[str, Any] = {"exported_at": utc_now()}
        with self.connect() as connection:
            for table in tables:
                rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                result[table] = self._deserialize_export_rows(table, rows)
        return result

    def export_yaml_files(self) -> dict[str, Any]:
        """将当前数据快照细分为多个 YAML 文件，便于人工调试。"""
        return self.refresh_yaml_snapshots(set(self.EXPORT_FILE_TABLES))

    def refresh_yaml_snapshots(self, filenames: set[str] | None = None) -> dict[str, Any]:
        """按受影响文件刷新 YAML 快照，只有内容变化时才重写文件。"""
        selected = set(filenames or self.EXPORT_FILE_TABLES)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        exported_at = utc_now()
        file_payloads = self._build_export_file_payloads(selected)
        for filename, payload in file_payloads.items():
            self._write_yaml_if_changed(self.export_dir / filename, payload)
        managed_files = set(self.EXPORT_FILE_TABLES) | {"00_overview.yaml"}
        for path in self.export_dir.glob("*.yaml"):
            if path.name not in managed_files:
                path.unlink(missing_ok=True)
        overview = self._build_overview_payload(exported_at)
        self._write_yaml_if_changed(self.export_dir / "00_overview.yaml", overview)
        return {"exported_at": exported_at, "export_dir": str(self.export_dir)}

    def _build_overview_payload(self, exported_at: str) -> dict[str, Any]:
        files: dict[str, Any] = {}
        for filename in self.EXPORT_FILE_TABLES:
            files[filename] = {
                "path": str(self.export_dir / filename),
                "exists": (self.export_dir / filename).exists(),
            }
        return {
            "exported_at": exported_at,
            "database_path": str(self.db_path),
            "persona_path": str(self.persona_path),
            "files": files,
        }

    def _build_export_file_payloads(self, filenames: set[str]) -> dict[str, Any]:
        needed_tables = set().union(*(self.EXPORT_FILE_TABLES.get(name, frozenset()) for name in filenames))
        table_data = self._load_export_tables(needed_tables)
        payloads: dict[str, Any] = {}
        for filename in filenames:
            payloads[filename] = self._compose_export_file(filename, table_data)
        return payloads

    def _load_export_tables(self, tables: set[str]) -> dict[str, Any]:
        actual_tables = [table for table in tables if table not in {"persona_profile", "ai_memory_profile"}]
        loaded: dict[str, Any] = {}
        if actual_tables:
            with self.connect() as connection:
                for table in actual_tables:
                    rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                    loaded[table] = self._deserialize_export_rows(table, rows)
        if "persona_profile" in tables:
            loaded["persona_profile"] = self.get_persona_profile()
        if "ai_memory_profile" in tables:
            loaded["ai_memory_profile"] = self.get_ai_memory_profile()
        return loaded

    def _compose_export_file(self, filename: str, table_data: dict[str, Any]) -> Any:
        if filename == "01_contacts.yaml":
            return table_data.get("contacts", [])
        if filename == "02_personas.yaml":
            return {
                "persona_profile": table_data.get("persona_profile", {}),
                "persona_update_proposals": table_data.get("persona_update_proposals", []),
            }
        if filename == "03_messages.yaml":
            return table_data.get("messages", [])
        if filename == "04_contact_memories.yaml":
            return table_data.get("memories", [])
        if filename == "05_ai_memory.yaml":
            return table_data.get("ai_memory_profile", {})
        if filename == "06_emotions.yaml":
            return {
                "user_emotions": table_data.get("emotions", []),
                "ai_emotion_states": table_data.get("ai_emotion_states", []),
                "conversation_events": table_data.get("conversation_events", []),
            }
        if filename == "07_relationships.yaml":
            return {
                "relationships": table_data.get("relationships", []),
                "relationship_events": table_data.get("relationship_events", []),
            }
        if filename == "08_generations.yaml":
            return table_data.get("generations", [])
        if filename == "09_settings.yaml":
            return table_data.get("settings", [])
        raise KeyError(f"未知导出文件: {filename}")

    def _export_file_manifest(self, exported: dict[str, Any]) -> dict[str, Any]:
        return {
            "01_contacts.yaml": exported["contacts"],
            "02_personas.yaml": {
                "persona_profile": self.get_persona_profile(),
                "persona_update_proposals": exported["persona_update_proposals"],
            },
            "03_messages.yaml": exported["messages"],
            "04_contact_memories.yaml": exported["memories"],
            "05_ai_memory.yaml": self.get_ai_memory_profile(),
            "06_emotions.yaml": {
                "user_emotions": exported["emotions"],
                "ai_emotion_states": exported["ai_emotion_states"],
                "conversation_events": exported["conversation_events"],
            },
            "07_relationships.yaml": {
                "relationships": exported["relationships"],
                "relationship_events": exported["relationship_events"],
            },
            "08_generations.yaml": exported["generations"],
            "09_settings.yaml": exported["settings"],
        }

    def _deserialize_export_rows(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if table == "messages":
            return [self._deserialize_message_row(row) for row in rows]
        if table == "memories":
            return [self._deserialize_memory_row(row) for row in rows]
        if table == "ai_emotion_states":
            return [self._deserialize_ai_emotion_state_row(row) for row in rows]
        if table == "generations":
            return [self._deserialize_generation_row(row) for row in rows]
        return rows

    @staticmethod
    def _deserialize_message_row(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["image_paths"] = _loads(result.get("image_paths"), [])
        result.pop("source_generation_id", None)
        return result

    @staticmethod
    def _deserialize_ai_emotion_state_row(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["dimensions"] = _loads(result.get("dimensions"), {})
        return result

    @staticmethod
    def _deserialize_memory_row(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["owner_type"] = str(result.get("owner_type") or MEMORY_OWNER_CONTACT)
        if result.get("owner_type") == MEMORY_OWNER_CONTACT:
            result["owner_label"] = "contact"
        else:
            result["owner_label"] = result["owner_type"]
        return result

    @staticmethod
    def _deserialize_generation_row(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["candidates"] = _loads(result.get("candidates"), [])
        result["analysis"] = _loads(result.get("analysis"), {})
        return result

    def _write_yaml_if_changed(self, path: Path, payload: Any) -> None:
        content = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        digest = sha256(content.encode("utf-8")).hexdigest()
        cache_key = str(path)
        if self._export_hashes.get(cache_key) == digest and path.exists():
            return
        if path.exists() and path.read_text(encoding="utf-8") == content:
            self._export_hashes[cache_key] = digest
            return
        path.write_text(content, encoding="utf-8")
        self._export_hashes[cache_key] = digest


store = ChatStore()
