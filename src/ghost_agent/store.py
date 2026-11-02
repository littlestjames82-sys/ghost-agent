"""JSON/JSONL store rooted at GHOST_AGENT_HOME (default ~/.ghost-agent/).

Entities: tasks, memories, skills, approvals (JSON files) and an
append-only trail (trail.jsonl). Zero dependencies, stdlib only.

The trail is the audit spine: append_trail() flushes and fsyncs, and
raises on failure so callers can refuse to execute an action whose
record could not be written (record-before-execute).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def default_home() -> Path:
    env = os.environ.get("GHOST_AGENT_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".ghost-agent"


class Store:
    def __init__(self, home: str | Path | None = None):
        self.home = Path(home).expanduser() if home else default_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.workspace = self.home / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.trail_path = self.home / "trail.jsonl"

    # ---- generic JSON collections -------------------------------------
    def _path(self, name: str) -> Path:
        return self.home / f"{name}.json"

    def _load(self, name: str) -> dict:
        p = self._path(name)
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, name: str, data: dict) -> None:
        p = self._path(name)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, p)

    def put(self, collection: str, entity: dict) -> dict:
        data = self._load(collection)
        entity = dict(entity)
        entity.setdefault("created_at", now_iso())
        entity["updated_at"] = now_iso()
        data[entity["id"]] = entity
        self._save(collection, data)
        return entity

    def get(self, collection: str, entity_id: str) -> dict | None:
        return self._load(collection).get(entity_id)

    def list(self, collection: str) -> list[dict]:
        items = list(self._load(collection).values())
        items.sort(key=lambda e: e.get("created_at", ""))
        return items

    def update(self, collection: str, entity_id: str, **fields) -> dict | None:
        data = self._load(collection)
        entity = data.get(entity_id)
        if entity is None:
            return None
        entity.update(fields)
        entity["updated_at"] = now_iso()
        data[entity_id] = entity
        self._save(collection, data)
        return entity

    # ---- trail (append-only) -------------------------------------------
    def append_trail(self, event_type: str, detail: dict | None = None,
                     task_id: str | None = None, step_index: int | None = None,
                     tool: str | None = None) -> dict:
        """Append one trail event. Raises OSError if the write fails.

        This is the mandatory record-before-execute write: callers must
        let the exception propagate and refuse to execute.
        """
        event = {
            "id": new_id("ev"),
            "ts": now_iso(),
            "ts_ns": time.time_ns(),
            "type": event_type,
            "task_id": task_id,
            "step_index": step_index,
            "tool": tool,
            "detail": detail or {},
        }
        line = json.dumps(event, sort_keys=True) + "\n"
        # open in append mode; if trail_path is a directory or otherwise
        # unwritable this raises, which is exactly the fault we want.
        with open(self.trail_path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        return event

    def try_trail(self, *args, **kwargs) -> dict | None:
        """Best-effort trail write for non-execution bookkeeping events."""
        try:
            return self.append_trail(*args, **kwargs)
        except OSError:
            return None

    def read_trail(self, task_id: str | None = None, limit: int = 200) -> list[dict]:
        if not self.trail_path.exists() or not self.trail_path.is_file():
            return []
        events = []
        try:
            lines = self.trail_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if task_id is None or ev.get("task_id") == task_id:
                events.append(ev)
        return events[-limit:]
