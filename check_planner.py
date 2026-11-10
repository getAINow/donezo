#!/usr/bin/env python3
"""Guards for a director's planner: settings, the sealed scout run, board_propose and the schedule.

A scout run reads untrusted web pages. What can go wrong quietly:
  - the run inherits the director's folder, its bypass permission mode or the user's other MCP
    servers (documents, drive, notes), so a page it reads can steer it into reading or sending them;
  - a signal tool other than board_propose works for it (send_signal as the director, spawning work);
  - it files more than 3 proposals, re-proposes what the user already saw, or files on a board that
    is not the director's;
  - it costs the director a sub-agent slot, or messages the director when it ends;
  - the schedule starts a disabled planner, one for a removed agent, or the same one again and again;
  - a normal session's command line changes.

Runs the real state machine, MCP tools and HTTP routes against a throwaway database. The claude CLI
is a fake (the subprocess call is replaced, as a fake .exe cannot be written on Windows) that
records argv, cwd and the prompt. No real claude, no web request.

    python3 check_planner.py
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TMP = Path(tempfile.mkdtemp(prefix="check_planner_"))
BIN = TMP / "bin"
BIN.mkdir()
EXE = ".exe" if os.name == "nt" else ""
for name in ("claude", "agy"):
    f = BIN / (name + EXE)
    f.write_bytes(b"")
    f.chmod(0o755)
REAL_DB = Path.home() / ".session_orch_db"
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ.update({
    "HOME": str(TMP / "home"), "USERPROFILE": str(TMP / "home"),
    "ORCH_DB": "check_planner", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"), "ORCH_DRY_RUN": "1",
    "CLAUDE_BIN": str(BIN / ("claude" + EXE)), "ORCH_AGY_BIN": str(BIN / ("agy" + EXE)),
    "ORCH_SUBTASK_MAX_ATTEMPTS": "1", "ORCH_SUBAGENTS_MAX": "1", "ORCH_STREAM": "0",
})
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx                        # noqa: E402
import session_orchestrator as so   # noqa: E402
import signal_mcp                   # noqa: E402
import team                         # noqa: E402
import board                        # noqa: E402
import planner                      # noqa: E402

for _log in ("httpx", "mcp"):
    logging.getLogger(_log).setLevel(logging.WARNING)

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


so.DB_DIR = TMP / "db"
assert REAL_DB not in [so.DB_DIR, *so.DB_DIR.parents], so.DB_DIR
so.CLAUDE_PROJECTS_DIR = TMP / "claude-projects"
so.AGY_CONVERSATIONS_DIR = TMP / "agy-conversations"
so.STREAM = False
so.init_db()
team._worktrees_root = lambda: TMP / "worktrees"
signal_mcp._INPROC = True

WS = so.DEFAULT_WORKSPACE
WS_B = so.create_workspace("other")["id"]
PROJ = TMP / "proj-lead"
PROJ.mkdir()
(PROJ / "secret.txt").write_text("the director's files\n", encoding="utf-8")
for sid, name in (("dir-1", "lead"), ("dir-2", "peer"), ("dir-3", "third"), ("dir-4", "gone")):
    so.register_session(sid, name, cwd=str(PROJ), model="", permission_mode="bypassPermissions",
                        workspace_id=WS)
so.register_session("sub-x", "sub-x-feature", cwd=str(PROJ), model="", workspace_id=WS)
team._set_team("sub-x", "dir-1", "subagent")

# ── The fake claude CLI ───────────────────────────────────────────────────────
CALLS = []                      # {argv, cwd, files, prompt, mcp}
SCOUT = {"do": [], "crash": False, "results": []}   # what the fake scout files, and what it got back
PROPOSE_RE = re.compile(r'board_propose\(from_role="([^"]+)", subtask_id="([^"]+)", key="([^"]+)"')


class FakeProc:
    def __init__(self, argv, cwd):
        self.argv, self.cwd, self.returncode, self.pid = argv, cwd, 0, 0

    async def communicate(self, input=b""):
        prompt = (input or b"").decode("utf-8")
        cwd = Path(self.cwd or ".")
        mcp = None
        if "--mcp-config" in self.argv:
            mcp = json.loads(Path(self.argv[self.argv.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
        CALLS.append({"argv": list(self.argv), "cwd": str(cwd), "prompt": prompt, "mcp": mcp,
                      "files": sorted(os.listdir(cwd)) if cwd.is_dir() else None})
        sid = self.argv[self.argv.index("--session-id") + 1] if "--session-id" in self.argv else \
            self.argv[self.argv.index("--resume") + 1] if "--resume" in self.argv else str(uuid.uuid4())
        if "[SCOUT " in prompt:
            if SCOUT["crash"]:
                self.returncode = 1
                return b"", b"CLI died"
            m = PROPOSE_RE.search(prompt)
            for title, url in SCOUT["do"]:
                SCOUT["results"].append(await signal_mcp.board_propose(
                    from_role=m[1], subtask_id=m[2], key=m[3], title=title,
                    body="What it is: x\nWhy it fits this product: y", url=url))
        return json.dumps({"result": "report: searched, filed", "session_id": sid,
                           "usage": {"output_tokens": 321}}).encode(), b""

    async def wait(self):
        return self.returncode


async def fake_exec(*argv, cwd=None, **kw):
    if cwd and TMP.resolve() not in Path(cwd).resolve().parents:
        raise RuntimeError(f"refusing to run outside {TMP}: {cwd}")
    return FakeProc(argv, cwd)


so.asyncio.create_subprocess_exec = fake_exec


async def run_for_real(session, prompt, on_event=None, dry_run=False):
    """The real _run_claude (it builds the command line), against the fake CLI above."""
    so.DRY_RUN = False
    try:
        return await so._run_claude(session, prompt)
    finally:
        so.DRY_RUN = True


so.ENGINES["claude"].run = run_for_real


async def drain(rounds=300):
    for _ in range(rounds):
        sigs = so.eligible_signals()
        if sigs:
            for s in sigs:
                await so.process_signal(s)
            continue
        if team._bg:
            await asyncio.gather(*list(team._bg), return_exceptions=True)
            continue
        return


def call_with(marker):
    return next((c for c in CALLS if marker in c["prompt"]), None)


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def to_dir1_since(sig_id):
    return team._row("SELECT COUNT(*) n FROM signals WHERE to_session = 'dir-1' AND id > ?", (sig_id,))["n"]


def last_sig():
    return team._row("SELECT COALESCE(MAX(id), 0) n FROM signals")["n"]


APP = so.build_app()


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=APP), base_url="http://test")


async def case_a():
    async with client() as c:
        r = await c.get("/api/planner", params={"workspace_id": WS, "owner_id": "dir-1"})
        s = r.json().get("settings", {})
        check("a) a new agent's planner is off, no brief, every 7 days, no next run",
              r.status_code == 200 and (s.get("enabled"), s.get("brief"), s.get("every_days"),
                                        s.get("next_run_at")) == (0, "", 7, "") and r.json()["last"] is None,
              r.text)
        r = await c.post("/api/planner", json={"workspace_id": WS, "owner_id": "dir-1", "enabled": True})
        check("a) switching on without a brief is refused, readably",
              r.status_code == 400 and "brief" in r.json().get("error", "")
              and not planner.settings("dir-1")["enabled"], r.text)
        bad = [await c.post("/api/planner", json={"workspace_id": WS, "owner_id": "dir-1", "every_days": n})
               for n in (0, 91, "x")]
        check("a) every_days outside 1–90 is refused", all(x.status_code == 400 for x in bad),
              [x.text for x in bad])
        r = await c.post("/api/planner", json={"workspace_id": WS, "owner_id": "dir-1", "enabled": True,
                                               "brief": "A CLI todo app for developers", "every_days": 5})
        s = r.json().get("settings", {})
        want = datetime.now() + timedelta(days=5)
        got = datetime.fromisoformat(s.get("next_run_at") or "2000-01-01")
        check("a) switching on sets next_run_at to now + every_days",
              r.status_code == 200 and s.get("enabled") == 1 and abs((got - want).total_seconds()) < 60, r.text)
        n_calls = len(CALLS)
        check("a) switching on starts no run", len(CALLS) == n_calls and not planner._active("dir-1")
              and not so.eligible_signals(), str(so.eligible_signals()))
        r = await c.get("/api/planner", params={"workspace_id": WS_B, "owner_id": "dir-1"})
        check("a) another workspace cannot read it", r.status_code == 400, r.text)

    got = await signal_mcp.planner_brief(from_role="lead")
    check("a) planner_brief with no text reads the caller's brief", got == "A CLI todo app for developers", got)
    res = await signal_mcp.planner_brief(from_role="lead", text="A CLI todo app for developers who live in git")
    check("a) planner_brief sets the caller's own brief only",
          res == "Brief saved." and planner.settings("dir-1")["brief"].endswith("live in git")
          and planner.settings("dir-2")["brief"] == "" and planner.settings("dir-1")["enabled"], res)
    res = await signal_mcp.planner_brief(from_role="peer")
    check("a) another agent reads its own (empty) brief", res.startswith("No brief yet"), res)
    res = [await signal_mcp.planner_brief(from_role="sub-x-feature"),
           await signal_mcp.planner_brief(from_role="sub-x-feature", text="hijack")]
    check("a) a team session is refused by planner_brief",
          all(x.startswith("⛔") for x in res) and planner.settings("sub-x")["brief"] == "", str(res))


S = {}      # subtask ids by case


async def case_b():
    check("b) start_scout refuses a team session", "error" in planner.start_scout("sub-x"))
    check("b) start_scout refuses an empty brief", "brief" in planner.start_scout("dir-2").get("error", ""))
    # Board data for d): an open card, a seen proposal the user rejected with a reason.
    board.add(WS, "dir-1", "Ship dark mode")
    seen = board.propose(WS, "dir-1", "Old idea", "", "https://seen.example/old")
    team._exec("UPDATE board_cards SET status = 'dropped', decision = 'rejected', "
               "decision_reason = 'too heavy for a CLI' WHERE id = ?", (seen["id"],))
    folder_before = set(os.listdir(so.DB_DIR / "scout")) if (so.DB_DIR / "scout").is_dir() else set()
    r = planner.start_scout("dir-1")
    st = team.get_subtask(r.get("id", ""))
    S["b"], S["b_key"], S["b_folders"] = r.get("id"), (st or {}).get("run_key", ""), folder_before
    check("b) start_scout creates a running 'scout' subtask with a key",
          st and st["kind"] == "scout" and st["playbook"] == "scout" and st["status"] == "running"
          and st["run_key"] and st["director_id"] == "dir-1", str(st))
    check("b) a second scout for the same agent is refused while one is active",
          "already" in planner.start_scout("dir-1").get("error", ""))
    check("b) starting moves next_run_at to now + every_days",
          abs((datetime.fromisoformat(planner.settings("dir-1")["next_run_at"])
               - (datetime.now() + timedelta(days=5))).total_seconds()) < 60, planner.settings("dir-1"))
    res = await signal_mcp.spawn_subagent(from_role="lead", playbook="investigate", brief="look at x",
                                          done_when="answer printed")
    check("b) a scout takes no sub-agent slot: spawn_subagent works at a limit of 1",
          team.MAX_ACTIVE == 1 and res.startswith("Sub-agent st_"), res)
    S["sub"] = res.split()[1] if res.startswith("Sub-agent") else ""
    await drain()


async def case_c():
    c = call_with(f"[SCOUT {S['b']}]")
    if not c:
        return check("c) the scout ran through the fake CLI", False, str([x["prompt"][:40] for x in CALLS]))
    argv, cwd = c["argv"], Path(c["cwd"])
    check("c) cwd is a new empty folder under the temp DB dir, not the director's",
          so.DB_DIR.resolve() in cwd.resolve().parents and c["files"] == [] and cwd.name == S["b"]
          and S["b"] not in S["b_folders"] and cwd.resolve() != PROJ.resolve(), f"{cwd} {c['files']}")
    tools = flag(argv, "--tools")
    check("c) --tools is exactly WebSearch and WebFetch",
          argv.count("--tools") == 1 and sorted((tools or "").replace(",", " ").split()) == ["WebFetch", "WebSearch"],
          str(argv))
    servers = (c["mcp"] or {}).get("mcpServers", {})
    check("c) --strict-mcp-config with an --mcp-config naming only the signal server",
          "--strict-mcp-config" in argv and argv.count("--mcp-config") == 1 and list(servers) == ["signal"]
          and servers["signal"]["url"].endswith("/signal/mcp"), f"{argv} {c['mcp']}")
    allowed = flag(argv, "--allowedTools")
    check("c) --allowedTools is exactly WebSearch, WebFetch and the board_propose tool",
          argv.count("--allowedTools") == 1 and sorted((allowed or "").split())
          == ["WebFetch", "WebSearch", "mcp__signal__board_propose"], str(argv))
    mode = flag(argv, "--permission-mode")
    check("c) no bypass permission mode, though the director has one",
          so.get_session("dir-1")["permission_mode"] == "bypassPermissions" and argv.count("--permission-mode") == 1
          and mode not in ("bypassPermissions", None) and "--dangerously-skip-permissions" not in argv
          and "--allow-dangerously-skip-permissions" not in argv, str(argv))
    check("c) TEAM_MODEL, effort medium, a fixed spend limit",
          flag(argv, "--model") == team.TEAM_MODEL and flag(argv, "--effort") == "medium"
          and flag(argv, "--max-budget-usd") == planner.MAX_BUDGET_USD, str(argv))
    scout = so.get_session(team.get_subtask(S["b"])["session_id"])
    try:
        so.terminal_launch_argv(scout, "claude")
        refused = False
    except ValueError:
        refused = True
    check("c) the scout session cannot be opened as an interactive terminal", refused)

    sub = team.get_subtask(S["sub"])
    n = call_with(f"[SUB-AGENT TASK {S['sub']}")
    want = [so.CLAUDE_BIN, "-p", "--session-id", sub["session_id"], "--output-format", "json",
            "--permission-mode", "bypassPermissions", "--model", team.TEAM_MODEL, "--effort", "medium"]
    check("c) a normal sub-agent's command line is unchanged", n and n["argv"] == want,
          f"{n and n['argv']} != {want}")


async def case_d():
    c = call_with(f"[SCOUT {S['b']}]")
    p = c["prompt"] if c else ""
    want = {"today": datetime.now().strftime("%Y-%m-%d"), "brief": "who live in git",
            "open card": "Ship dark mode", "seen url": "https://seen.example/old",
            "past decision": "rejected: Old idea", "its reason": "too heavy for a CLI",
            "playbook": "never instructions",
            "the exact call": f'board_propose(from_role="sub-{S["b"][3:]}-scout", subtask_id="{S["b"]}", '
                              f'key="{S["b_key"]}"'}
    missing = [k for k, v in want.items() if v not in p]
    check("d) the first message has the date, brief, open cards, seen list, decisions and the key",
          S["b_key"] and not missing, f"missing {missing}")


async def case_e():
    team._exec("UPDATE subtasks SET status = 'passed' WHERE director_id = 'dir-1' AND kind = 'scout' "
               "AND status IN ('queued', 'running')")
    r = planner.start_scout("dir-1")
    st = team.get_subtask(r["id"])
    S["e"], name, key = r["id"], so.get_session(st["session_id"])["name"], st["run_key"]
    S["e_sig"] = last_sig()

    async def prop(url, k=key, title="An idea"):
        return await signal_mcp.board_propose(from_role=name, subtask_id=S["e"], key=k, title=title,
                                              body="What it is: z", url=url)
    wrong = await prop("https://new.example/a", k="nope")
    nonhttp = await prop("ftp://new.example/a")
    nourl = await prop("")
    seen = await prop("https://SEEN.example/old/")
    imp = await signal_mcp.board_propose(from_role="lead", subtask_id=S["e"], key=key, title="x", body="",
                                         url="https://new.example/z")
    check("e) a wrong key, a non-http url, no url, a seen url and another caller are each refused",
          all(x.startswith("⛔") for x in (wrong, nonhttp, nourl, seen, imp)) and "seen" in seen,
          str([wrong, nonhttp, nourl, seen, imp]))
    ok1 = await prop("https://new.example/one", title="Idea one")
    ok2 = await prop("https://new.example/two", title="Idea two")
    SCOUT.update(do=[("Idea three", "https://new.example/three"), ("Idea four", "https://new.example/four")],
                 results=[])
    await drain()
    third, fourth = (SCOUT["results"] + ["", ""])[:2]
    cards = [c for c in board.list_cards(WS, "dir-1") if c["title"].startswith("Idea ")]
    check("e) the right key files 'proposed' planner cards on the DIRECTOR's board, the 4th is refused",
          ok1.startswith("Proposal") and ok2.startswith("Proposal") and third.startswith("Proposal")
          and fourth.startswith("⛔") and len(cards) == 3
          and all(c["status"] == "proposed" and c["source"] == "planner" and c["owner_id"] == "dir-1"
                  and c["url"].startswith("https://new.example/") for c in cards)
          and not board.list_cards(WS, st["session_id"]), f"{ok1} {ok2} {third} {fourth} {cards}")
    after = await prop("https://new.example/five")
    check("e) the right key is refused once the run has finished",
          team.get_subtask(S["e"])["status"] == "passed" and after.startswith("⛔"), after)


async def case_f():
    st = team.get_subtask(S["e"])
    name = so.get_session(st["session_id"])["name"]
    tools = await signal_mcp.mcp.list_tools()
    names = [t.name for t in tools]
    leaked = []
    for t in tools:
        if t.name == planner.TOOL:
            continue
        args = {}
        for p, spec in (t.inputSchema.get("properties") or {}).items():
            args[p] = {"integer": 1, "boolean": False, "number": 1}.get(spec.get("type"), "x")
        args["from_role"] = name
        out = await signal_mcp.mcp.call_tool(t.name, args)
        text = " ".join(getattr(b, "text", "") for b in (out[0] if isinstance(out, tuple) else out))
        if not text.startswith("⛔ a planner run"):
            leaked.append(f"{t.name}: {text[:80]}")
    check(f"f) every other registered signal tool ({len(names) - 1}) refuses the planner session",
          len(names) > 10 and planner.TOOL in names and "send_signal" in names and not leaked, str(leaked))
    out = await signal_mcp.mcp.call_tool("board_list", {"from_role": "lead"})
    text = " ".join(getattr(b, "text", "") for b in (out[0] if isinstance(out, tuple) else out))
    check("f) …while the director still uses them", "Idea one" in text, text[:200])

    # Over HTTP, the run's own MCP config marks each request: a spoofed from_role does not help.
    app = signal_mcp.mcp.streamable_http_app()
    hdr = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "board_list", "arguments": {"from_role": "lead"}}}
    async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        r1 = await c.post("/mcp", json=body, headers={**hdr, "x-orch-planner": "1"}, follow_redirects=True)
        r2 = await c.post("/mcp", json=body, headers=hdr, follow_redirects=True)
    check("f) a request marked X-Orch-Planner is refused even as the director; unmarked it works",
          "a planner run can only call" in r1.text and "Idea one" in r2.text, f"{r1.text[:200]} | {r2.text[:200]}")


async def case_g():
    v = planner.view(WS, "dir-1")
    last = v.get("last") or {}
    check("g) a run that ended well is passed, with the proposals it filed and its tokens",
          team.get_subtask(S["e"])["status"] == "passed" and last.get("id") == S["e"]
          and last.get("proposals") == 3 and last.get("tokens") == 321 and not v["running"], str(v))
    zero = team.get_subtask(S["b"])
    check("g) a run that filed nothing is passed too", zero["status"] == "passed", str(zero["status"]))
    SCOUT.update(crash=True, do=[])
    r = planner.start_scout("dir-1")
    await drain()
    SCOUT["crash"] = False
    st = team.get_subtask(r["id"])
    check("g) a run that failed is failed, with the reason",
          st["status"] == "failed" and "CLI died" in st["reason"]
          and planner.view(WS, "dir-1")["last"]["reason"] == st["reason"], str(st["status"]) + st["reason"])
    check("g) no signal was enqueued for the director by any of these runs", to_dir1_since(S["e_sig"]) == 0,
          str(team._rows("SELECT id, message FROM signals WHERE to_session = 'dir-1' AND id > ?", (S["e_sig"],))))


async def case_h():
    past = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    future = (datetime.now() + timedelta(days=2)).isoformat(timespec="seconds")
    for owner in ("dir-2", "dir-3", "dir-4"):
        planner.save(WS, owner, enabled=True, brief=f"brief of {owner}", every_days=3)
    team._exec("UPDATE planners SET next_run_at = ? WHERE owner_id IN ('dir-1', 'dir-4')", (past,))
    team._exec("UPDATE planners SET next_run_at = ? WHERE owner_id = 'dir-3'", (future,))
    planner.save(WS, "dir-2", enabled=False)
    team._exec("UPDATE planners SET next_run_at = ? WHERE owner_id = 'dir-2'", (past,))
    team.on_session_removed("dir-4")
    so.unregister_session("dir-4")
    started = planner.run_due()
    who = [team.get_subtask(x)["director_id"] for x in started]
    nxt = datetime.fromisoformat(planner.settings("dir-1")["next_run_at"])
    check("h) only the due, enabled planner with a living agent is started",
          who == ["dir-1"], f"{who}")
    check("h) its next_run_at moved forward by every_days",
          abs((nxt - (datetime.now() + timedelta(days=5))).total_seconds()) < 60, str(nxt))
    check("h) calling it again right away starts nothing", planner.run_due() == [])
    for x in started:
        team.cancel(x)
    team._exec("UPDATE planners SET next_run_at = ? WHERE owner_id = 'dir-1'", (past,))
    team._exec("INSERT INTO subtasks (id, workspace_id, director_id, kind, playbook, status, created_at, "
               "updated_at) VALUES ('st_stuck', ?, 'dir-1', 'scout', 'scout', 'running', ?, ?)",
               (WS, so._now(), so._now()))
    check("h) a refused start is not retried on the next tick",
          planner.run_due() == [] and planner.settings("dir-1")["next_run_at"] > so._now()
          and planner.run_due() == [], planner.settings("dir-1"))
    team._exec("DELETE FROM subtasks WHERE id = 'st_stuck'")
    await drain()


async def case_i():
    c = board.propose(WS, "dir-2", "Shared piece", "body", "https://shared.example")
    check("i) board.propose makes a proposed planner card with its url",
          (c.get("status"), c.get("source"), c.get("url")) == ("proposed", "planner", "https://shared.example"), str(c))
    check("i) board.propose refuses a non-http url", "error" in board.propose(WS, "dir-2", "x", "", "file:///etc"))
    old = TMP / "old.db"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE board_cards (id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL "
                 "DEFAULT 'default', owner_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', "
                 "status TEXT NOT NULL DEFAULT 'todo', source TEXT NOT NULL DEFAULT 'user', subtask_id TEXT NOT "
                 "NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO board_cards (owner_id, title, created_at, updated_at) VALUES ('a', 'kept', '', '')")
    board.migrate(conn)
    board.migrate(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(board_cards)")}
    row = conn.execute("SELECT title, url, decision, decision_reason FROM board_cards").fetchone()
    conn.close()
    check("i) an old board_cards table gains url, decision and decision_reason (twice is fine)",
          {"url", "decision", "decision_reason"} <= cols and row == ("kept", "", "", ""), f"{cols} {row}")


async def main():
    for case in (case_a, case_b, case_c, case_d, case_e, case_f, case_g, case_h, case_i):
        await case()


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:\n  " + "\n  ".join(fails))
    sys.exit(1)
print("\nall planner checks passed")
