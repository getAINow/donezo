"""Planner: a director's scout. Switched on per agent, it runs a fresh, sealed Claude session on a
schedule that searches the web for ideas that would improve the agent's product and files at most
MAX_PROPOSALS proposals on the agent's board ('proposed' cards). Only the user approves them.

The run reads untrusted web pages, so it has nothing to steal and one way to act: board_propose.
The seal lives in three places: the command line (session_orchestrator._planner_flags), the signal
MCP server (signal_mcp refuses every other tool for this session) and the empty run folder here.
Like team.py, this module never imports session_orchestrator at module level.
"""

import secrets
import uuid
from datetime import datetime, timedelta

import team

ROLE = "scout"           # team_role of the run's session, kind and playbook of its subtask
EFFORT = "medium"
MAX_BUDGET_USD = "2"     # claude -p --max-budget-usd for one run (there is no turn limit flag)
MAX_PROPOSALS = 3
BRIEF_MAX = 4000
EVERY_DEFAULT, EVERY_MIN, EVERY_MAX = 7, 1, 90
SEEN_MAX, DECIDED_MAX = 100, 20
TOOL = "board_propose"


def migrate(conn):
    """Called from init_db. Idempotent. One row per agent (session id) that ever saved settings."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS planners (
            owner_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            brief TEXT NOT NULL DEFAULT '',          -- the product brief: all the run sees of the product
            every_days INTEGER NOT NULL DEFAULT 7,
            next_run_at TEXT NOT NULL DEFAULT '',    -- '' while switched off
            updated_at TEXT NOT NULL DEFAULT '',
            waiting TEXT NOT NULL DEFAULT ''         -- agent asked for the brief: '' | 'ask' | 'run' (start once saved)
        );
    """)
    if "waiting" not in {r[1] for r in conn.execute("PRAGMA table_info(planners)")}:
        conn.execute("ALTER TABLE planners ADD COLUMN waiting TEXT NOT NULL DEFAULT ''")


def _in_days(n):
    return (datetime.now() + timedelta(days=n)).isoformat(timespec="seconds")


def _owner(ws, owner_id):
    """(session, error). ws '' = any workspace (MCP callers are already resolved)."""
    so = team._so()
    s = so.get_session(owner_id)
    if not s or (ws and (s.get("workspace_id") or so.DEFAULT_WORKSPACE) != ws):
        return None, "no such agent in this workspace"
    if team.is_team_session(s):
        return None, "sub-agents and verifiers have no planner"
    return s, ""


def settings(owner_id):
    return team._row("SELECT * FROM planners WHERE owner_id = ?", (owner_id,)) or {
        "owner_id": owner_id, "enabled": 0, "brief": "", "every_days": EVERY_DEFAULT,
        "next_run_at": "", "updated_at": "", "waiting": ""}


def save(ws, owner_id, enabled=None, brief=None, every_days=None):
    """None leaves a field as it is. Switching on schedules the first run every_days from now; with
    no brief yet it asks the agent to write one. The first non-empty brief starts a run that waits."""
    s, err = _owner(ws, owner_id)
    if err:
        return {"error": err}
    cur = settings(owner_id)
    brief = cur["brief"] if brief is None else str(brief).strip()
    if len(brief) > BRIEF_MAX:
        return {"error": f"the brief is at most {BRIEF_MAX} characters"}
    try:
        every = cur["every_days"] if every_days in (None, "") else int(every_days)
    except (TypeError, ValueError):
        return {"error": "every_days must be a whole number of days"}
    if not EVERY_MIN <= every <= EVERY_MAX:
        return {"error": f"every_days must be between {EVERY_MIN} and {EVERY_MAX}"}
    on = bool(cur["enabled"]) if enabled is None else bool(enabled)
    nxt = cur["next_run_at"]
    if not on:
        nxt = ""
    elif not cur["enabled"] or every != cur["every_days"] or not nxt:
        nxt = _in_days(every)
    team._exec("INSERT INTO planners (owner_id, enabled, brief, every_days, next_run_at, updated_at) "
               "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(owner_id) DO UPDATE SET enabled = excluded.enabled, "
               "brief = excluded.brief, every_days = excluded.every_days, "
               "next_run_at = excluded.next_run_at, updated_at = excluded.updated_at",
               (owner_id, int(on), brief, every, nxt, team._so()._now()))
    if brief and cur["waiting"]:
        team._exec("UPDATE planners SET waiting = '' WHERE owner_id = ?", (owner_id,))
        if cur["waiting"] == "run":
            try:
                r = start_scout(owner_id)
            except Exception as e:  # noqa: BLE001 — the brief is saved either way
                r = {"error": repr(e)}
            if r.get("error"):
                print(f"[planner] {s['name']}: {r['error']}", flush=True)
    elif not brief and on:
        ask(ws, owner_id, run=True)
    elif cur["waiting"] == "run" and cur["enabled"] and not on:
        team._exec("UPDATE planners SET waiting = 'ask' WHERE owner_id = ?", (owner_id,))
    if brief != cur["brief"]:     # the open Board tab reloads the panel
        so = team._so()
        so.publish({"type": "planner", "owner_id": owner_id,
                    "workspace_id": s.get("workspace_id") or so.DEFAULT_WORKSPACE})
    return settings(owner_id)


def ask(ws, owner_id, run=False, force=False):
    """Ask the agent to write (or update) its brief: one message. run = start a run once it is saved.
    While an ask is outstanding (asked, brief still empty) only force sends again."""
    s, err = _owner(ws, owner_id)
    if err:
        return {"error": err}
    cur = settings(owner_id)
    waiting = "run" if run or cur["waiting"] == "run" else "ask"
    send = force or not cur["waiting"] or bool(cur["brief"])
    team._exec("INSERT INTO planners (owner_id, waiting, updated_at) VALUES (?, ?, ?) "
               "ON CONFLICT(owner_id) DO UPDATE SET waiting = excluded.waiting",
               (owner_id, waiting, team._so()._now()))
    if send:
        name, brief = s["name"], cur["brief"]
        now = (f"Your current brief is below. Update it if it is out of date.\n\n{brief}\n\n" if brief else "")
        team._enqueue(owner_id,
                      f"[PLANNER BRIEF] Write your planner's product brief with "
                      f"planner_brief(from_role=\"{name}\", text=...).\n\n"
                      f"Say what the product is, who uses it, what it does today, what matters now, and "
                      f"what is not wanted. At most {BRIEF_MAX} characters.\n\n"
                      f"The planner types parts of this text into public web searches. Keep out secrets, "
                      f"tokens, private names, emails and file paths.\n\n{now}"
                      f"This is a status message: call planner_brief, no reply to the sender is needed.",
                      s.get("workspace_id") or team._so().DEFAULT_WORKSPACE)
    return {"asked": send, "waiting": waiting}


def _active(owner_id):
    q = ",".join("?" * len(team.ACTIVE))
    return team._row(f"SELECT * FROM subtasks WHERE director_id = ? AND kind = ? AND status IN ({q}) "
                     f"LIMIT 1", (owner_id, ROLE, *team.ACTIVE))


def _filed(st):
    """Proposals this run filed. One scout per agent at a time, so 'planner cards since it began'."""
    r = team._row("SELECT COUNT(*) n FROM board_cards WHERE owner_id = ? AND source = 'planner' "
                  "AND created_at >= ? AND (? = '' OR created_at <= ?)",
                  (st["director_id"], st["created_at"], st["ended_at"], st["ended_at"]))
    return r["n"] if r else 0


def view(ws, owner_id):
    """Settings + the last run, for the Board tab's planner panel."""
    _, err = _owner(ws, owner_id)
    if err:
        return {"error": err}
    st = team._row("SELECT * FROM subtasks WHERE director_id = ? AND kind = ? "
                   "ORDER BY created_at DESC, rowid DESC LIMIT 1", (owner_id, ROLE))
    last = None
    if st:
        last = {k: st[k] for k in ("id", "status", "reason", "created_at", "ended_at")}
        last.update(proposals=_filed(st), tokens=team._run_totals([st["session_id"]])["tokens"])
    return {"settings": settings(owner_id), "last": last, "running": bool(st and st["status"] in team.ACTIVE),
            "now": team._so()._now()}


# ─── The run ─────────────────────────────────────────────────────────────────

def _first_message(st, director, name, key, brief):
    rows = team._rows
    owner, ws = director["id"], st["workspace_id"]
    open_cards = rows("SELECT title, status FROM board_cards WHERE workspace_id = ? AND owner_id = ? "
                      "AND status IN ('todo', 'in_progress', 'proposed') ORDER BY id DESC LIMIT 200", (ws, owner))
    seen = rows("SELECT title, url FROM board_cards WHERE workspace_id = ? AND owner_id = ? "
                "AND source = 'planner' ORDER BY id DESC LIMIT ?", (ws, owner, SEEN_MAX))
    decided = rows("SELECT title, url, decision, decision_reason FROM board_cards WHERE workspace_id = ? "
                   "AND owner_id = ? AND source = 'planner' AND decision != '' "
                   "ORDER BY updated_at DESC, id DESC LIMIT ?", (ws, owner, DECIDED_MAX))

    def lines(items, fmt):
        return "\n".join(fmt.format(**x) for x in items) or "(none)"
    playbook = team.playbook_text(ROLE).replace("<ROLE_NAME>", name)
    return (f"[SCOUT {st['id']}] Today is {datetime.now():%Y-%m-%d}.\n\n{playbook}\n\n"
            f"## Product brief\n{brief}\n\n"
            f"## Open cards on the board (titles only)\n{lines(open_cards, '- [{status}] {title}')}\n\n"
            f"## Seen list: already proposed, never propose these again\n{lines(seen, '- {title} — {url}')}\n\n"
            f"## Past decisions by the user, newest first: learn from them\n"
            f"{lines(decided, '- {decision}: {title} ({url}) — reason: {decision_reason}')}\n\n"
            f"## Filing a proposal\nCall, once per proposal and at most {MAX_PROPOSALS} times:\n"
            f"{TOOL}(from_role=\"{name}\", subtask_id=\"{st['id']}\", key=\"{key}\", title=..., "
            f"body=..., url=\"https://...\")\n"
            f"The key proves the proposal comes from this run; never write it anywhere else. When "
            f"done, end your turn with a short report: what you searched, what you filed, what you "
            f"skipped and why.")


def start_scout(director_id):
    """A fresh sealed session for one planner run. No worktree, no verifier, no message at the end.
    Moves next_run_at forward before anything can fail, so a broken start is not retried in a loop."""
    so = team._so()
    d = so.get_session(director_id)
    if not d:
        return {"error": "agent not found"}
    if team.is_team_session(d):
        return {"error": "sub-agents and verifiers have no planner"}
    cfg = settings(director_id)
    if not cfg["brief"].strip():
        return {"error": "the planner has no product brief yet"}
    if _active(director_id):
        return {"error": "a scout is already running for this agent"}
    team._exec("UPDATE planners SET next_run_at = ? WHERE owner_id = ? AND enabled = 1",
               (_in_days(cfg["every_days"]), director_id))
    ws = d.get("workspace_id") or so.DEFAULT_WORKSPACE
    st_id, now = team._new_id(), so._now()
    team._exec("INSERT INTO subtasks (id, workspace_id, director_id, kind, playbook, title, brief, "
               "done_when, model, effort_start, effort, status, created_at, updated_at) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, 'queued', ?, ?)",
               (st_id, ws, director_id, ROLE, ROLE, f"Scout for {d['name']}", cfg["brief"],
                team.TEAM_MODEL, EFFORT, EFFORT, now, now))
    # A new empty folder: no repo, no files, no project settings or skills for the run to read.
    cwd = so.DB_DIR / "scout" / st_id
    cwd.mkdir(parents=True)
    sid, name, key = str(uuid.uuid4()), f"sub-{st_id[3:]}-scout", secrets.token_hex(8)
    so.register_session(sid, name, cwd=str(cwd), model=team.TEAM_MODEL, effort=EFFORT, workspace_id=ws)
    team._set_team(sid, director_id, ROLE)
    st = team._update(st_id, _from=("queued",), session_id=sid, status="running", run_key=key)
    sig = team._enqueue(sid, _first_message(st, d, name, key, cfg["brief"]), ws)
    team._attach(st_id, sig, _from=("running",))
    return {"id": st_id, "status": "running"}


def after_run(st, report):
    """team._after_work, for a run that ended well: passed, with however many proposals (0 is fine)."""
    done = team._update(st["id"], _from=("running",), status="passed", report=report, run_key="",
                        reason="")
    if done is not None:
        team._finish(done)


def propose(scout_name, subtask_id, key, title, body, url):
    """board_propose: the planner session of a running scout, with its key, files on the director's board."""
    so = team._so()
    st = team.get_subtask(subtask_id)
    if not st or st["kind"] != ROLE:
        return "⛔ no such scout run"
    me = so.get_session(st["session_id"]) or {}
    if st["status"] != "running" or me.get("name") != scout_name:
        return "⛔ only the scout of this run can file proposals, while it runs"
    if not st["run_key"] or not secrets.compare_digest(str(key or ""), st["run_key"]):
        return "⛔ wrong or missing key — use the key from your [SCOUT] task message"
    if _filed(st) >= MAX_PROPOSALS:
        return f"⛔ {MAX_PROPOSALS} proposals already filed — end your turn now"
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "⛔ url is required and must start with http:// or https://"
    norm = url.rstrip("/").lower()
    for c in team._rows("SELECT url FROM board_cards WHERE workspace_id = ? AND owner_id = ? AND url != ''",
                        (st["workspace_id"], st["director_id"])):
        if c["url"].rstrip("/").lower() == norm:
            return "⛔ already seen: that url is on the board — propose something else, or nothing"
    import board                        # noqa: PLC0415 — board imports team
    card = board.propose(st["workspace_id"], st["director_id"], title, body, url)
    if card.get("error"):
        return f"⛔ {card['error']}"
    return f"Proposal {card['id']} filed ({_filed(st)}/{MAX_PROPOSALS}). The user decides."


# ─── Schedule ────────────────────────────────────────────────────────────────

def run_due():
    """Start a scout for every enabled planner whose next run has come and whose agent still exists.
    Called at startup and on run_loop's sweep tick. Returns the started subtask ids."""
    so = team._so()
    started = []
    for p in team._rows("SELECT * FROM planners WHERE enabled = 1 AND next_run_at != '' "
                        "AND next_run_at <= ?", (so._now(),)):
        s = so.get_session(p["owner_id"])
        if not s or team.is_team_session(s):
            continue
        if not p["brief"].strip():      # no brief yet: ask once, start when it is saved
            ask("", p["owner_id"], run=True)
            continue
        try:
            r = start_scout(p["owner_id"])
        except Exception as e:  # noqa: BLE001 — one planner must not stop the others
            r = {"error": repr(e)}
        if r.get("error"):
            print(f"[planner] {s['name']}: {r['error']}", flush=True)
            # Refused before it moved next_run_at (e.g. one still running): wait a full interval.
            team._exec("UPDATE planners SET next_run_at = ? WHERE owner_id = ?",
                       (_in_days(p["every_days"]), p["owner_id"]))
        else:
            started.append(r["id"])
    return started


# ─── HTTP routes (team.routes adds them) ─────────────────────────────────────

def routes(body, reply, ws_of):
    import asyncio                                   # noqa: PLC0415
    from starlette.routing import Route              # noqa: PLC0415

    async def api_get(request):
        return reply(await asyncio.to_thread(view, ws_of(request), request.query_params.get("owner_id", "")))

    async def api_save(request):
        b = await body(request)
        res = save(ws_of(request, b), b.get("owner_id", ""), b.get("enabled"), b.get("brief"),
                   b.get("every_days"))
        return reply(res if res.get("error") else view(ws_of(request, b), b.get("owner_id", "")))

    async def api_scout(request):
        b = await body(request)
        ws, owner = ws_of(request, b), b.get("owner_id", "")
        _, err = _owner(ws, owner)
        if err:
            return reply({"error": err})
        if not settings(owner)["brief"].strip():
            return reply(ask(ws, owner, run=True))
        return reply(start_scout(owner), created=True)

    async def api_ask(request):
        b = await body(request)
        return reply(ask(ws_of(request, b), b.get("owner_id", ""), force=True))

    return [Route("/api/planner", api_get),
            Route("/api/planner", api_save, methods=["POST"]),
            Route("/api/planner/scout", api_scout, methods=["POST"]),
            Route("/api/planner/ask", api_ask, methods=["POST"])]
