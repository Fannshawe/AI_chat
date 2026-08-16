"""User registry and local process management for multi-instance mode."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GATEWAY_LOGGER = logging.getLogger("uvicorn.error.instance")
_GATEWAY_LOG_HANDLER_MARKER = "_is_instance_debug_relay_handler"


def configure_gateway_log_relay() -> None:
    """Ensure relayed instance debug records reach the gateway terminal."""
    GATEWAY_LOGGER.setLevel(logging.DEBUG)
    GATEWAY_LOGGER.propagate = False
    if any(getattr(handler, _GATEWAY_LOG_HANDLER_MARKER, False) for handler in GATEWAY_LOGGER.handlers):
        return
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(levelname)s:    %(message)s"))
    setattr(handler, _GATEWAY_LOG_HANDLER_MARKER, True)
    GATEWAY_LOGGER.addHandler(handler)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pbkdf2(password: str, salt: bytes, iterations: int = 120_000) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return base64.b64encode(digest).decode("ascii")


def hash_password(password: str) -> dict[str, Any]:
    salt = os.urandom(16)
    return {
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": _pbkdf2(password, salt),
        "iterations": 120_000,
    }


def verify_password(password: str, stored: dict[str, Any]) -> bool:
    try:
        salt = base64.b64decode(str(stored["salt"]))
        expected = str(stored["hash"])
        iterations = int(stored.get("iterations", 120_000))
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(_pbkdf2(password, salt, iterations), expected)


def validate_username(username: str) -> str:
    value = username.strip()
    if len(value) < 3 or len(value) > 32:
        raise ValueError("用户名长度必须在 3 到 32 个字符之间")
    if value.startswith("."):
        raise ValueError("用户名不能以 . 开头")
    forbidden = set('\\/:*?"<>|')
    if any(char in forbidden for char in value):
        raise ValueError("用户名包含非法路径字符")
    return value


@dataclass(frozen=True)
class UserRecord:
    username: str
    port: int
    data_dir: Path
    persona_path: Path
    user_root: Path


class UserRegistry:
    """Persisted user definitions and fixed port allocation."""

    def __init__(self, base_dir: Path, gateway_port: int = 8000, base_instance_port: int = 8101):
        self.base_dir = Path(base_dir)
        self.gateway_port = gateway_port
        self.base_instance_port = base_instance_port
        self.users_dir = self.base_dir / "users"
        self.registry_path = self.users_dir / "registry.json"
        self._lock = threading.Lock()
        self.users_dir.mkdir(parents=True, exist_ok=True)
        if not self.registry_path.exists():
            self.registry_path.write_text(json.dumps({"users": {}}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"users": {}}
        if not isinstance(data, dict):
            data = {"users": {}}
        users = data.get("users")
        if not isinstance(users, dict):
            data["users"] = {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        temporary = self.registry_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.registry_path)

    def _allocate_port(self, users: dict[str, Any]) -> int:
        used = {int(item.get("port", 0)) for item in users.values() if isinstance(item, dict)}
        port = self.base_instance_port
        while port == self.gateway_port or port in used:
            port += 1
        return port

    def _legacy_data_dir(self) -> Path:
        return self.base_dir / "data"

    def _copy_initial_data(self, data_dir: Path, is_first_user: bool) -> None:
        legacy = self._legacy_data_dir()
        if is_first_user and legacy.exists():
            shutil.copytree(legacy, data_dir, dirs_exist_ok=True)
            return
        data_dir.mkdir(parents=True, exist_ok=True)

    def create_user(self, username: str, password: str) -> UserRecord:
        normalized = validate_username(username)
        if len(password) < 6:
            raise ValueError("密码长度至少为 6 位")
        with self._lock:
            payload = self._load()
            users = payload["users"]
            if normalized in users:
                raise FileExistsError("用户名已存在")
            port = self._allocate_port(users)
            user_root = self.users_dir / normalized
            data_dir = user_root / "data"
            self._copy_initial_data(data_dir, is_first_user=not users)
            persona_path = data_dir / "persona.yaml"
            logs_dir = user_root / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            now = utc_now()
            users[normalized] = {
                "username": normalized,
                "password": hash_password(password),
                "port": port,
                "user_root": str(user_root),
                "data_dir": str(data_dir),
                "persona_path": str(persona_path),
                "created_at": now,
                "last_used_at": now,
            }
            self._save(payload)
        return UserRecord(normalized, port, data_dir, persona_path, user_root)

    def verify_user(self, username: str, password: str) -> UserRecord | None:
        normalized = validate_username(username)
        with self._lock:
            payload = self._load()
            item = payload["users"].get(normalized)
        if not isinstance(item, dict) or not verify_password(password, item.get("password", {})):
            return None
        return self.get_user(normalized)

    def get_user(self, username: str) -> UserRecord | None:
        normalized = validate_username(username)
        with self._lock:
            payload = self._load()
            item = payload["users"].get(normalized)
        if not isinstance(item, dict):
            return None
        return UserRecord(
            username=normalized,
            port=int(item["port"]),
            data_dir=Path(item["data_dir"]),
            persona_path=Path(item["persona_path"]),
            user_root=Path(item["user_root"]),
        )

    def touch_user(self, username: str) -> None:
        normalized = validate_username(username)
        with self._lock:
            payload = self._load()
            item = payload["users"].get(normalized)
            if not isinstance(item, dict):
                return
            item["last_used_at"] = utc_now()
            self._save(payload)

    def list_users(self) -> list[dict[str, Any]]:
        with self._lock:
            payload = self._load()
            users = payload["users"]
        result: list[dict[str, Any]] = []
        for username, item in sorted(users.items()):
            if not isinstance(item, dict):
                continue
            result.append({
                "username": username,
                "port": int(item.get("port", 0)),
                "created_at": item.get("created_at"),
                "last_used_at": item.get("last_used_at"),
                "data_dir": item.get("data_dir"),
            })
        return result


def _port_is_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


class InstanceManager:
    """Launch and recycle local per-user app processes."""

    def __init__(self, base_dir: Path, registry: UserRegistry, host: str = "127.0.0.1", idle_timeout_seconds: int = 1800):
        self.base_dir = Path(base_dir)
        self.registry = registry
        self.host = host
        self.idle_timeout_seconds = idle_timeout_seconds
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[Any]] = {}

    def ensure_running(self, username: str) -> UserRecord:
        record = self.registry.get_user(username)
        if record is None:
            raise KeyError("用户不存在")
        with self._lock:
            process = self._processes.get(record.username)
            if process is not None and process.poll() is None and _port_is_open(self.host, record.port):
                self.registry.touch_user(record.username)
                return record
            if _port_is_open(self.host, record.port):
                self.registry.touch_user(record.username)
                return record
            self._processes.pop(record.username, None)
            self._start_process(record)
        self.registry.touch_user(record.username)
        return record

    def _start_process(self, record: UserRecord) -> None:
        log_path = record.user_root / "logs" / "instance.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["CHAT_HOST"] = self.host
        env["CHAT_PORT"] = str(record.port)
        env["CHAT_DATA_DIR"] = str(record.data_dir)
        env["PERSONA_PATH"] = str(record.persona_path)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=str(self.base_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if process.stdout is not None:
            threading.Thread(
                target=self._relay_process_output,
                args=(record.username, process.stdout, log_path),
                daemon=True,
            ).start()
        self._processes[record.username] = process
        deadline = time.time() + 20
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"实例启动失败，退出码 {process.returncode}")
            if _port_is_open(self.host, record.port, timeout=0.2):
                return
            time.sleep(0.25)
        process.terminate()
        raise TimeoutError("实例启动超时")

    @staticmethod
    def _is_debug_log_line(line: str, in_workflow_debug_block: bool) -> tuple[bool, bool]:
        stripped = line.lstrip()
        if in_workflow_debug_block:
            return True, "[/workflow-debug]" not in stripped
        if stripped.startswith("[workflow-debug]"):
            return True, "[/workflow-debug]" not in stripped
        is_debug = stripped.startswith("[DEBUG]") or stripped.startswith("DEBUG:")
        return is_debug, False

    def _relay_process_output(self, username: str, output: Any, log_path: Path) -> None:
        in_workflow_debug_block = False
        with log_path.open("a", encoding="utf-8") as log_handle:
            for line in iter(output.readline, ""):
                log_handle.write(line)
                log_handle.flush()
                is_debug, in_workflow_debug_block = self._is_debug_log_line(line, in_workflow_debug_block)
                if is_debug:
                    GATEWAY_LOGGER.debug("instance=%s %s", username, line.rstrip())
        output.close()

    def restart_instance(self, username: str) -> UserRecord:
        normalized = validate_username(username)
        if self.registry.get_user(normalized) is None:
            raise KeyError("用户不存在")
        self.stop_instance(normalized)
        return self.ensure_running(normalized)

    def stop_instance(self, username: str) -> None:
        normalized = validate_username(username)
        with self._lock:
            process = self._processes.pop(normalized, None)
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def stop_all(self) -> None:
        usernames = list(self._processes)
        for username in usernames:
            self.stop_instance(username)

    def cleanup_idle_instances(self) -> list[str]:
        removed: list[str] = []
        now = datetime.now(timezone.utc)
        for item in self.registry.list_users():
            username = str(item["username"])
            last_used = str(item.get("last_used_at") or "")
            try:
                last_used_at = datetime.fromisoformat(last_used)
            except ValueError:
                continue
            if (now - last_used_at).total_seconds() < self.idle_timeout_seconds:
                continue
            with self._lock:
                process = self._processes.get(username)
                if process is None or process.poll() is not None:
                    self._processes.pop(username, None)
                    continue
            self.stop_instance(username)
            removed.append(username)
        return removed

    def list_runtime(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        registry_items = {item["username"]: item for item in self.registry.list_users()}
        for username, item in registry_items.items():
            with self._lock:
                process = self._processes.get(username)
                running = bool(process is not None and process.poll() is None)
            items.append({
                **item,
                "running": running or _port_is_open(self.host, int(item["port"]), timeout=0.1),
            })
        return items