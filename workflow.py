"""Workflows: a director's named, ordered list of roles that run one after the other.

Definitions (workflows) and runs (workflow_runs). A run snapshots the steps at start, then runs
each role as a fresh team session (team_role 'role') in the director's folder. A role ends its
step with finish_step; the run advances when that role's turn ends (after_run), never mid-turn.
A role is a sub-director: it only reads, delegates to sub-agents (team.spawn_subagent), reviews and
merges. Its step spans several turns and waits while its sub-agents work or a message to it is
still queued; finish_step needs at least one of them ended (_delegated).
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

_READ = ("Read", "Grep", "Glob", "Bash(git *)")
BASICS = {
    ROLE: _READ + ("mcp__signal__spawn_subagent", "mcp__signal__list_subagents",
                   "mcp__signal__cancel_subagent", "mcp__signal__finish_step"),
    "subagent": _READ + ("Edit", "Write"),
    "verifier": _READ + ("mcp__signal__submit_verdict",),
}


def tool_list(kind, tools):
    """A role's sub-agent's (kind 'subagent') or its verifier's allow list: the basics + the step's
    tools, de-duplicated in order. Plain Bash = the full shell, so Bash(git *) is dropped.
    The role itself runs with BASICS[ROLE] only: the step's tools are for its sub-agents."""
    out = list(dict.fromkeys([*BASICS[kind], *(t.strip() for t in tools)]))
    return [t for t in out if t != "Bash(git *)"] if "Bash" in out else out


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


def role_run(session_id):
    """The running run whose current step this session is, else None."""
    return _load_run(team._row("SELECT * FROM workflow_runs WHERE session_id = ? AND status = 'running'",
                               (session_id,))) if session_id else None


LIVE = ("pending", "approved")


def _waiting(run, live=LIVE):
    """The current role still has a sub-agent at work or a message yet to run: its step is not over."""
    sid = run["session_id"]
    return bool(sid) and bool(team._active_count(sid) or team._row(
        f"SELECT 1 FROM signals WHERE to_session = ? AND status IN ({','.join('?' * len(live))}) LIMIT 1",
        (sid, *live)))


def _close_visit(run):
    """A step visit ends: the role's queued messages are denied, its active sub-agents cancelled and
    its SKILL folder (in the director's folder) dropped."""
    sid = run["session_id"]
    if not sid:
        return
    team._exec("UPDATE signals SET status = 'denied', reason = 'workflow step ended' "
               "WHERE to_session = ? AND status IN ('pending', 'approved')", (sid,))
    team.on_session_removed(sid)
    s = team._so().get_session(sid) or {}
    team._drop_skill(s.get("cwd"), s.get("name"))


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
    tools = f" Your sub-agents get these tools: {', '.join(s['tools'])}." if s["tools"] else ""
    parts.append(
        f"\n## How you work\nYou are a sub-director. You do no hands-on work yourself and you do not have "
        f"the tools for it: you read files, run git, delegate and review.{tools}\n"
        f"Split your job into units that can be verified on their own. For each unit call "
        f"spawn_subagent(from_role=\"{sess_name}\", playbook=..., brief=..., done_when=...). The brief must "
        f"stand on its own: the sub-agent cannot see this message. done_when is a check an independent "
        f"verifier can observe.\n"
        f"A sub-agent that only needs to read and report uses playbook \"investigate\". Work done only "
        f"through MCP tools (for example on a remote service) makes no commit: say so in done_when.\n"
        f"Then end your turn and wait for the [SUB-AGENT PASSED] or [SUB-AGENT FAILED] message. Review a "
        f"passed branch's diff and merge it into the current branch of this folder; never push. A failed "
        f"sub-agent means the brief or done_when was wrong: fix it before you spawn again.\n"
        f"Build your report from your sub-agents' verified results. Call finish_step only when nothing "
        f"you delegated is still active; it is refused until one of your sub-agents has passed or failed.")
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
                                              skill=s["playbook"], tools=list(BASICS[ROLE]))
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
    """Close a still-running run and its current step visit; done/stopped tell the director exactly once."""
    if not run:
        return
    run = _update_run(run["id"], status=status, reason=reason[:2000])
    if run is None:
        return
    _close_visit(run)
    so = team._so()
    if status == "cancelled":
        return
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


def _delegated(session_id):
    """A sub-agent of this role session ended passed or failed. Every step visit is a fresh session."""
    return bool(team._row("SELECT 1 FROM subtasks WHERE director_id = ? AND kind = 'subagent' "
                          "AND status IN ('passed', 'failed') LIMIT 1", (session_id,)))


def finish_step(session_id, result, report, back_to=""):
    """Record the current role's result. The run advances when that role's turn ends."""
    run = role_run(session_id)
    if not run:
        return "⛔ only the role that is the current step of a running workflow can call finish_step."
    if team._active_count(session_id):
        return ("⛔ a sub-agent you delegated is still active — end your turn and wait for its "
                "[SUB-AGENT PASSED]/[SUB-AGENT FAILED] message, or cancel_subagent it first.")
    if not _delegated(session_id):
        return ("⛔ you have not delegated any work yet: a role does no hands-on work itself. Split your "
                "job, spawn_subagent each unit, end your turn and wait for its [SUB-AGENT PASSED]/"
                "[SUB-AGENT FAILED] message, then finish_step. A cancelled sub-agent does not count.")
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
    """process_signal calls this after every run. Acts only on a turn of the current role of a
    running run; a turn that ends without a result while the role still waits leaves the step open."""
    run = role_run(signal.get("to_session"))
    if not run:
        return
    try:
        if result.get("status") in ("done", "ok") and not run["result"] and _waiting(run):
            return
        await _advance(run, result)
    except Exception as e:  # noqa: BLE001 — a broken run must never kill the run loop
        print(f"[workflow] {run['id']} error: {e!r}", flush=True)
        _end(get_run(run["id"]), "stopped", f"orchestrator error: {e}")


async def _advance(run, result):
    _close_visit(run)
    i, steps, rec = run["step"], run["steps"], run["result"]
    role = steps[i]["role"]
    if result.get("status") not in ("done", "ok") or not rec:
        why = ("its run failed: " + str(result.get("reason") or result.get("result") or "unknown")[:300]
               if result.get("status") not in ("done", "ok")
               else "it ended its turn without calling finish_step")
        if run["retried"]:
            return _end(run, "stopped", f"step {i + 1} ({role}) failed twice — {why}")
        # keep the findings of a rejection that sent the work here: the retry still needs them
        note = (run["note"] + "\n\n" if run["note"] else "") + (
            f"Your previous attempt at this step failed: {why}. Do the step again and end with finish_step.")
        if _update_run(run["id"], retried=1, note=note) is None:
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


def _cancel(run, reason):
    if run["session_id"]:
        team._kill(run["session_id"])
    if _update_run(run["id"], status="cancelled", reason=reason) is None:
        return {"error": "it ended in the meantime."}
    _close_visit(run)
    return {"id": run["id"]}


def cancel_run(director_id):
    """Cancel the director's running run: the role killed, its messages denied, its sub-agents
    cancelled. No message."""
    run = running_for(director_id)
    return _cancel(run, "cancelled by the director") if run else {"error": "you have no running workflow."}


def cancel_by_user(run_id):
    """The dashboard's Stop: same as cancel_run, and the director is told once."""
    run = get_run(run_id)
    if not run:
        return {"error": "not found"}
    if run["status"] != "running":
        return {"error": f"already {run['status']}"}
    res = _cancel(run, "cancelled by the user")
    if "error" not in res and team._so().get_session(run["director_id"]):
        team._enqueue(run["director_id"], f"[WORKFLOW CANCELLED] {run['id']} · {run['name']} was cancelled by "
                      f"the user from the dashboard. Do not reply to this.", run["workspace_id"])
    return res


def _role_sessions(run, sessions):
    """{role: id} of each role's LATEST session in this run (names are sub-<run id>-<n>-<role>; a retried
    or revisited role has several). A removed session is simply absent."""
    pre, best = f"sub-{run['id'][3:]}-", {}
    for s in sessions.values():
        n, _, role = s["name"][len(pre):].partition("-") if s["name"].startswith(pre) else ("", "", "")
        if n.isdigit() and role and int(n) > best.get(role, (0,))[0]:
            best[role] = (int(n), s["id"])
    return {role: v[1] for role, v in best.items()}


def overview(workspace_id, sessions, status=None):
    """The Team tab's workflow runs: every running one plus the latest ended ones, steps in order with the
    role's job, a state, its report and its latest session id. Playbooks are never sent.
    status None = the 5 latest ended; done|stopped|cancelled = the 10 latest of it; all = the 10 latest."""
    where, args, lim = "status != 'running'", [workspace_id], 5
    if status is not None:
        lim = 10
        if status in ("done", "stopped", "cancelled"):
            where, args = "status = ?", [workspace_id, status]
    rows = team._rows("SELECT * FROM workflow_runs WHERE workspace_id = ? AND status = 'running' "
                      "ORDER BY created_at DESC", (workspace_id,)) + team._rows(
        f"SELECT * FROM workflow_runs WHERE workspace_id = ? AND {where} "
        "ORDER BY ended_at DESC, rowid DESC LIMIT ?", (*args, lim))
    out = []
    for run in map(_load_run, rows):
        def state(i):
            if run["status"] == "done" or i < run["step"]:
                return "done"
            return ("current" if run["status"] == "running" else "stopped") if i == run["step"] else "pending"
        sess = _role_sessions(run, sessions)
        out.append({
            **{k: run[k] for k in ("id", "name", "job", "status", "reason", "created_at", "ended_at")},
            "director_name": (sessions.get(run["director_id"]) or {}).get("name", ""),
            "steps": [{"role": s["role"], "job": s["job"], "state": state(i),
                       "report": run["reports"].get(s["role"], ""),
                       "session_id": sess.get(s["role"], "")} for i, s in enumerate(run["steps"])]})
    return out


def definitions(workspace_id, sessions):
    """The Team tab's saved workflows: every definition of a director still in this workspace, by director
    name then workflow name. Steps carry the full job and tools; playbooks are never sent (get() has them)."""
    running = {(r["director_id"], r["name"]) for r in team._rows(
        "SELECT director_id, name FROM workflow_runs WHERE workspace_id = ? AND status = 'running'", (workspace_id,))}
    out = [{"director_id": w["director_id"], "director_name": sessions[w["director_id"]]["name"],
            "name": w["name"], "purpose": w["purpose"], "updated_at": w["updated_at"],
            "running": (w["director_id"], w["name"]) in running,
            "steps": [{k: p[k] for k in ("role", "job", "tools")} for p in w["steps"]]}
           for w in map(_load, team._rows("SELECT * FROM workflows WHERE workspace_id = ?", (workspace_id,)))
           if w["director_id"] in sessions]
    return sorted(out, key=lambda d: (d["director_name"], d["name"]))


def on_session_removed(session_id):
    """Director removed → its run is cancelled. Current role removed → the run stops."""
    if running_for(session_id):
        cancel_run(session_id)
    for r in team._rows("SELECT * FROM workflow_runs WHERE session_id = ? AND status = 'running'", (session_id,)):
        run = _load_run(r)
        _end(run, "stopped", f"the session of step {run['step'] + 1} ({run['steps'][run['step']]['role']}) "
                             f"was removed")


def on_signal_dropped(signal_id, why):
    """A message to the current role was denied: stop the run if the role has nothing left to wait for
    (a turn in progress counts: its end decides)."""
    sig = team._row("SELECT to_session FROM signals WHERE id = ?", (signal_id,))
    run = role_run(sig["to_session"]) if sig else None
    if not run or _waiting(run, LIVE + ("processing",)):
        return
    what = "the task message of" if signal_id == run["signal_id"] else "a message to"
    _end(run, "stopped", f"{what} step {run['step'] + 1} ({run['steps'][run['step']]['role']}) was {why} "
                         f"before it ran, and the step had nothing else to wait for")


def reconcile():
    """Server start, after team.reconcile: a running run whose role has no live message and no active
    sub-agent has nobody to push it on."""
    for r in team._rows("SELECT * FROM workflow_runs WHERE status = 'running'"):
        run = _load_run(r)
        if not _waiting(run):
            _end(run, "stopped", f"the orchestrator restarted during step {run['step'] + 1} "
                                 f"({run['steps'][run['step']]['role']})")


def progress_line(director_id, name):
    run = running_for(director_id)
    if not run or run["name"] != name:
        return ""
    return f"  ▶ running {run['id']}: step {run['step'] + 1}/{len(run['steps'])} ({run['steps'][run['step']]['role']})"
