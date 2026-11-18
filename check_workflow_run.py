#!/usr/bin/env python3
"""Guards for running workflows (workflow.py runs + the MCP tools run_workflow, cancel_workflow,
finish_step), against a throwaway database, a real git repo and a scripted fake CLI.
What can go wrong quietly:
  - a run advancing mid-turn, skipping a step, or looping forever on rejections / failures;
  - a later role not seeing the earlier reports, or a sent-back role not seeing the findings;
  - the director told nothing, or told twice, or told after a cancel;
  - redefining a workflow changing a run already in flight;
  - a role's SKILL folder showing in the user's git status or left behind;
  - a run hanging 'running' after a restart, a removed director or a denied task message.

    python3 check_workflow_run.py
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

TMP = Path(tempfile.mkdtemp(prefix="check_workflow_run_"))
BIN = TMP / "bin"
BIN.mkdir()
for name in ("claude", "agy"):
    f = BIN / (name + (".exe" if os.name == "nt" else ""))
    f.write_bytes(b"")
    f.chmod(0o755)
os.environ.update({
    "ORCH_DB": "check_workflow_run", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"), "ORCH_DRY_RUN": "1",
    "CLAUDE_BIN": str(BIN / ("claude" + (".exe" if os.name == "nt" else ""))),
    "ORCH_AGY_BIN": str(BIN / ("agy" + (".exe" if os.name == "nt" else ""))),
})
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx                        # noqa: E402
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
so.register_session("dir-1", "lead", cwd=str(REPO), model="", workspace_id=WS)
so.register_session("peer-1", "peer", cwd=str(REPO), model="", workspace_id=WS)

# ── Scripted fake CLI ────────────────────────────────────────────────────────
# PLAN[role] = one entry per run of that role: "crash", or a list of finish_step calls
# (result, back_to, report) — an empty list ends the turn without finish_step.
# A callable entry runs first (inside the turn) and its return value is the calls list.
PLAN, ROLE_RUNS, DURING, RESPONSES = {}, [], {}, []


def _role_in(prompt):
    i = prompt.find("[WORKFLOW STEP ")
    return prompt[i:].split("]")[0].split(" · ")[-1] if i >= 0 else ""


async def fake_run(session, prompt, on_event=None, dry_run=False):
    role = _role_in(prompt)
    if not role:
        return {"ok": True, "result": "noted", "session_id": session["id"], "tokens": 1, "raw": {}}
    name, cwd = session["name"], Path(session.get("cwd") or ".")
    ROLE_RUNS.append((role, name, prompt))
    DURING[name] = ((cwd / ".claude" / "skills" / name / "SKILL.md").exists(),
                    git("status", "--porcelain", "--untracked-files=all", cwd=cwd))
    plan = PLAN.get(role, [[("done", "", f"{role} report")]])
    act = plan.pop(0) if len(plan) > 1 else plan[0]
    if callable(act):
        act = await act(session)
    if act == "crash":
        return {"ok": False, "result": "CLI died", "session_id": session["id"], "tokens": 0, "raw": {}}
    for result, back_to, report in act:
        r = await signal_mcp.finish_step(from_role=name, result=result, report=report, back_to=back_to,
                                         workspace_id=WS)
        run = workflow.get_run(_run_id(prompt))
        RESPONSES.append((role, result, back_to, r, run["status"], run["step"], run["session_id"] == session["id"]))
    return {"ok": True, "result": f"{role} finished", "session_id": session["id"], "tokens": 5, "raw": {}}


def _run_id(prompt):
    return prompt.split("[WORKFLOW STEP ")[1].split()[0]


for eng in so.ENGINES.values():
    eng.run = fake_run


async def drain(rounds=300, signals=True):
    for _ in range(rounds):
        sigs = so.eligible_signals() if signals else []
        if sigs:
            for s in sigs:
                await so.process_signal(s)
            continue
        if team._bg:
            await asyncio.gather(*list(team._bg), return_exceptions=True)
            continue
        return


def to_director(did="dir-1"):
    return [r["message"] for r in team._rows("SELECT message FROM signals WHERE to_session = ? ORDER BY id",
                                             (did,))]


def wf_msgs(did="dir-1"):
    return [m for m in to_director(did) if m.startswith("[WORKFLOW ")]


def step(role):
    return {"role": role, "job": f"{role} job", "playbook": f"You are {role}. Do it well.", "tools": []}


async def define(name, roles, who="lead"):
    r = await signal_mcp.define_workflow(from_role=who, name=name, purpose=f"{name} purpose",
                                         steps=[step(x) for x in roles], workspace_id=WS)
    assert "created" in r or "replaced" in r, r


async def run(name, job, from_step=1, who="lead"):
    return await signal_mcp.run_workflow(from_role=who, name=name, job=job, from_step=from_step, workspace_id=WS)


def latest(did="dir-1"):
    return workflow._load_run(team._row("SELECT * FROM workflow_runs WHERE director_id = ? "
                                        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (did,)))


def skill_dirs(sess):
    """The role's SKILL folders that exist, in every CLI root."""
    return [d for root in so.CLI_SKILL_ROOTS
            if (d := Path(sess["cwd"]) / root / "skills" / sess["name"]).exists()]


def runs_of(prefix):
    return [(role, prompt) for role, _, prompt in ROLE_RUNS if role.startswith(prefix)]


async def main():
    # ── a) happy path ────────────────────────────────────────────────────────
    await define("happy", ["a1", "a2", "a3"])
    seen_list = []

    async def a2_turn(session):
        seen_list.append(await signal_mcp.list_workflows(from_role="lead", workspace_id=WS))
        return [("done", "", "a2 report: built the thing")]
    PLAN["a2"] = [a2_turn]
    n_msgs = len(wf_msgs())
    r = await run("happy", "make the happy thing")
    check("a) run_workflow returns at once with a run id", "started at step 1/3" in r, r)
    await drain()
    wf = latest()
    check("a) run ends done", wf["status"] == "done", wf["status"])
    rr = runs_of("a")
    check("a) three roles ran in order", [x[0] for x in rr] == ["a1", "a2", "a3"], [x[0] for x in rr])
    sess = [so.get_session_by_name(n, WS) for role, n, _p in ROLE_RUNS if role.startswith("a")]
    check("a) three fresh role sessions in the director's folder, team role 'role', parent the director",
          len({s["id"] for s in sess}) == 3 and all(
              s["cwd"] == str(REPO) and s["team_role"] == "role" and s["parent_id"] == "dir-1" for s in sess),
          [(s["cwd"], s["team_role"], s["parent_id"]) for s in sess])
    check("a) role sessions run high effort on the builder model",
          all(s["effort"] == "high" and s["model"] == team.builder_model("", "high") for s in sess),
          [(s["effort"], s["model"]) for s in sess])
    p1, p2, p3 = (p for _, p in rr)
    check("a) step 1 has the run's job, its own job and the finish_step instruction",
          "make the happy thing" in p1 and "a1 job" in p1 and "happy purpose" in p1 and "finish_step(" in p1, p1)
    check("a) later roles see the earlier reports",
          "a1 report" in p2 and "a1 report" in p3 and "a2 report: built the thing" in p3, p3)
    check("a) list_workflows shows the running step", "step 2/3 (a2)" in (seen_list or [""])[0], seen_list)
    msgs = wf_msgs()[n_msgs:]
    check("a) exactly one [WORKFLOW DONE] message to the director, with each role and report",
          len(msgs) == 1 and msgs[0].startswith("[WORKFLOW DONE]") and all(x in msgs[0] for x in (
              "a1", "a2", "a3", "a1 report", "a2 report: built the thing", "a3 report", "Do not reply to this.")),
          msgs)
    check("a) list_workflows has no running line after the run",
          "running" not in await signal_mcp.list_workflows(from_role="lead", workspace_id=WS))
    names = [n for role, n, _ in ROLE_RUNS if role.startswith("a")]
    check("a) during a step the SKILL exists but git status is clean",
          all(DURING[n] == (True, "") for n in names), {n: DURING[n] for n in names})
    left = [p.name for p in (REPO / ".claude" / "skills").glob("sub-*")] if (REPO / ".claude" / "skills").exists() else []
    check("a) the role SKILL folders are gone after their steps", left == [], left)
    check("a) a recorded done does not advance the run mid-turn",
          all(st == "running" and same for role, *_, st, _s, same in RESPONSES if role.startswith("a")),
          RESPONSES)
    check("a) a role session cannot spawn sub-agents",
          "error" in team.spawn_subagent(sess[0]["id"], "feature", "b", "d"))

    # ── b) reject → back to step 1 → second rejection stops ─────────────────
    await define("rej", ["r1", "r2", "r3"])
    PLAN["r3"] = [[("reject", "r1", "FIND-1: r1 used the wrong colour")],
                  [("reject", "r1", "FIND-2: still wrong")]]
    n_msgs = len(wf_msgs())
    await run("rej", "reject job")
    await drain()
    wf = latest()
    order = [x[0] for x in runs_of("r")]
    check("b) r1 r2 r3 run again after the rejection, then stop", order == ["r1", "r2", "r3"] * 2, order)
    check("b) step 1's second message carries the findings",
          "FIND-1" in runs_of("r1")[1][1] and "FIND-1" not in runs_of("r1")[0][1], runs_of("r1")[1][1])
    check("b) the run is stopped", wf["status"] == "stopped" and "FIND-2" in wf["reason"], wf["reason"])
    msgs = wf_msgs()[n_msgs:]
    check("b) exactly one [WORKFLOW STOPPED] with the findings and the step to resume from",
          len(msgs) == 1 and msgs[0].startswith("[WORKFLOW STOPPED]") and "FIND-2" in msgs[0]
          and "from_step=1" in msgs[0] and "Do not reply to this." in msgs[0], msgs)

    await define("rej2", ["q1", "q2"])
    PLAN["q2"] = [[("reject", "", "FIND-Q: q1 is missing a part")], [("done", "", "q2 report")]]
    PLAN["q1"] = [[("done", "", "q1 report")], [], [("done", "", "q1 fixed")]]
    await run("rej2", "rej2 job")
    await drain()
    q1 = runs_of("q1")
    check("b) a sent-back step that then fails on its own keeps the findings in its retry",
          len(q1) == 3 and "FIND-Q" in q1[2][1] and "without calling finish_step" in q1[2][1]
          and latest()["status"] == "done", (len(q1), q1[-1][1] if q1 else ""))

    # ── c) resume from step 2 of the stopped run ────────────────────────────
    PLAN["r3"] = [[("done", "", "r3 report")]]
    r = await run("rej", "", from_step=2)
    check("c) resume starts at step 2", "started at step 2/3" in r, r)
    await drain()
    wf = latest()
    resumed = runs_of("r")[6:]
    check("c) resume runs r2, r3 only and ends done",
          [x[0] for x in resumed] == ["r2", "r3"] and wf["status"] == "done", ([x[0] for x in resumed], wf["status"]))
    check("c) step 2 sees step 1's earlier report and the earlier job",
          "r1 report" in resumed[0][1] and "reject job" in resumed[0][1], resumed[0][1])
    await define("fresh", ["z1", "z2"])
    r = await run("fresh", "", from_step=2)
    check("c) resume with no earlier run is refused", r.startswith("⛔"), r)

    # ── d) own failure: retried once, second failure stops ──────────────────
    await define("own", ["f1", "f2"])
    PLAN["f1"] = [[], [("done", "", "f1 report")]]
    await run("own", "own job")
    await drain()
    f1 = runs_of("f1")
    check("d) a turn without finish_step runs the step once more with the reason",
          len(f1) == 2 and "without calling finish_step" in f1[1][1] and latest()["status"] == "done",
          (len(f1), latest()["status"]))
    check("d) the retry is a fresh session", len({n for role, n, _ in ROLE_RUNS if role == "f1"}) == 2)
    PLAN["f1"] = [[]]
    n_msgs = len(wf_msgs())
    await run("own", "own job 2")
    await drain()
    wf = latest()
    check("d) two turns without finish_step stop the run",
          len(runs_of("f1")) == 4 and wf["status"] == "stopped" and "without calling finish_step" in wf["reason"],
          wf["reason"])
    msgs = wf_msgs()[n_msgs:]
    check("d) one [WORKFLOW STOPPED] naming the step to resume from",
          len(msgs) == 1 and msgs[0].startswith("[WORKFLOW STOPPED]") and "from_step=1" in msgs[0], msgs)
    PLAN["f1"] = ["crash", [("done", "", "f1 report")]]
    await run("own", "own job 3")
    await drain()
    check("d) a failed run is retried once", latest()["status"] == "done" and "run failed" in runs_of("f1")[-1][1])
    PLAN["f1"] = ["crash"]
    await run("own", "own job 4")
    await drain()
    wf = latest()
    check("d) two failed runs stop the run", wf["status"] == "stopped" and "run failed" in wf["reason"], wf["reason"])

    # ── e) finish_step refusals ─────────────────────────────────────────────
    await define("refuse", ["e1", "e2", "e3"])
    PLAN["e1"] = [[("reject", "", "x"), ("done", "", "e1 report")]]
    PLAN["e2"] = [[("reject", "nope", "x"), ("reject", "e3", "x"), ("reject", "e2", "x"), ("done", "", "e2 report")]]
    RESPONSES.clear()
    await run("refuse", "refusal job")
    await drain()
    got = {(role, res, back): r for role, res, back, r, *_ in RESPONSES}
    check("e) reject on step 1 is refused", got[("e1", "reject", "")].startswith("⛔"), got)
    check("e) unknown back_to is refused", got[("e2", "reject", "nope")].startswith("⛔"), got)
    check("e) back_to naming a later role is refused", got[("e2", "reject", "e3")].startswith("⛔"), got)
    check("e) back_to naming itself is refused", got[("e2", "reject", "e2")].startswith("⛔"), got)
    check("e) refused calls changed nothing: the run is done",
          latest()["status"] == "done" and [x[0] for x in runs_of("e")] == ["e1", "e2", "e3"])
    r = await signal_mcp.finish_step(from_role="peer", result="done", report="sneak", workspace_id=WS)
    check("e) a session that is not the current role is refused", r.startswith("⛔"), r)

    # ── f) one running run; refusals of run_workflow; cancel ────────────────
    await define("slow", ["s1", "s2"])
    n_msgs = len(to_director())
    r = await run("slow", "slow job")
    await drain(signals=False)                 # the role exists, its task message is pending
    wf = latest()
    check("f) the first role's task message is pending", wf["status"] == "running" and wf["signal_id"])
    r2 = await run("happy", "another")
    check("f) a second run is refused while one runs", r2.startswith("⛔"), r2)
    r2 = await run("ghost", "x")
    check("f) an unknown workflow is refused", r2.startswith("⛔"), r2)
    role_sess = so.get_session(wf["session_id"])
    r2 = await signal_mcp.run_workflow(from_role=role_sess["name"], name="slow", job="x", workspace_id=WS)
    check("f) a role session cannot run workflows", r2.startswith("⛔"), r2)
    r2 = await signal_mcp.cancel_workflow(from_role="lead", workspace_id=WS)
    await drain()
    wf = latest()
    sig = team._row("SELECT status FROM signals WHERE id = ?", (wf["signal_id"],))
    check("f) cancel_workflow cancels, denies the task message, tells the director nothing",
          wf["status"] == "cancelled" and sig["status"] == "denied" and len(to_director()) == n_msgs
          and not runs_of("s"), (r2, wf["status"], sig["status"]))
    check("f) the cancelled role's SKILL folder is gone",
          not (REPO / ".claude" / "skills" / role_sess["name"]).exists())
    r2 = await run("happy", "", from_step=1)
    check("f) an empty job is refused for a fresh run", r2.startswith("⛔"), r2)

    # ── g) redefining during a run does not change it ───────────────────────
    await define("redef", ["g1", "g2"])

    async def g1_turn(session):
        await define("redef", ["h1"])
        return [("done", "", "g1 report")]
    PLAN["g1"] = [g1_turn]
    await run("redef", "redef job")
    await drain()
    check("g) the run keeps its snapshot", [x[0] for x in runs_of("g")] == ["g1", "g2"]
          and not runs_of("h") and latest()["status"] == "done", [x[0] for x in runs_of("g")])

    # ── h) director / role removed, task message denied, restart ────────────
    REPO2 = TMP / "repo2"
    REPO2.mkdir()
    git("init", "-q", "-b", "main", cwd=REPO2)
    git("-c", "user.email=d@example.com", "-c", "user.name=D", "commit", "-q", "--allow-empty", "-m", "i", cwd=REPO2)
    so.register_session("dir-2", "lead2", cwd=str(REPO2), model="", workspace_id=WS)
    await define("slow", ["s1", "s2"], who="lead2")
    await run("slow", "job", who="lead2")
    await drain(signals=False)
    wf = latest("dir-2")
    role_sess = so.get_session(wf["session_id"])
    had = bool(skill_dirs(role_sess))
    r = await api.post("/api/sessions/dir-2/unregister")
    sig = team._row("SELECT status FROM signals WHERE id = ?", (wf["signal_id"],))
    check("h) removing the director (POST unregister) cancels its run",
          r.status_code == 200 and latest("dir-2")["status"] == "cancelled" and sig["status"] == "denied",
          (r.status_code, latest("dir-2")["status"], sig["status"]))
    check("h) …and its role's SKILL folder is gone", had and not skill_dirs(role_sess), (had, skill_dirs(role_sess)))

    n_msgs = len(wf_msgs())
    await run("slow", "job")
    await drain(signals=False)
    wf = latest()
    role_sess = so.get_session(wf["session_id"])
    had = bool(skill_dirs(role_sess))
    r = await api.post(f"/api/sessions/{role_sess['id']}/unregister")
    wf = latest()
    msgs = wf_msgs()[n_msgs:]
    check("h) removing the role's session (POST unregister) stops the run and tells the director once",
          r.status_code == 200 and wf["status"] == "stopped" and "removed" in wf["reason"] and len(msgs) == 1,
          (r.status_code, wf["reason"], msgs))
    check("h) …and its SKILL folder is gone", had and not skill_dirs(role_sess), (had, skill_dirs(role_sess)))
    await drain()

    await run("slow", "job")
    await drain(signals=False)
    wf = latest()
    role_sess = so.get_session(wf["session_id"])
    had = bool(skill_dirs(role_sess))
    r = await api.post(f"/api/signals/{wf['signal_id']}/deny")
    wf = latest()
    check("h) a task message denied on the dashboard (POST deny) stops the run",
          r.status_code == 200 and wf["status"] == "stopped" and "denied" in wf["reason"], (r.status_code, wf["reason"]))
    check("h) …and its role's SKILL folder is gone", had and not skill_dirs(role_sess), (had, skill_dirs(role_sess)))

    await run("slow", "job")
    await drain(signals=False)
    workflow.reconcile()
    check("h) restart keeps a run whose task message is still pending", latest()["status"] == "running")
    role_sess = so.get_session(latest()["session_id"])
    had = bool(skill_dirs(role_sess))
    so.set_signal_status(latest()["signal_id"], "running")      # the run died with the old process
    n_msgs = len(wf_msgs())
    workflow.reconcile()
    wf = latest()
    msgs = wf_msgs()[n_msgs:]
    check("h) restart stops a run with no live task message, naming the step",
          wf["status"] == "stopped" and "step 1 (s1)" in wf["reason"] and len(msgs) == 1
          and "from_step=1" in msgs[0], (wf["status"], wf["reason"]))
    check("h) …and its role's SKILL folder is gone", had and not skill_dirs(role_sess), (had, skill_dirs(role_sess)))
    await drain()
    check("h) nothing is left running", not team._rows("SELECT id FROM workflow_runs WHERE status = 'running'"))
    check("h) git status of the repo is clean at the end", git("status", "--porcelain", cwd=REPO) == "",
          git("status", "--porcelain", cwd=REPO))


async def with_api():
    global api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=so.build_app()), base_url="http://test") as api:
        await main()


try:
    asyncio.run(with_api())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:\n  " + "\n  ".join(fails))
    sys.exit(1)
print("\nall workflow run checks passed")
