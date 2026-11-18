"""Workflows: a director's named, ordered list of roles that will later run one after the other.

This module only STORES and validates definitions (no runner yet). Like board.py, it never imports
session_orchestrator at module level.
"""

import json
import re

import team

NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
MAX_STEPS = 12
PLAYBOOK_MAX = 20000
PURPOSE_MAX = 300
TOOLS_MAX = 100


def migrate(conn):
    """Called from init_db. Idempotent."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflows (
            director_id TEXT NOT NULL,                -- the director session that owns it
            name TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            purpose TEXT NOT NULL,
            steps TEXT NOT NULL,                      -- JSON list of {role, job, playbook, tools}
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (director_id, name)
        );
    """)


def _validate(name, purpose, steps):
    """(clean name, purpose, steps, error). Nothing is stored unless error is ''."""
    def bad(msg):
        return "", "", [], msg
    if not isinstance(name, str) or not NAME_RE.match(name):
        return bad("name must be 1 to 40 characters: lowercase letters, digits and dashes only.")
    purpose = purpose.strip() if isinstance(purpose, str) else ""
    if not purpose or len(purpose) > PURPOSE_MAX:
        return bad(f"purpose must be one non-empty line of at most {PURPOSE_MAX} characters.")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return bad(f"a workflow needs 1 to {MAX_STEPS} steps.")
    clean, seen = [], set()
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            return bad(f"step {i} must be an object with role, job, playbook and tools.")
        role = s.get("role")
        if not isinstance(role, str) or not NAME_RE.match(role):
            return bad(f"step {i}: role must be 1 to 40 characters: lowercase letters, digits and dashes only.")
        if role in team.PLAYBOOKS or role in team.SYSTEM_PLAYBOOKS:
            return bad(f"step {i}: role '{role}' is a built-in playbook name; choose another name.")
        if role in seen:
            return bad(f"step {i}: role '{role}' appears twice; every role name must be unique in a workflow.")
        seen.add(role)
        job, playbook, tools = s.get("job"), s.get("playbook"), s.get("tools", [])
        if not isinstance(job, str) or not job.strip():
            return bad(f"step {i} ({role}): job must not be empty.")
        if not isinstance(playbook, str) or not playbook.strip():
            return bad(f"step {i} ({role}): playbook must not be empty.")
        if len(playbook) > PLAYBOOK_MAX:
            return bad(f"step {i} ({role}): playbook is {len(playbook)} characters; the limit is {PLAYBOOK_MAX}.")
        if (not isinstance(tools, list) or len(tools) > TOOLS_MAX
                or not all(isinstance(t, str) and t.strip() for t in tools)):
            return bad(f"step {i} ({role}): tools must be a list of at most {TOOLS_MAX} non-empty tool names.")
        clean.append({"role": role, "job": job.strip(), "playbook": playbook, "tools": tools})
    return name, purpose, clean, ""


def define(ws, director_id, name, purpose, steps):
    """Create, or replace when the director already has that name. {'status', 'name', 'roles'} or {'error'}."""
    name, purpose, steps, err = _validate(name, purpose, steps)
    if err:
        return {"error": err}
    now = team._so()._now()
    old = get(director_id, name)
    team._exec(
        "INSERT INTO workflows (director_id, name, workspace_id, purpose, steps, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(director_id, name) DO UPDATE SET "
        "workspace_id = excluded.workspace_id, purpose = excluded.purpose, steps = excluded.steps, "
        "updated_at = excluded.updated_at",
        (director_id, name, ws, purpose, json.dumps(steps, ensure_ascii=False), now, now))
    return {"status": "replaced" if old else "created", "name": name, "roles": [s["role"] for s in steps]}


def _load(r):
    return {**r, "steps": json.loads(r["steps"])}


def list_for(director_id):
    return [_load(r) for r in team._rows(
        "SELECT * FROM workflows WHERE director_id = ? ORDER BY name", (director_id,))]


def get(director_id, name):
    r = team._row("SELECT * FROM workflows WHERE director_id = ? AND name = ?", (director_id, name))
    return _load(r) if r else None
