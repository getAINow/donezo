#!/usr/bin/env python3
"""Guards for a director's board: the backlog of cards an agent holds.

Runs the real MCP tools, the real team state machine and the real HTTP routes against a throwaway
database, a real git repo and a scripted fake CLI. What can go wrong quietly:
  - an agent reading or moving another agent's cards, or a sub-agent using a board at all;
  - a card that says "in progress" long after its sub-agent failed, or "done" before the work is
    merged;
  - one workspace's cards showing through another workspace's API;
  - cards vanishing with the agent that owned them.

    python3 check_board.py
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TMP = Path(tempfile.mkdtemp(prefix="check_board_"))
BIN = TMP / "bin"
BIN.mkdir()
EXE = ".exe" if os.name == "nt" else ""
for name in ("claude", "agy"):
    f = BIN / (name + EXE)
    f.write_bytes(b"")
    f.chmod(0o755)
os.environ.update({
    "ORCH_DB": "check_board", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"), "ORCH_DRY_RUN": "1",
    "CLAUDE_BIN": str(BIN / ("claude" + EXE)), "ORCH_AGY_BIN": str(BIN / ("agy" + EXE)),
    "ORCH_SUBTASK_MAX_ATTEMPTS": "1",
})
sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_orchestrator as so   # noqa: E402
import signal_mcp                   # noqa: E402
import team                         # noqa: E402
import board                        # noqa: E402

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


so.DB_DIR = TMP / "db"
assert Path.home() / ".session_orch_db" not in [so.DB_DIR, *so.DB_DIR.parents], so.DB_DIR
db = so.DB_DIR / "check_board.db"
so.init_db()
team._worktrees_root = lambda: TMP / "worktrees"
signal_mcp._INPROC = True


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


REPO = TMP / "repo"
REPO.mkdir()
git("init", "-q", "-b", "main", cwd=REPO)
git("config", "user.email", "dev@example.com", cwd=REPO)
git("config", "user.name", "Dev", cwd=REPO)
(REPO / "app.py").write_text("x = 1\n", encoding="utf-8")
git("add", "-A", cwd=REPO)
git("commit", "-q", "-m", "init", cwd=REPO)

WS = so.DEFAULT_WORKSPACE
WS_B = so.create_workspace("other")["id"]
so.register_session("dir-1", "lead", cwd=str(REPO), model="", workspace_id=WS)
so.register_session("dir-2", "peer", cwd=str(REPO), model="", workspace_id=WS)
so.register_session("sub-x", "sub-x-feature", cwd=str(REPO), model="", workspace_id=WS)
team._set_team("sub-x", "dir-1", "subagent")

BUILD = {}       # subtask id → "commit" | "read" | "crash"


async def fake_run(session, prompt, on_event=None, dry_run=False):
    cwd = Path(session.get("cwd") or ".")
    if TMP.resolve() not in cwd.resolve().parents:
        return {"ok": False, "result": f"refusing to write outside {TMP}", "session_id": session["id"],
                "tokens": 0, "raw": {}}
    if "[SUB-AGENT TASK " in prompt:
        st = prompt[prompt.index("[SUB-AGENT TASK ") + 16:].split()[0].rstrip("]")
        act = BUILD.get(st, "commit")
        if act == "crash":
            return {"ok": False, "result": "CLI died", "session_id": session["id"], "tokens": 0, "raw": {}}
        if act == "commit":
            (cwd / "feature.py").write_text("V = 1\n", encoding="utf-8")
            git("add", "feature.py", cwd=cwd)
            git("commit", "-q", "-m", "feature", cwd=cwd)
        return {"ok": True, "result": "done", "session_id": session["id"], "tokens": 1, "raw": {}}
    if "[VERIFY " in prompt:
        st = prompt[prompt.index("[VERIFY ") + 8:].split()[0].rstrip("]")
        i = prompt.find('key="')
        team.submit_verdict(session["name"], st, "PASS", "ran it → ok", key=prompt[i + 5:prompt.index('"', i + 5)])
        return {"ok": True, "result": "verified", "session_id": session["id"], "tokens": 1, "raw": {}}
    return {"ok": True, "result": "noted", "session_id": session["id"], "tokens": 1, "raw": {}}


for eng in so.ENGINES.values():
    eng.run = fake_run


async def drain(rounds=200):
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


def card(cid):
    return team._row("SELECT * FROM board_cards WHERE id = ?", (cid,))


def cid_of(res):
    return int(res.split("Card ")[1].split()[0])


async def add(role, title, body=""):
    return cid_of(await signal_mcp.board_add(from_role=role, title=title, body=body))


async def case_a():
    res = await signal_mcp.board_add(from_role="lead", title="Write the docs", body="guide page")
    c = card(cid_of(res))
    check("a) board_add makes a todo card for the caller, source agent",
          c and c["status"] == "todo" and c["owner_id"] == "dir-1" and c["source"] == "agent"
          and c["body"] == "guide page", f"{res} {c}")
    mine = await signal_mcp.board_list(from_role="lead")
    theirs = await signal_mcp.board_list(from_role="peer")
    check("a) board_list shows the caller's cards; another agent sees none",
          "Write the docs" in mine and theirs == "Your board is empty.", f"{mine[:120]} / {theirs}")
    refused = [await signal_mcp.board_add(from_role="sub-x-feature", title="sneak"),
               await signal_mcp.board_list(from_role="sub-x-feature"),
               await signal_mcp.board_update(from_role="sub-x-feature", card_id=c["id"], status="done")]
    check("a) a team session is refused by every board tool",
          all(r.startswith("⛔") for r in refused) and card(c["id"])["status"] == "todo", str(refused))
    empty = await signal_mcp.board_add(from_role="lead", title="  ")
    check("a) a card needs a title", empty.startswith("⛔"), empty)


async def case_b():
    cid = await add("lead", "Old title")
    res = await signal_mcp.board_update(from_role="lead", card_id=cid, status="in_progress",
                                        title="New title", body="details")
    c = card(cid)
    check("b) board_update changes status, title and body",
          (c["status"], c["title"], c["body"]) == ("in_progress", "New title", "details"), f"{res} {c}")
    other = await signal_mcp.board_update(from_role="peer", card_id=cid, status="dropped")
    check("b) another agent's card is refused", other.startswith("⛔") and card(cid)["status"] == "in_progress",
          other)
    bad = await signal_mcp.board_update(from_role="lead", card_id=cid, status="blocked")
    check("b) an invalid status is refused", bad.startswith("⛔") and card(cid)["status"] == "in_progress", bad)
    bad = await signal_mcp.board_list(from_role="lead", status="blocked")
    check("b) board_list refuses an invalid status filter", bad.startswith("⛔"), bad)


async def spawn(cid, playbook="feature", build="commit", role="lead"):
    res = await signal_mcp.spawn_subagent(from_role=role, playbook=playbook, brief="do it",
                                          done_when="it works", card_id=cid)
    if res.startswith("⛔"):
        return res
    st = res.split()[1]
    BUILD[st] = build
    return st


async def case_c():
    cid = await add("lead", "Feature X")
    st = await spawn(cid)
    c = card(cid)
    check("c) spawn_subagent(card_id=…) sets in_progress and links the task",
          c["status"] == "in_progress" and c["subtask_id"] == st and team.get_subtask(st), f"{st} {c}")
    team.cancel(st)
    await drain()
    theirs = await add("peer", "Peer's work")
    res = await spawn(theirs)
    check("c) someone else's card is refused", str(res).startswith("⛔") and card(theirs)["subtask_id"] == "",
          str(res))
    await signal_mcp.board_update(from_role="lead", card_id=cid, status="done")
    n = len(team._rows("SELECT id FROM subtasks"))
    res = await spawn(cid)
    check("c) a done card is refused, and no task is created",
          str(res).startswith("⛔") and len(team._rows("SELECT id FROM subtasks")) == n, str(res))
    st = await spawn(0)
    check("c) spawn without a card works as before", not str(st).startswith("⛔") and team.get_subtask(st),
          str(st))
    team.cancel(st)
    await drain()


async def case_d():
    cid = await add("lead", "Will fail")
    st = await spawn(cid, build="crash")
    await drain()
    c = card(cid)
    check("d) linked task failed → todo with a note saying why",
          team.get_subtask(st)["status"] == "failed" and c["status"] == "todo" and st in c["note"]
          and "failed" in c["note"], f"{team.get_subtask(st)['status']} {c}")

    cid = await add("lead", "Will be cancelled")
    st = await spawn(cid)
    team.cancel(st, by_user=True)
    await drain()
    c = card(cid)
    check("d) linked task cancelled → todo", c["status"] == "todo" and "cancelled" in c["note"], str(c))

    cid = await add("lead", "Look into Y")
    st = await spawn(cid, playbook="investigate", build="read")
    await drain()
    c = card(cid)
    check("d) investigate passed → done", team.get_subtask(st)["status"] == "passed" and c["status"] == "done",
          f"{team.get_subtask(st)['status']} {team.get_subtask(st)['reason']} {c}")

    cid = await add("lead", "Build Z")
    st = await spawn(cid)
    await drain()
    t, c = team.get_subtask(st), card(cid)
    check("d) code task passed → still in_progress, waiting for merge",
          t["status"] == "passed" and t["branch"] and c["status"] == "in_progress"
          and "waiting for merge" in c["note"], f"{t['status']} {t['reason']} {c}")
    git("merge", "-q", "--no-ff", "-m", f"merge {t['branch']}", t["branch"], cwd=REPO)
    cleaned = team.sweep_merged()
    c = card(cid)
    check("d) after a real merge and sweep_merged → done",
          st in cleaned and c["status"] == "done" and "merged" in c["note"], f"{cleaned} {c}")

    cid = await add("lead", "Dropped while running")
    st = await spawn(cid, build="crash")
    await signal_mcp.board_update(from_role="lead", card_id=cid, status="dropped")
    await drain()
    check("d) a card dropped by hand stays dropped when its task fails", card(cid)["status"] == "dropped",
          str(card(cid)))

    cid = await add("lead", "Discard me")
    st = await spawn(cid)
    await drain()
    team.discard(st)
    c = card(cid)
    check("d) a passed task discarded by the user → todo", c["status"] == "todo" and "discarded" in c["note"],
          str(c))


async def case_e():
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=so.build_app()),
                                 base_url="http://test") as c:
        r = await c.post("/api/board", json={"workspace_id": WS, "owner_id": "dir-2", "title": "From the UI"})
        made = r.json()
        check("e) POST /api/board creates a todo card from the user",
              r.status_code == 201 and made.get("source") == "user" and made.get("status") == "todo", r.text)
        r = await c.get("/api/board", params={"workspace_id": WS, "owner_id": "dir-2"})
        cards = r.json()["cards"]
        check("e) GET lists that owner's cards only, with open counts",
              {x["owner_id"] for x in cards} == {"dir-2"}
              and [x["title"] for x in cards] == ["From the UI", "Peer's work"]
              and r.json()["open"].get("dir-2") == len(cards), r.text[:300])
        r = await c.post(f"/api/board/{made['id']}", json={"workspace_id": WS, "status": "in_progress",
                                                           "title": "Renamed", "body": "more"})
        check("e) POST /api/board/<id> updates it",
              r.status_code == 200 and (r.json()["status"], r.json()["title"], r.json()["body"])
              == ("in_progress", "Renamed", "more"), r.text)
        r = await c.post(f"/api/board/{made['id']}", json={"workspace_id": WS, "status": "nope"})
        check("e) an invalid status is a 400", r.status_code == 400, r.text)
        r = await c.get("/api/board", params={"workspace_id": WS_B, "owner_id": "dir-2"})
        check("e) workspace B does not list workspace A's cards", r.json()["cards"] == [], r.text)
        r1 = await c.post(f"/api/board/{made['id']}", json={"workspace_id": WS_B, "status": "dropped"})
        r2 = await c.delete(f"/api/board/{made['id']}", params={"workspace_id": WS_B})
        check("e) workspace B cannot change or delete it",
              r1.status_code == 404 and r2.status_code == 404 and card(made["id"])["status"] == "in_progress",
              f"{r1.status_code} {r2.status_code}")
        r = await c.post("/api/board", json={"workspace_id": WS_B, "owner_id": "dir-2", "title": "x"})
        check("e) workspace B cannot add to an agent of workspace A", r.status_code == 400, r.text)
        r = await c.delete(f"/api/board/{made['id']}", params={"workspace_id": WS})
        check("e) DELETE /api/board/<id> removes it", r.status_code == 200 and card(made["id"]) is None, r.text)


async def case_f():
    n = len(team._rows("SELECT id FROM board_cards WHERE owner_id = 'dir-2'"))
    team.on_session_removed("dir-2")
    so.unregister_session("dir-2")
    left = len(team._rows("SELECT id FROM board_cards WHERE owner_id = 'dir-2'"))
    check("f) cards survive their owner being unregistered", n > 0 and left == n and not so.get_session("dir-2"),
          f"{n} → {left}")


async def main():
    for case in (case_a, case_b, case_c, case_d, case_e, case_f):
        await case()


try:
    asyncio.run(main())
finally:
    for st in team._rows("SELECT * FROM subtasks"):
        team.remove_worktree(st)
    shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:\n  " + "\n  ".join(fails))
    sys.exit(1)
print("\nall board checks passed")
