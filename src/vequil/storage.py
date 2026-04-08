from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import ROOT


class VequilStorage:
    """Lightweight durable storage for server-side state."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (ROOT / "data" / "vequil.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS resolutions (
                    finding_id TEXT PRIMARY KEY,
                    resolution TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS action_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workspaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    slug TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workspace_api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id INTEGER NOT NULL,
                    key_value TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );

                CREATE TABLE IF NOT EXISTS ingest_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    event_status TEXT NOT NULL,
                    event_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT,
                    tool_name TEXT,
                    cost_usd REAL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
                );
                """
            )

    def get_resolutions_map(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT finding_id, resolution FROM resolutions"
            ).fetchall()
        return {row["finding_id"]: row["resolution"] for row in rows}

    def upsert_resolution(self, finding_id: str, resolution: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO resolutions (finding_id, resolution, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(finding_id) DO UPDATE SET
                    resolution = excluded.resolution,
                    created_at = excluded.created_at
                """,
                (finding_id, resolution, datetime.now().isoformat()),
            )

    def insert_lead(self, email: str, ip: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO leads (email, ip, created_at) VALUES (?, ?, ?)",
                (email, ip, datetime.now().isoformat()),
            )

    def insert_action_log(self, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO action_logs (payload_json, created_at) VALUES (?, ?)",
                (json.dumps(payload), datetime.now().isoformat()),
            )

    def create_workspace(self, name: str, slug: str) -> dict[str, Any]:
        key_value = f"vk_ws_{secrets.token_urlsafe(24)}"
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO workspaces (name, slug, created_at) VALUES (?, ?, ?)",
                (name, slug, now),
            )
            workspace_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO workspace_api_keys (workspace_id, key_value, created_at, revoked_at)
                VALUES (?, ?, ?, NULL)
                """,
                (workspace_id, key_value, now),
            )
        return {
            "id": workspace_id,
            "name": name,
            "slug": slug,
            "ingest_api_key": key_value,
        }

    def workspace_exists(self, workspace_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()
        return bool(row)

    def list_workspaces(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, slug, created_at FROM workspaces ORDER BY id ASC"
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "slug": row["slug"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def resolve_workspace_by_key(self, key_value: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT w.id, w.name, w.slug
                FROM workspace_api_keys k
                JOIN workspaces w ON w.id = k.workspace_id
                WHERE k.key_value = ? AND k.revoked_at IS NULL
                """,
                (key_value,),
            ).fetchone()
        if not row:
            return None
        return {"id": int(row["id"]), "name": row["name"], "slug": row["slug"]}

    def create_workspace_api_key(self, workspace_id: int) -> dict[str, Any]:
        key_value = f"vk_ws_{secrets.token_urlsafe(24)}"
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO workspace_api_keys (workspace_id, key_value, created_at, revoked_at)
                VALUES (?, ?, ?, NULL)
                """,
                (workspace_id, key_value, now),
            )
            key_id = int(cur.lastrowid)
        return {
            "id": key_id,
            "workspace_id": workspace_id,
            "key_value": key_value,
            "created_at": now,
            "revoked_at": None,
        }

    def list_workspace_api_keys(self, workspace_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, workspace_id, key_value, created_at, revoked_at
                FROM workspace_api_keys
                WHERE workspace_id = ?
                ORDER BY id ASC
                """,
                (workspace_id,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "workspace_id": int(row["workspace_id"]),
                "key_value": row["key_value"],
                "created_at": row["created_at"],
                "revoked_at": row["revoked_at"],
            }
            for row in rows
        ]

    def revoke_workspace_api_key(self, workspace_id: int, key_id: int) -> bool:
        now = datetime.now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE workspace_api_keys
                SET revoked_at = ?
                WHERE id = ? AND workspace_id = ? AND revoked_at IS NULL
                """,
                (now, key_id, workspace_id),
            )
        return cur.rowcount > 0

    def insert_ingest_event(
        self,
        workspace_id: int,
        event_type: str,
        event_status: str,
        event_at: str,
        source: str,
        agent_id: str,
        session_id: str | None,
        tool_name: str | None,
        cost_usd: float | None,
        payload: dict[str, Any],
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO ingest_events (
                    workspace_id, event_type, event_status, event_at, source,
                    agent_id, session_id, tool_name, cost_usd, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    event_type,
                    event_status,
                    event_at,
                    source,
                    agent_id,
                    session_id,
                    tool_name,
                    cost_usd,
                    json.dumps(payload),
                    datetime.now().isoformat(),
                ),
            )
            return int(cur.lastrowid)
