#!/usr/bin/env python3
"""Guards for a workflow step's tool limit: a role, its sub-agents and their verifiers run with only
the tools the step lists plus fixed basics (workflow.tool_list).
What can go wrong quietly:
  - a role's first `claude -p` or a later run without the limit, so it can use any tool;
  - a role's sub-agent or verifier (also one started after the step ended) not inheriting it;
  - a worktree session losing the director folder's MCP servers, or getting them through argv, a
    file others can read, or a file inside the repo;
  - an ordinary session, an ordinary director's sub-agent or verifier with a changed command line.

Real state machine against a throwaway database, a real git repo and a fake claude CLI (the
subprocess call is replaced) that records argv. No real claude, no real ~/.claude.json.

    python3 check_workflow_tools.py
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TMP = Path(tempfile.mkdtemp(prefix="check_workflow_tools_"))
BIN = TMP / "bin"
BIN.mkdir()
EXE = ".exe" if os.name == "nt" else ""
for name in ("claude", "agy"):
    f = BIN / (name + EXE)
    f.write_bytes(b"")
    f.chmod(0o755)
CONFIG = TMP / "claude.json"
TOKEN = "tok-" + uuid.uuid4().hex
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ.update({
    "HOME": str(TMP / "home"), "USERPROFILE": str(TMP / "home"), "ORCH_CLAUDE_CONFIG": str(CONFIG),
    "ORCH_DB": "check_workflow_tools", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"), "ORCH_DRY_RUN": "0",
    "CLAUDE_BIN": str(BIN / ("claude" + EXE)), "ORCH_AGY_BIN": str(BIN / ("agy" + EXE)),
    "ORCH_STREAM": "0", "ORCH_SUBTASK_MAX_ATTEMPTS": "1",
})
sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_orchestrator as so   # noqa: E402
import signal_mcp                   # noqa: E402
import team                         # noqa: E402
import workflow                     # noqa: E402

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


so.DB_DIR = TMP / "db"
assert Path.home() / ".session_orch_db" not in [so.DB_DIR, *so.DB_DIR.parents], so.DB_DIR
assert so.CLAUDE_CONFIG_FILE == CONFIG, so.CLAUDE_CONFIG_FILE
so.CLAUDE_PROJECTS_DIR = TMP / "claude-projects"
so.AGY_CONVERSATIONS_DIR = TMP / "agy-conversations"
so.STREAM = False
so.DRY_RUN = False
so.init_db()
team._worktrees_root = lambda: TMP / "worktrees"
signal_mcp._INPROC = True


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


def make_repo(name):
    repo = TMP / name
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "dev@example.com", cwd=repo)
    git("config", "user.name", "Dev", cwd=repo)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


REPO, PLAIN = make_repo("repo"), make_repo("plain")     # PLAIN: no per-folder MCP server
CONFIG.write_text(json.dumps({"projects": {str(REPO): {"mcpServers": {"demo": {
    "type": "http", "url": "http://127.0.0.1:1/mcp", "headers": {"Authorization": "Bearer " + TOKEN}}}}}}),
    encoding="utf-8")
WS = so.DEFAULT_WORKSPACE
so.register_session("dir-1", "lead", cwd=str(REPO), model="", permission_mode="bypassPermissions",
                    workspace_id=WS)
so.register_session("dir-2", "plainlead", cwd=str(PLAIN), model="", permission_mode="bypassPermissions",
                    workspace_id=WS)

# ── The fake claude CLI ───────────────────────────────────────────────────────
CALLS = []                      # {argv, cwd, prompt, sid}
DELEGATE = set()                # roles whose first turn spawns one feature sub-agent


class FakeProc:
    def __init__(self, argv, cwd):
        self.argv, self.cwd, self.returncode, self.pid = list(argv), cwd, 0, 0

    async def communicate(self, input=b""):
        prompt, a = (input or b"").decode("utf-8"), self.argv
        sid = a[a.index("--session-id") + 1] if "--session-id" in a else \
            a[a.index("--resume") + 1] if "--resume" in a else str(uuid.uuid4())
        CALLS.append({"argv": a, "cwd": str(self.cwd), "prompt": prompt, "sid": sid})
        if "[WORKFLOW STEP " in prompt:
            role = prompt[prompt.index("[WORKFLOW STEP "):].split("]")[0].split(" · ")[-1]
            if role in DELEGATE:
                res = team.spawn_subagent(sid, "feature", "add feature.py", "feature.py exists")
                assert res.get("id"), res
            else:
                workflow.finish_step(sid, "done", f"{role} report")
        elif "[SUB-AGENT PASSED]" in prompt or "[SUB-AGENT FAILED]" in prompt:
            workflow.finish_step(sid, "done", "merged")
        elif "[SUB-AGENT TASK " in prompt:
            cwd = Path(self.cwd)
            assert TMP.resolve() in cwd.resolve().parents, cwd
            (cwd / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
            git("add", "feature.py", cwd=cwd)
            git("commit", "-q", "-m", "feature", cwd=cwd)
        elif "[VERIFY " in prompt:
            st = prompt.split("[VERIFY ")[1].split("]")[0]   # the SKILL text comes first
            key = prompt.split('key="')[1].split('"')[0]
            name = (so.get_session(sid) or {}).get("name", "")
            team.submit_verdict(name, st, "PASS", "ran it", key=key)
        return json.dumps({"result": "ok", "session_id": sid, "usage": {"output_tokens": 1}}).encode(), b""

    async def wait(self):
        return self.returncode


async def fake_exec(*argv, cwd=None, **kw):
    if cwd and TMP.resolve() not in Path(cwd).resolve().parents:
        raise RuntimeError(f"refusing to run outside {TMP}: {cwd}")
    return FakeProc(argv, cwd)


so.asyncio.create_subprocess_exec = fake_exec


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


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def calls_of(sid):
    return [c for c in CALLS if c["sid"] == sid]


def words(v):
    """--allowedTools / --tools value → list; Bash(git *) holds a space."""
    return (v or "").replace("Bash(git *)", "Bash(git_*)").split()


ROLE_BASE = ["Read", "Grep", "Glob", "Bash(git_*)", "mcp__signal__spawn_subagent", "mcp__signal__list_subagents",
             "mcp__signal__cancel_subagent", "mcp__signal__finish_step"]


def limited(label, calls, allowed, builtins, mcp):
    """Every run: dontAsk, exactly that allow list and built-ins, --mcp-config only when mcp."""
    bad = [c["argv"] for c in calls
           if flag(c["argv"], "--permission-mode") != "dontAsk" or c["argv"].count("--permission-mode") != 1
           or sorted(words(flag(c["argv"], "--allowedTools"))) != sorted(allowed)
           or sorted(words(flag(c["argv"], "--tools"))) != sorted(builtins)
           or ("--mcp-config" in c["argv"]) != mcp or "--strict-mcp-config" in c["argv"]]
    check(f"{label}: first claude -p and every run limited ({len(calls)} calls)",
          len(calls) >= 2 and "--resume" not in calls[0]["argv"] and not bad, str(bad[:1] or calls))


def unlimited(label, calls):
    bad = [c["argv"] for c in calls if {"--tools", "--allowedTools", "--mcp-config"} & set(c["argv"])
           or flag(c["argv"], "--permission-mode") != "bypassPermissions"]
    check(f"{label}: command line as before ({len(calls)} calls)", len(calls) >= 2 and not bad, str(bad[:1]))


def subtask_of(role_sid):
    return team._row("SELECT * FROM subtasks WHERE director_id = ?", (role_sid,))


async def run_wf(director, name, tools, delegate):
    steps = [{"role": f"{name}-r", "job": "do it", "playbook": "be good", "tools": tools}]
    assert workflow.define(WS, director, name, "test", steps).get("status"), name
    if delegate:
        DELEGATE.add(f"{name}-r")
    res = workflow.start(WS, director, name, "the job")
    assert res.get("id"), res
    await drain()
    run = workflow.get_run(res["id"])
    check(f"{name}: run done", run["status"] == "done", str(run["status"]) + " " + run["reason"])
    return run


async def main():
    # Step lists an MCP tool of the director folder's server and the full shell; the role delegates.
    run = await run_wf("dir-1", "wa", ["mcp__demo__do_thing", "Bash"], True)
    role = run["session_id"]
    role_allowed = [t for t in ROLE_BASE if t != "Bash(git_*)"] + ["mcp__demo__do_thing", "Bash"]
    limited("a) role", calls_of(role), role_allowed, ["Read", "Grep", "Glob", "Bash"], False)
    st = subtask_of(role)
    limited("a) role's sub-agent", calls_of(st["session_id"]),
            ["Read", "Grep", "Glob", "Edit", "Write", "Bash", "mcp__demo__do_thing"],
            ["Read", "Grep", "Glob", "Edit", "Write", "Bash"], True)
    ver_allowed = ["Read", "Grep", "Glob", "Bash", "mcp__signal__submit_verdict", "mcp__demo__do_thing"]
    limited("a) its verifier", calls_of(st["verifier_id"]), ver_allowed, ["Read", "Grep", "Glob", "Bash"], True)
    paths = {flag(c["argv"], "--mcp-config") for c in calls_of(st["session_id"]) + calls_of(st["verifier_id"])}
    ok = []
    for p in paths:
        f = Path(p)
        ok.append(f.is_file() and so.DB_DIR.resolve() in f.resolve().parents
                  and REPO.resolve() not in f.resolve().parents
                  and Path(st["worktree"]).resolve() not in f.resolve().parents
                  and list(json.loads(f.read_text(encoding="utf-8"))["mcpServers"]) == ["demo"]
                  and (os.name == "nt" or (f.stat().st_mode & 0o777) == 0o600))
    check("a) MCP file per worktree session: under the DB dir, owner-only, lists the folder's server",
          len(paths) == 2 and all(ok), str(paths))
    check("a) the server's token is in no argv", not any(TOKEN in " ".join(c["argv"]) for c in CALLS))

    # "Verify again" after the run ended: same verifier list.
    res = await team.reverify(st["id"])
    await drain()
    st2 = team.get_subtask(st["id"])
    check("b) verify again after the run ended ran a new verifier",
          res.get("ok") and st2["verifier_id"] != st["verifier_id"] and st2["status"] == "passed",
          f"{res} {st2['status']}")
    limited("b) the later verifier", calls_of(st2["verifier_id"]), ver_allowed,
            ["Read", "Grep", "Glob", "Bash"], True)

    # tools: [] → exactly the role basics; Bash(git *), not the full shell.
    run = await run_wf("dir-1", "wb", [], False)
    limited("c) role with no tools", calls_of(run["session_id"]), ROLE_BASE, ["Read", "Grep", "Glob", "Bash"], False)

    # A folder with no per-folder MCP server: no --mcp-config even when an MCP tool is listed.
    run = await run_wf("dir-2", "wc", ["mcp__demo__do_thing"], True)
    st = subtask_of(run["session_id"])
    limited("d) sub-agent, folder without servers", calls_of(st["session_id"]),
            ["Read", "Grep", "Glob", "Edit", "Write", "Bash(git_*)", "mcp__demo__do_thing"],
            ["Read", "Grep", "Glob", "Edit", "Write", "Bash"], False)
    limited("d) verifier, folder without servers", calls_of(st["verifier_id"]),
            ["Read", "Grep", "Glob", "Bash(git_*)", "mcp__signal__submit_verdict", "mcp__demo__do_thing"],
            ["Read", "Grep", "Glob", "Bash"], False)

    # Ordinary director, its sub-agent and verifier: unchanged.
    DELEGATE.clear()
    res = team.spawn_subagent("dir-1", "feature", "add feature.py", "feature.py exists")
    await drain()
    st = team.get_subtask(res["id"])
    check("e) ordinary sub-agent passed", st["status"] == "passed" and not st["tools"], st["status"])
    unlimited("e) ordinary sub-agent", calls_of(st["session_id"]))
    unlimited("e) ordinary verifier", calls_of(st["verifier_id"]))
    unlimited("e) ordinary director (its [WORKFLOW DONE] / [SUB-AGENT PASSED] runs)",
              [c for c in CALLS if c["sid"] == "dir-1"])


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'ALL OK' if not fails else f'{len(fails)} FAIL'}")
sys.exit(1 if fails else 0)
