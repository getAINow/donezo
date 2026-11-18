"""Workflows: a director's named, ordered list of roles that run one after the other.

Definitions (workflows) and runs (workflow_runs). A run snapshots the steps at start, then runs
each role as a fresh team session (team_role 'role') in the director's folder. A role ends its
step with finish_step; the run advances when that role's turn ends (after_run), never mid-turn.
Like board.py, it never imports session_orchestrator at module level.
"""

import json
import re
import secrets
import sqlite3

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
        CREATE TABLE IF NOT EXISTS workflow_runs (
            id TEXT PRIMARY KEY,                      -- wf_<hex>
            director_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            name TEXT NOT NULL,
            purpose TEXT NOT NULL,
            job TEXT NOT NULL,
            steps TEXT NOT NULL,                      -- snapshot at start: redefining never changes a run
            status TEXT NOT NULL DEFAULT 'running',   -- running | done | stopped | cancelled
            step INTEGER NOT NULL DEFAULT 0,          -- 0-based index of the current step
            session_id TEXT NOT NULL DEFAULT '',      -- the current role session
            signal_id INTEGER,                        -- its task message
            result TEXT NOT NULL DEFAULT '',          -- finish_step of the current visit (JSON)
            note TEXT NOT NULL DEFAULT '',            -- findings / failure reason for the next start
            reports TEXT NOT NULL DEFAULT '{}',       -- {role: report}, latest visit of each
            rejects TEXT NOT NULL DEFAULT '{}',       -- {step index: times sent back to it}
            retried INTEGER NOT NULL DEFAULT 0,       -- own failures in this step visit
            spawned INTEGER NOT NULL DEFAULT 0,       -- role sessions made (unique session names)
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_one ON workflow_runs(director_id)
            WHERE status = 'running';
        CREATE INDEX IF NOT EXISTS idx_workflow_runs_signal ON workflow_runs(signal_id);
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


# ─── Runs ────────────────────────────────────────────────────────────────────

ROLE = "role"           # team_role of a step's session
WRITE_PLAYBOOK = "feature"


def _load_run(r):
    if not r:
        return None
    return {**r, "steps": json.loads(r["steps"]), "reports": json.loads(r["reports"]),
            "rejects": json.loads(r["rejects"]), "result": json.loads(r["result"] or "{}")}


def get_run(run_id):
    return _load_run(team._row("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)))


def running_for(director_id):
    return _load_run(team._row("SELECT * FROM workflow_runs WHERE director_id = ? AND status = 'running'",
                               (director_id,)))


def _update_run(run_id, _from=("running",), **fields):
    """Write only while the run is still in a _from status, so a cancel during a transition is
    never overwritten. None = not written."""
    now = team._so()._now()
    for k in ("reports", "rejects", "result"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    fields["updated_at"] = now
    if fields.get("status") in ("done", "stopped", "cancelled"):
        fields["ended_at"] = now
    cols = ", ".join(f"{k} = ?" for k in fields)
    n = team._exec_n(f"UPDATE workflow_runs SET {cols} WHERE id = ? AND status IN ({','.join('?' * len(_from))})",
                     (*fields.values(), run_id, *_from))
    return get_run(run_id) if n else None


def start(ws, director_id, name, job, from_step=1):
    """Start (or resume from from_step) a run; the first role starts in the background.
    {'id', 'step'} or {'error'}."""
    job = str(job or "").strip()
    if running_for(director_id):
        return {"error": "you already have a running workflow — wait for it or cancel_workflow first."}
    wf = get(director_id, name)
    if not wf:
        return {"error": f"no workflow named '{name}' (list_workflows)."}
    steps = wf["steps"]
    if not isinstance(from_step, int) or not 1 <= from_step <= len(steps):
        return {"error": f"from_step must be between 1 and {len(steps)}."}
    reports = {}
    if from_step == 1:
        if not job:
            return {"error": "job must not be empty: say what this run should produce."}
    else:
        prev = _load_run(team._row("SELECT * FROM workflow_runs WHERE director_id = ? AND name = ? "
                                   "ORDER BY created_at DESC, rowid DESC LIMIT 1", (director_id, name)))
        if not prev:
            return {"error": f"there is no earlier run of '{name}' to resume from."}
        missing = [s["role"] for s in steps[:from_step - 1] if not prev["reports"].get(s["role"])]
        if missing:
            return {"error": f"the latest run of '{name}' has no report for: {', '.join(missing)} — "
                             f"resume from an earlier step."}
        reports = {s["role"]: prev["reports"][s["role"]] for s in steps[:from_step - 1]}
        job = job or prev["job"]
    run_id, now = "wf_" + secrets.token_hex(4), team._so()._now()
    try:
        team._exec("INSERT INTO workflow_runs (id, director_id, workspace_id, name, purpose, job, steps, "
                   "step, reports, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (run_id, director_id, ws, name, wf["purpose"], job, json.dumps(steps, ensure_ascii=False),
                    from_step - 1, json.dumps(reports, ensure_ascii=False), now, now))
    except sqlite3.IntegrityError:          # idx_workflow_runs_one: a run started in the meantime
        return {"error": "you already have a running workflow — wait for it or cancel_workflow first."}
    team._spawn_bg(_start_step(run_id))
    return {"id": run_id, "step": from_step, "total": len(steps)}


def _step_msg(run, sess_name):
    i, steps = run["step"], run["steps"]
    s = steps[i]
    parts = [f"[WORKFLOW STEP {run['id']} · step {i + 1}/{len(steps)} · {s['role']}]",
             f"Workflow: {run['name']} — {run['purpose']}",
             f"\n## The run's job\n{run['job']}",
             f"\n## Your job ({s['role']})\n{s['job']}"]
    earlier = [f"### {k}. {e['role']}\n{run['reports'][e['role']]}"
               for k, e in enumerate(steps[:i], 1) if run["reports"].get(e["role"])]
    if earlier:
        parts.append("\n## Reports of the earlier steps\n" + "\n\n".join(earlier))
    if run["note"]:
        parts.append(f"\n## Why you are running this step again\n{run['note']}")
    back = ("" if i == 0 else
            f' If an earlier step\'s work is not good enough to build on, call instead: result="reject", '
            f'back_to="<earlier role, empty = {steps[i - 1]["role"]}>", report="<findings: what is wrong '
            f'and what it needs>".')
    parts.append(f"\nWhen you finish, call exactly once:\nfinish_step(from_role=\"{sess_name}\", "
                 f"result=\"done\", report=\"<what you produced, where, and the evidence>\", "
                 f"workspace_id=\"{run['workspace_id']}\").{back}\nThen end your turn. Do not call send_signal.")
    return "\n".join(parts)


async def _start_step(run_id):
    """Fresh role session for the current step, in the director's folder, then its task message."""
    try:
        so = team._so()
        run = get_run(run_id)
        if not run or run["status"] != "running":
            return
        director = so.get_session(run["director_id"])
        if not director:
            return _end(run, "cancelled", "the director was removed")
        s, n = run["steps"][run["step"]], run["spawned"] + 1
        run = _update_run(run_id, spawned=n, session_id="", signal_id=None, result="")
        if run is None:
            return
        name = f"sub-{run_id[3:]}-{n}-{s['role']}"
        effort = team.default_effort(WRITE_PLAYBOOK)
        sess, err = await team._spawn_session(name, director.get("cwd") or "", "", effort, director,
                                              run["workspace_id"], "", team.builder_model("", effort),
                                              skill=s["playbook"])
        if err:
            return _end(get_run(run_id), "stopped", f"could not start step {run['step'] + 1} ({s['role']}): {err}")
        team._set_team(sess["id"], director["id"], ROLE)
        if _update_run(run_id, session_id=sess["id"]) is None:
            return team._drop_skill(sess.get("cwd"), name)      # cancelled while it was being made
        sig = team._enqueue(sess["id"], _step_msg(run, name), run["workspace_id"])
        if _update_run(run_id, signal_id=sig) is None:
            team._exec("UPDATE signals SET status = 'denied', reason = 'workflow cancelled' "
                       "WHERE id = ? AND status IN ('pending', 'approved')", (sig,))
            team._drop_skill(sess.get("cwd"), name)
    except Exception as e:  # noqa: BLE001 — a broken run must end with a reason, not hang
        print(f"[workflow] {run_id} error: {e!r}", flush=True)
        _end(get_run(run_id), "stopped", f"orchestrator error: {e}")


def _resume_hint(run, idx):
    return (f"Resume from step {idx + 1} ({run['steps'][idx]['role']}): "
            f"run_workflow(name=\"{run['name']}\", job=\"\", from_step={idx + 1}).")


def _end(run, status, reason="", resume=None):
    """Close a still-running run; done/stopped tell the director exactly once."""
    if not run:
        return
    run = _update_run(run["id"], status=status, reason=reason[:2000])
    if run is None or status == "cancelled":
        return
    so = team._so()
    head = "[WORKFLOW DONE]" if status == "done" else "[WORKFLOW STOPPED]"
    lines = [f"{head} {run['id']} · {run['name']}", f"Job: {run['job']}"]
    if status == "stopped":
        lines.append(f"Reason: {reason}")
        lines.append(_resume_hint(run, run["step"] if resume is None else resume))
    lines.append("")
    for k, s in enumerate(run["steps"], 1):
        lines.append(f"## {k}. {s['role']}\n{run['reports'].get(s['role']) or '(no report)'}")
    lines.append("\nDo not reply to this.")
    if so.get_session(run["director_id"]):
        team._enqueue(run["director_id"], "\n".join(lines), run["workspace_id"])


def finish_step(session_id, result, report, back_to=""):
    """Record the current role's result. The run advances when that role's turn ends."""
    run = _load_run(team._row("SELECT * FROM workflow_runs WHERE session_id = ? AND status = 'running'",
                              (session_id,)))
    if not run:
        return "⛔ only the role that is the current step of a running workflow can call finish_step."
    result = str(result or "").strip().lower()
    if result not in ("done", "reject"):
        return "⛔ result must be done or reject."
    i, roles = run["step"], [s["role"] for s in run["steps"]]
    rec = {"result": result, "report": str(report or "")[:team.REPORT_MAX]}
    if result == "reject":
        if i == 0:
            return "⛔ the first step has no earlier step to send back to — use result=\"done\" and say what is wrong."
        back_to = str(back_to or "").strip() or roles[i - 1]
        if back_to not in roles:
            return f"⛔ back_to '{back_to}' is not a role of this workflow ({', '.join(roles[:i])})."
        if roles.index(back_to) >= i:
            return f"⛔ back_to must name an EARLIER role: {', '.join(roles[:i])}."
        rec["back_to"] = roles.index(back_to)
    if _update_run(run["id"], result=rec) is None:
        return "⛔ the workflow is no longer running."
    return f"Step {i + 1} ({roles[i]}) recorded as {result}. End your turn now."


async def after_run(signal, result):
    """process_signal calls this after every run. Acts only on the current role of a running run."""
    run = _load_run(team._row("SELECT * FROM workflow_runs WHERE signal_id = ? AND session_id = ? "
                              "AND status = 'running'", (signal.get("id"), signal.get("to_session"))))
    if not run:
        return
    try:
        await _advance(run, result)
    except Exception as e:  # noqa: BLE001 — a broken run must never kill the run loop
        print(f"[workflow] {run['id']} error: {e!r}", flush=True)
        _end(get_run(run["id"]), "stopped", f"orchestrator error: {e}")


async def _advance(run, result):
    so = team._so()
    sess = so.get_session(run["session_id"]) or {}
    team._drop_skill(sess.get("cwd"), sess.get("name"))
    i, steps, rec = run["step"], run["steps"], run["result"]
    role = steps[i]["role"]
    if result.get("status") not in ("done", "ok") or not rec:
        why = ("its run failed: " + str(result.get("reason") or result.get("result") or "unknown")[:300]
               if result.get("status") not in ("done", "ok")
               else "it ended its turn without calling finish_step")
        if run["retried"]:
            return _end(run, "stopped", f"step {i + 1} ({role}) failed twice — {why}")
        if _update_run(run["id"], retried=1, note=f"Your previous attempt at this step failed: {why}. "
                                                  f"Do the step again and end with finish_step.") is None:
            return
        return await _start_step(run["id"])
    if rec["result"] == "done":
        reports = {**run["reports"], role: rec["report"]}
        if i + 1 == len(steps):
            if _update_run(run["id"], reports=reports) is not None:
                _end(get_run(run["id"]), "done")
            return
        if _update_run(run["id"], reports=reports, step=i + 1, retried=0, note="") is None:
            return
        return await _start_step(run["id"])
    t = rec["back_to"]
    findings = rec["report"] or "(no findings given)"
    if run["rejects"].get(str(t)):
        return _end(run, "stopped", f"{role} sent the work back to step {t + 1} ({steps[t]['role']}) a "
                                    f"second time. Findings: {findings}", resume=t)
    if _update_run(run["id"], rejects={**run["rejects"], str(t): 1}, step=t, retried=0,
                   note=f"Step {i + 1} ({role}) sent the work back to you. Findings:\n{findings}") is None:
        return
    await _start_step(run["id"])


def cancel_run(director_id):
    """Cancel the director's running run: its task message denied, the role killed. No message."""
    run = running_for(director_id)
    if not run:
        return {"error": "you have no running workflow."}
    if run["signal_id"]:
        team._exec("UPDATE signals SET status = 'denied', reason = 'workflow cancelled' "
                   "WHERE id = ? AND status IN ('pending', 'approved')", (run["signal_id"],))
    if run["session_id"]:
        team._kill(run["session_id"])
    if _update_run(run["id"], status="cancelled", reason="cancelled by the director") is None:
        return {"error": "it ended in the meantime."}
    s = team._so().get_session(run["session_id"]) or {}
    team._drop_skill(s.get("cwd"), s.get("name"))
    return {"id": run["id"]}


def on_session_removed(session_id):
    """Director removed → its run is cancelled. Current role removed → the run stops."""
    if running_for(session_id):
        cancel_run(session_id)
    for r in team._rows("SELECT * FROM workflow_runs WHERE session_id = ? AND status = 'running'", (session_id,)):
        run = _load_run(r)
        _end(run, "stopped", f"the session of step {run['step'] + 1} ({run['steps'][run['step']]['role']}) "
                             f"was removed")


def on_signal_dropped(signal_id, why):
    for r in team._rows("SELECT * FROM workflow_runs WHERE signal_id = ? AND status = 'running'", (signal_id,)):
        run = _load_run(r)
        _end(run, "stopped", f"the task message of step {run['step'] + 1} "
                             f"({run['steps'][run['step']]['role']}) was {why} before it ran")


def reconcile():
    """Server start: a running run whose task message is no longer live has nobody to push it on."""
    for r in team._rows("SELECT * FROM workflow_runs WHERE status = 'running'"):
        sig = team._row("SELECT status FROM signals WHERE id = ?", (r["signal_id"],)) if r["signal_id"] else None
        if not (sig and sig["status"] in ("pending", "approved")):
            run = _load_run(r)
            _end(run, "stopped", f"the orchestrator restarted during step {run['step'] + 1} "
                                 f"({run['steps'][run['step']]['role']})")


def progress_line(director_id, name):
    run = running_for(director_id)
    if not run or run["name"] != name:
        return ""
    return f"  ▶ running {run['id']}: step {run['step'] + 1}/{len(run['steps'])} ({run['steps'][run['step']]['role']})"
