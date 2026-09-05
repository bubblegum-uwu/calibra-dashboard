"""
tracing.py -- instrumentation + storage layer, v2.

Added in v2:
  - users table: username, password_hash, role (admin/labeler)
  - labels table: replaces ground_truth/*.json files
  - All existing JSON labels migrated in via migrate_json_labels()
  - Safe migrations for existing traces.db (ALTER TABLE only adds columns)
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Optional

DB_PATH = "traces.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")   # safer for concurrent Flask

    # -- existing tables (unchanged) --
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            final_answer TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            group_id TEXT
        )
    """)
    existing_run_cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
    if "group_id" not in existing_run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN group_id TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS spans (
            span_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            name TEXT NOT NULL,
            input TEXT,
            output TEXT,
            duration_ms REAL,
            error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(run_id)
        )
    """)

    # -- new: users --
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'labeler',
            created_at TEXT NOT NULL
        )
    """)

    # -- new: labels (replaces ground_truth/*.json) --
    conn.execute("""
        CREATE TABLE IF NOT EXISTS labels (
            label_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            task TEXT,
            label TEXT NOT NULL,
            failure_category TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(run_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    # Unique constraint: one label per (run_id, user_id)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_labels_run_user
        ON labels (run_id, user_id)
    """)

    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def create_user(conn: sqlite3.Connection, username: str, password_hash: str,
                role: str = "labeler") -> int:
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        (username, password_hash, role, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT user_id, username, password_hash, role FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if not row:
        return None
    return {"user_id": row[0], "username": row[1], "password_hash": row[2], "role": row[3]}


def get_user_by_id(conn: sqlite3.Connection, user_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT user_id, username, role FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    return {"user_id": row[0], "username": row[1], "role": row[2]}


def list_users(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT user_id, username, role, created_at FROM users ORDER BY created_at"
    ).fetchall()
    return [{"user_id": r[0], "username": r[1], "role": r[2], "created_at": r[3]} for r in rows]


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

def upsert_label(conn: sqlite3.Connection, run_id: str, user_id: int,
                 label: str, failure_category: Optional[str] = None,
                 notes: str = "", task: str = "") -> None:
    conn.execute("""
        INSERT INTO labels (run_id, user_id, task, label, failure_category, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (run_id, user_id) DO UPDATE SET
            label = excluded.label,
            failure_category = excluded.failure_category,
            notes = excluded.notes,
            created_at = excluded.created_at
    """, (run_id, user_id, task, label, failure_category, notes,
          datetime.now(timezone.utc).isoformat()))
    conn.commit()


def delete_label(conn: sqlite3.Connection, run_id: str, user_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM labels WHERE run_id = ? AND user_id = ?", (run_id, user_id)
    )
    conn.commit()
    return cur.rowcount > 0


def get_labels_for_run(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """All labels for a run, one per user."""
    rows = conn.execute("""
        SELECT l.label_id, l.run_id, l.user_id, u.username, l.label,
               l.failure_category, l.notes, l.created_at
        FROM labels l JOIN users u ON l.user_id = u.user_id
        WHERE l.run_id = ?
        ORDER BY l.created_at
    """, (run_id,)).fetchall()
    return [{
        "label_id": r[0], "run_id": r[1], "user_id": r[2], "username": r[3],
        "label": r[4], "failure_category": r[5], "notes": r[6], "created_at": r[7],
    } for r in rows]


def load_all_labels(conn: sqlite3.Connection) -> dict:
    """
    Returns a flat dict {run_id: {label, failure_category, notes}} using the
    most recent label per run (across all users). Used by calibrate.py and
    regression_check.py for backward compatibility.
    Admin labels take precedence over labeler labels when both exist.
    """
    rows = conn.execute("""
        SELECT l.run_id, l.label, l.failure_category, l.notes, u.role
        FROM labels l JOIN users u ON l.user_id = u.user_id
        ORDER BY CASE u.role WHEN 'admin' THEN 0 ELSE 1 END, l.created_at DESC
    """).fetchall()
    result = {}
    for run_id, label, cat, notes, role in rows:
        if run_id not in result:
            result[run_id] = {"label": label, "failure_category": cat, "notes": notes or ""}
    return result


def label_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT label, COUNT(*) FROM labels GROUP BY label"
    ).fetchall()
    counts = {r[0]: r[1] for r in rows}
    return {
        "pass_count": counts.get("pass", 0),
        "fail_count": counts.get("fail", 0),
        "total_labeled": sum(counts.values()),
    }


# ---------------------------------------------------------------------------
# Migration: JSON -> SQLite labels
# ---------------------------------------------------------------------------

def migrate_json_labels(conn: sqlite3.Connection, json_dir: str = "ground_truth",
                        default_username: str = "admin") -> int:
    """
    One-time migration: reads all *.json files in json_dir and writes them
    into the labels table attributed to default_username (created if needed).
    Skips runs already labeled by this user. Returns count of new labels added.
    """
    import glob
    import os
    from werkzeug.security import generate_password_hash

    # Ensure admin user exists
    user = get_user_by_username(conn, default_username)
    if not user:
        pw_hash = generate_password_hash("changeme")
        user_id = create_user(conn, default_username, pw_hash, role="admin")
        print(f"Created user '{default_username}' (password: changeme — change this!)")
    else:
        user_id = user["user_id"]

    # Load all JSON labels
    all_labels: dict = {}
    for path in glob.glob(os.path.join(json_dir, "*.json")):
        try:
            with open(path) as f:
                all_labels.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass

    # Check which are already in DB for this user
    existing = set(r[0] for r in conn.execute(
        "SELECT run_id FROM labels WHERE user_id = ?", (user_id,)
    ).fetchall())

    count = 0
    for run_id, data in all_labels.items():
        if run_id in existing:
            continue
        label = data.get("label")
        if label not in ("pass", "fail"):
            continue
        upsert_label(
            conn,
            run_id=run_id,
            user_id=user_id,
            label=label,
            failure_category=data.get("failure_category"),
            notes=data.get("notes", "") or "",
            task=data.get("task", "") or "",
        )
        count += 1

    print(f"Migrated {count} labels from {json_dir}/*.json -> labels table (user: {default_username})")
    return count


# ---------------------------------------------------------------------------
# Tracer (unchanged API, uses new init_db)
# ---------------------------------------------------------------------------

class Tracer:
    """One Tracer wraps one SQLite connection. Create one per process."""

    def __init__(self, db_path: str = DB_PATH):
        self.conn = init_db(db_path)
        self._step_counters: dict[str, int] = {}

    def start_run(self, task: str, group_id: Optional[str] = None) -> str:
        run_id = str(uuid.uuid4())[:8]
        self.conn.execute(
            "INSERT INTO runs (run_id, task, final_answer, started_at, ended_at, group_id) "
            "VALUES (?, ?, NULL, ?, NULL, ?)",
            (run_id, task, datetime.now(timezone.utc).isoformat(), group_id),
        )
        self.conn.commit()
        self._step_counters[run_id] = 0
        return run_id

    def end_run(self, run_id: str, final_answer: str) -> None:
        self.conn.execute(
            "UPDATE runs SET final_answer = ?, ended_at = ? WHERE run_id = ?",
            (final_answer, datetime.now(timezone.utc).isoformat(), run_id),
        )
        self.conn.commit()

    def log_span(self, run_id: str, name: str, input_data: Any,
                 output_data: Any, duration_ms: float,
                 error: Optional[str] = None) -> None:
        self._step_counters[run_id] = self._step_counters.get(run_id, 0) + 1
        span_id = str(uuid.uuid4())[:8]
        self.conn.execute(
            "INSERT INTO spans "
            "(span_id, run_id, step_index, name, input, output, duration_ms, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                span_id, run_id, self._step_counters[run_id], name,
                json.dumps(input_data, default=str),
                json.dumps(output_data, default=str),
                duration_ms, error,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def get_trace(self, run_id: str):
        run = self.conn.execute(
            "SELECT run_id, task, final_answer, started_at, ended_at FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        spans = self.conn.execute(
            "SELECT step_index, name, input, output, duration_ms, error "
            "FROM spans WHERE run_id = ? ORDER BY step_index",
            (run_id,),
        ).fetchall()
        return run, spans

    def get_all_runs_full(self) -> list[dict]:
        runs = self.conn.execute(
            "SELECT run_id, task, final_answer, started_at, ended_at, group_id "
            "FROM runs ORDER BY started_at DESC"
        ).fetchall()
        result = []
        for run_id, task, final_answer, started_at, ended_at, group_id in runs:
            spans = self.conn.execute(
                "SELECT step_index, name, input, output, duration_ms, error "
                "FROM spans WHERE run_id = ? ORDER BY step_index",
                (run_id,),
            ).fetchall()
            substeps = [
                {"step_index": s[0], "name": s[1], "input": s[2],
                 "output": s[3], "duration_ms": s[4], "error": s[5]}
                for s in spans
            ]
            result.append({
                "run_id": run_id, "task": task, "final_answer": final_answer,
                "started_at": started_at, "ended_at": ended_at, "group_id": group_id,
                "total_runtime_ms": round(sum(s["duration_ms"] or 0 for s in substeps), 1),
                "substeps": substeps,
            })
        return result

    def pretty_print(self, run_id: str) -> None:
        run, spans = self.get_trace(run_id)
        if run is None:
            print(f"No run found with id {run_id}")
            return
        _, task, final_answer, started_at, ended_at = run
        print(f"\n=== Run {run_id} ===")
        print(f"Task: {task}")
        print(f"Final answer: {final_answer}")
        for step_index, name, input_data, output_data, duration_ms, error in spans:
            print(f"\nStep {step_index}: {name}  ({duration_ms}ms)")
            print(f"  input:  {input_data}")
            print(f"  output: {output_data}")
            if error:
                print(f"  ERROR: {error}")


# ---------------------------------------------------------------------------
# @traced decorator (unchanged)
# ---------------------------------------------------------------------------

def traced(name: str, tracer: Tracer, summarize=None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, run_id: Optional[str] = None, **kwargs):
            start = time.perf_counter()
            error = None
            result = None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:
                error = str(e)
                raise
            finally:
                duration_ms = round((time.perf_counter() - start) * 1000, 1)
                if run_id:
                    logged_input = kwargs if kwargs else args
                    logged_output = summarize(result) if (summarize and result is not None) else result
                    tracer.log_span(run_id, name, logged_input, logged_output, duration_ms, error)
        return wrapper
    return decorator
