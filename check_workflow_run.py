#!/usr/bin/env python3
"""Guards for running workflows (workflow.py runs + the MCP tools run_workflow, cancel_workflow,
finish_step), against a throwaway database, a real git repo and a scripted fake CLI.
What can go wrong quietly:
  - a run advancing mid-turn, skipping a step, or looping forever on rejections / failures;
  - a later role not seeing the earlier reports, or a sent-back role not seeing the findings;
  - the director told nothing, or told twice, or told after a cancel;
  - redefining a workflow changing a run already in flight;
  - a role's SKILL folder showing in the user's git status or left behind;
  - a run hanging 'running' after a restart, a removed director or a denied task message;
  - a role that delegates: its step ending while a sub-agent still works, or its sub-agents
    outliving the step; a director delegating into the folder its roles are merging in;
  - a role finishing its step without having delegated (it must be a sub-director), or a retried
    role inheriting its failed session's delegations; work with no commit refused.

    python3 check_workflow_run.py
"""
import asyncio
import json
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
# A sub-agent commits feature.py in its worktree (brief NOCOMMIT: no change, CRASH: its run fails;
# investigate: reads only); its verifier passes it. WAIT[subtask] = what the run looked like while
# that sub-agent worked (its role's turn had already ended).
# Roles not in REAL test run order, not delegation: each of their sessions gets one passed sub-agent
# row (stub_delegation) so finish_step accepts them. REAL roles delegate for real.
PLAN, ROLE_RUNS, DURING, RESPONSES, ROLE_OF, WAIT, REAL = {}, [], {}, [], {}, {}, {"d1"}


def stub_delegation(session_id):
    if not team._row("SELECT 1 FROM subtasks WHERE director_id = ?", (session_id,)):
        now = so._now()
        team._exec("INSERT INTO subtasks (id, workspace_id, director_id, kind, playbook, status, created_at, "
                   "updated_at) VALUES (?, ?, ?, 'subagent', 'investigate', 'passed', ?, ?)",
                   (team._new_id(), WS, session_id, now, now))


def _role_in(prompt):
    i = prompt.find("[WORKFLOW STEP ")
    return prompt[i:].split("]")[0].split(" · ")[-1] if i >= 0 else ""


def _key_in(prompt):
    i = prompt.find('key="')
    return prompt[i + 5:prompt.index('"', i + 5)] if i >= 0 else ""


def _st_in(prompt, tag):
    return prompt.split(f"[{tag} ")[1].split()[0].rstrip("]")


def _ok(session, text):
    return {"ok": True, "result": text, "session_id": session["id"], "tokens": 1, "raw": {}}


async def fake_run(session, prompt, on_event=None, dry_run=False):
    name, cwd = session["name"], Path(session.get("cwd") or ".")
    if "[SUB-AGENT TASK " in prompt or "[VERIFY " in prompt:
        if TMP.resolve() not in cwd.resolve().parents:
            return {"ok": False, "result": f"refusing to write outside {TMP}: {cwd}", "session_id": session["id"],
                    "tokens": 0, "raw": {}}
        if "[VERIFY " in prompt:
            team.submit_verdict(name, _st_in(prompt, "VERIFY"), "PASS", "ran it: ok", key=_key_in(prompt))
            return _ok(session, "verified")
        st = team.get_subtask(_st_in(prompt, "SUB-AGENT TASK"))
        wf, role = workflow.role_run(st["director_id"]), so.get_session(st["director_id"]) or {}
        WAIT[st["id"]] = {"run": wf, "skill": (Path(role.get("cwd", "")) / ".claude" / "skills" / role.get("name", "x")
                                              / "SKILL.md").exists(),
                          "status": git("status", "--porcelain", "--untracked-files=all", cwd=role.get("cwd", ".")),
                          "cwd": str(cwd), "common": git("rev-parse", "--path-format=absolute", "--git-common-dir",
                                                         cwd=cwd)}
        if "CRASH" in st["brief"]:
            return {"ok": False, "result": "CLI died", "session_id": session["id"], "tokens": 0, "raw": {}}
        if st["playbook"] == "investigate" or "NOCOMMIT" in st["brief"]:
            return _ok(session, "Done through the service; no file changed. Evidence: read it back.")
        (cwd / "feature.py").write_text(f"VALUE = '{st['id']}'\n", encoding="utf-8")
        git("add", "feature.py", cwd=cwd)
        git("commit", "-q", "-m", "feature", cwd=cwd)
        return _ok(session, "Added feature.py. Evidence: ran it.")
    role = _role_in(prompt) or ROLE_OF.get(name, "")
    if not role:
        return _ok(session, "noted")
    ROLE_OF[name] = role
    ROLE_RUNS.append((role, name, prompt))
    DURING.setdefault(name, []).append((
        (cwd / ".claude" / "skills" / name / "SKILL.md").exists(),
        git("status", "--porcelain", "--untracked-files=all", cwd=cwd)))
    if role not in REAL:
        stub_delegation(session["id"])
    plan = PLAN.get(role, [[("done", "", f"{role} report")]])
    act = plan.pop(0) if len(plan) > 1 else plan[0]
    if callable(act):
        act = await act(session)
    if act == "crash":
        return {"ok": False, "result": "CLI died", "session_id": session["id"], "tokens": 0, "raw": {}}
    for result, back_to, report in act:
        r = await signal_mcp.finish_step(from_role=name, result=result, report=report, back_to=back_to,
                                         workspace_id=WS)
        run = latest(session["parent_id"])
        RESPONSES.append((role, result, back_to, r, run["status"], run["step"], run["session_id"] == session["id"]))
    return {"ok": True, "result": f"{role} finished", "session_id": session["id"], "tokens": 5, "raw": {}}


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
    check("a) the role is told it must delegate: sub-director, no hands-on work, spawn_subagent, investigate",
          all(x in p1 for x in ("You are a sub-director", "You do no hands-on work yourself",
                                "spawn_subagent(from_role=", '"investigate"', "[SUB-AGENT PASSED]",
                                "never push", "brief or done_when was wrong", "refused until"))
          and "may delegate" not in p1.lower() and "Your sub-agents get these tools" not in p1, p1)
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
          all(d == (True, "") for n in names for d in DURING[n]), {n: DURING[n] for n in names})
    left = [p.name for p in (REPO / ".claude" / "skills").glob("sub-*")] if (REPO / ".claude" / "skills").exists() else []
    check("a) the role SKILL folders are gone after their steps", left == [], left)
    check("a) a recorded done does not advance the run mid-turn",
          all(st == "running" and same for role, *_, st, _s, same in RESPONSES if role.startswith("a")),
          RESPONSES)
    check("a) a role session of an ended run cannot spawn sub-agents",
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

    # ── i) a role delegates: the step waits for its sub-agents ───────────────
    await define("deleg", ["d1", "d2"])
    got = {}

    async def d1_first(session):
        got["spawn"] = await signal_mcp.spawn_subagent(from_role=session["name"], playbook="feature",
                                                       brief="add feature.py", done_when="feature.py exists",
                                                       workspace_id=WS)
        got["a"] = got["spawn"].split()[1]
        b = await signal_mcp.spawn_subagent(from_role=session["name"], playbook="feature", brief="spare",
                                            done_when="x", workspace_id=WS)
        got["cancel_b"] = await signal_mcp.cancel_subagent(from_role=session["name"], subtask_id=b.split()[1],
                                                           workspace_id=WS)
        got["list"] = await signal_mcp.list_subagents(from_role=session["name"], workspace_id=WS)
        got["finish"] = await signal_mcp.finish_step(from_role=session["name"], result="done", report="early",
                                                     workspace_id=WS)
        got["director"] = await signal_mcp.spawn_subagent(from_role="lead", playbook="feature", brief="x",
                                                          done_when="y", workspace_id=WS)
        return []

    async def d1_second(session):
        git("merge", "--no-ff", "-q", "-m", "merge", team.get_subtask(got["a"])["branch"], cwd=session["cwd"])
        return [("done", "", "d1 merged the sub-agent's feature.py")]

    async def d2_turn(session):
        got["earlier"] = team.spawn_subagent(got["d1"]["id"], "feature", "x", "y")
        return [("done", "", "d2 report")]
    PLAN["d1"], PLAN["d2"] = [d1_first, d1_second], [d2_turn]
    await run("deleg", "delegate job")
    await drain(signals=False)
    got["d1"] = so.get_session(latest()["session_id"])
    await drain()
    wf, a = latest(), team.get_subtask(got["a"])
    d1 = got["d1"]
    check("i) a role's spawn_subagent returns a subtask id", got["spawn"].startswith("Sub-agent st_"), got["spawn"])
    check("i) the role is the sub-agent's director", a["director_id"] == d1["id"], a["director_id"])
    w = WAIT.get(got["a"], {})
    check("i) it worked in a git worktree of the director's repo",
          a["repo"] == str(REPO) and w.get("cwd") == a["worktree"] != str(REPO)
          and Path(w.get("common", "")) == (REPO / ".git").resolve(), (a["repo"], w.get("cwd"), w.get("common")))
    check("i) list_subagents and cancel_subagent work for the role",
          got["a"] in got["list"] and got["cancel_b"].startswith("Cancelled"), (got["list"], got["cancel_b"]))
    check("i) finish_step is refused while a sub-agent is active",
          got["finish"].startswith("⛔") and "still active" in got["finish"], got["finish"])
    check("i) the director's spawn_subagent is refused while its run is running",
          got["director"].startswith("⛔") and "workflow run" in got["director"], got["director"])
    check("i) while the sub-agent works the run waits: step 1, same role session, no failure counted",
          (w.get("run") or {}).get("status") == "running" and w["run"]["step"] == 0
          and w["run"]["session_id"] == d1["id"] and w["run"]["retried"] == 0 and w["run"]["spawned"] == 1,
          {k: (w.get("run") or {}).get(k) for k in ("status", "step", "session_id", "retried", "spawned")})
    d1_runs = [(n, p) for role, n, p in ROLE_RUNS if role == "d1"]
    check("i) the passed sub-agent's message starts a second turn of the same role session",
          len(d1_runs) == 2 and {n for n, _ in d1_runs} == {d1["name"]}
          and f"[SUB-AGENT PASSED] {got['a']}" in d1_runs[1][1], [n for n, _ in d1_runs])
    check("i) finish_step in that turn advances the run to step 2 when the turn ends",
          [x[0] for x in runs_of("d")] == ["d1", "d1", "d2"] and wf["status"] == "done"
          and "d1 merged" in wf["reports"]["d1"], ([x[0] for x in runs_of("d")], wf["status"]))
    check("i) the role merged the passed work into the director's folder", (REPO / "feature.py").exists())
    check("i) the role's SKILL stays for both turns and the wait, git status stays clean",
          DURING[d1["name"]] == [(True, "")] * 2 and w.get("skill") is True and w.get("status") == "",
          (DURING[d1["name"]], w.get("skill"), w.get("status")))
    check("i) …and the SKILL is gone after the step", not skill_dirs(d1), skill_dirs(d1))
    check("i) the role of an earlier step of the running run cannot spawn", "cannot spawn" in
          got["earlier"].get("error", ""), got["earlier"])
    for who, sid in (("a role of an ended run", d1["id"]), ("a sub-agent", a["session_id"]),
                     ("a verifier", a["verifier_id"])):
        r = team.spawn_subagent(sid, "feature", "x", "y")
        check(f"i) {who} cannot spawn sub-agents", "cannot spawn" in r.get("error", ""), r)
    r = await signal_mcp.spawn_subagent(from_role="lead", playbook="feature", brief="x", done_when="y",
                                        workspace_id=WS)
    check("i) the director's spawn_subagent works again after the run", r.startswith("Sub-agent st_"), r)
    team.cancel(r.split()[1])

    # cancel / stop / director removal cancel the role's active sub-agents
    async def hold(session):
        r = await signal_mcp.spawn_subagent(from_role=session["name"], playbook="feature", brief="hold",
                                            done_when="x", workspace_id=WS)
        HELD.append(r.split()[1])
        return []

    async def hold_crash(session):
        await hold(session)
        return "crash"
    HELD = []

    async def first_turn(name, who="lead", did="dir-1"):
        """Run only the first role's first turn; the sub-agent it spawned gets its session and brief."""
        await run(name, f"{name} job", who=who)
        await drain(signals=False)
        await so.process_signal(team._row("SELECT * FROM signals WHERE id = ?", (latest(did)["signal_id"],)))
        await drain(signals=False)
        return latest(did), team.get_subtask(HELD[-1])

    await define("hold", ["k1", "k2"])
    PLAN["k1"] = [hold]
    wf, st = await first_turn("hold")
    role = so.get_session(wf["session_id"])
    check("i) a waiting step: run running, the sub-agent running", wf["status"] == "running" and st["status"] == "running",
          (wf["status"], st["status"]))
    await signal_mcp.cancel_workflow(from_role="lead", workspace_id=WS)
    st = team.get_subtask(st["id"])
    brief = team._row("SELECT status FROM signals WHERE id = ?", (st["signal_id"],))
    check("i) cancel_workflow cancels the role's active sub-agent and denies its brief",
          latest()["status"] == "cancelled" and st["status"] == "cancelled" and brief["status"] == "denied",
          (latest()["status"], st["status"], brief["status"]))
    check("i) …and the role's SKILL is gone", not skill_dirs(role), skill_dirs(role))
    await drain()

    PLAN["k1"] = [hold_crash]
    n = len(HELD)
    await run("hold", "crash job")
    await drain()
    wf = latest()
    sts = [team.get_subtask(x)["status"] for x in HELD[n:]]
    check("i) a failed role turn is its own failure even with a sub-agent: retried once, then stopped",
          wf["status"] == "stopped" and "failed twice" in wf["reason"], (wf["status"], wf["reason"]))
    check("i) the retry and the stop cancel the role's active sub-agents", sts == ["cancelled"] * 2, sts)

    REPO3 = TMP / "repo3"
    REPO3.mkdir()
    git("init", "-q", "-b", "main", cwd=REPO3)
    git("-c", "user.email=d@example.com", "-c", "user.name=D", "commit", "-q", "--allow-empty", "-m", "i", cwd=REPO3)
    so.register_session("dir-3", "lead3", cwd=str(REPO3), model="", workspace_id=WS)
    await define("hold", ["k1", "k2"], who="lead3")
    PLAN["k1"] = [hold]
    wf, st = await first_turn("hold", who="lead3", did="dir-3")
    r = await api.post("/api/sessions/dir-3/unregister")
    check("i) removing the director cancels its run and the role's active sub-agent",
          r.status_code == 200 and latest("dir-3")["status"] == "cancelled"
          and team.get_subtask(st["id"])["status"] == "cancelled",
          (r.status_code, latest("dir-3")["status"], team.get_subtask(st["id"])["status"]))
    await drain()

    # a denied message to the waiting role; a restart while it waits
    await define("note", ["n1", "n2"])
    notes = []

    async def note_turn(session):
        notes.extend(team._enqueue(session["id"], f"note {k}", WS) for k in (1, 2))
        return []
    PLAN["n1"] = [note_turn]
    await run("note", "note job")
    await drain(signals=False)
    await so.process_signal(team._row("SELECT * FROM signals WHERE id = ?", (latest()["signal_id"],)))
    role = so.get_session(latest()["session_id"])
    check("i) a turn that ends with a message still queued for the role waits",
          latest()["status"] == "running" and latest()["retried"] == 0, latest()["status"])
    n_msgs = len(wf_msgs())
    await api.post(f"/api/signals/{notes[0]}/deny")
    check("i) denying one of two queued messages keeps the run", latest()["status"] == "running")
    await api.post(f"/api/signals/{notes[1]}/deny")
    wf = latest()
    check("i) denying the last one stops the run (nothing left to wait for)",
          wf["status"] == "stopped" and "denied" in wf["reason"] and len(wf_msgs()[n_msgs:]) == 1, wf["reason"])
    check("i) …and the role's SKILL is gone", not skill_dirs(role), skill_dirs(role))

    PLAN["k1"] = [hold]
    wf, st = await first_turn("hold")
    team.reconcile()
    workflow.reconcile()
    check("i) restart keeps a run whose role has an active sub-agent",
          latest()["status"] == "running" and team.get_subtask(st["id"])["status"] == "running")
    so.set_signal_status(st["signal_id"], "processing")          # the sub-agent's turn died with the process
    team.reconcile()
    workflow.reconcile()
    msg = team._row("SELECT id, status, message FROM signals WHERE to_session = ? ORDER BY id DESC LIMIT 1",
                    (wf["session_id"],))
    check("i) restart keeps a run whose role has a pending message (the sub-agent's failure)",
          latest()["status"] == "running" and msg["status"] == "pending" and "[SUB-AGENT FAILED]" in msg["message"],
          (latest()["status"], msg["status"]))
    so.set_signal_status(msg["id"], "processing")
    workflow.reconcile()
    check("i) restart stops a run whose role has neither", latest()["status"] == "stopped", latest()["status"])
    await drain()

    # ── l) a role is a sub-director: finish_step needs a delegation that ended ──
    SPAWNED, FIN = [], {}

    def deleg(playbook="feature", brief="add feature.py", fin=""):
        """A role turn that spawns one sub-agent (after a finish_step recorded in FIN[fin] when fin)."""
        async def turn(session):
            if fin:
                await finish_early(session, fin)
            r = await signal_mcp.spawn_subagent(from_role=session["name"], playbook=playbook, brief=brief,
                                                done_when="observed", workspace_id=WS)
            SPAWNED.append(r.split()[1])
            return []
        return turn

    async def finish_early(session, tag):
        FIN[tag] = (await signal_mcp.finish_step(from_role=session["name"], result="done", report="early",
                                                 workspace_id=WS), latest()["result"], latest()["step"])

    async def merged(session):
        """The turn on [SUB-AGENT PASSED/FAILED]: merge a passed branch, then finish."""
        st = team.get_subtask(SPAWNED[-1])
        if st["status"] == "passed" and st["branch"]:
            git("merge", "--no-ff", "-q", "-m", "merge", st["branch"], cwd=session["cwd"])
        return [("done", "", f"{ROLE_OF[session['name']]}: {st['id']} {st['status']}")]

    async def l1_first(session):
        await finish_early(session, "none")
        r = await signal_mcp.spawn_subagent(from_role=session["name"], playbook="feature", brief="spare",
                                            done_when="x", workspace_id=WS)
        await signal_mcp.cancel_subagent(from_role=session["name"], subtask_id=r.split()[1], workspace_id=WS)
        await finish_early(session, "cancelled")
        await deleg()(session)
        await finish_early(session, "active")
        return []

    def refused(tag, why):
        r, rec, i = FIN.get(tag, ("", None, None))
        check(f"l) finish_step {tag}: refused, the run unchanged", r.startswith("⛔") and why in r
              and rec == {} and i == 0, FIN.get(tag))

    REAL.update({"l1", "l2", "m1", "m2", "o1"})
    await define("subdir", ["l1", "l2"])
    PLAN["l1"] = [l1_first, merged, deleg(), merged]
    PLAN["l2"] = [deleg("investigate", "read app.py and report"), [("reject", "l1", "FIND-L: l1 must redo it")],
                  deleg("investigate", "read app.py again"), merged]
    n_msgs, n_resp = len(wf_msgs()), len(RESPONSES)
    await run("subdir", "sub-director job")
    await drain()
    wf, lr = latest(), runs_of("l")
    refused("none", "not delegated")
    refused("cancelled", "not delegated")
    refused("active", "still active")
    check("l) a reviewer that delegated an investigate sub-agent and rejected sends the run back to step 1",
          [x[0] for x in lr] == ["l1", "l1", "l2", "l2"] * 2 and "FIND-L" in lr[4][1] and "FIND-L" not in lr[0][1],
          [x[0] for x in lr])
    sts = [team.get_subtask(x) for x in SPAWNED]
    check("l) every unit was a real sub-agent that passed: feature, investigate, feature, investigate",
          [(s["playbook"], s["status"]) for s in sts] == [("feature", "passed"), ("investigate", "passed")] * 2
          and all(s["director_id"] != "dir-1" for s in sts), [(s["playbook"], s["status"]) for s in sts])
    resp = [x for x in RESPONSES[n_resp:] if x[0] in ("l1", "l2")]
    check("l) finish_step after a passed sub-agent is accepted (done and reject)",
          [(x[0], x[1]) for x in resp] == [("l1", "done"), ("l2", "reject"), ("l1", "done"), ("l2", "done")]
          and not any(x[3].startswith("⛔") for x in resp), resp)
    msgs = wf_msgs()[n_msgs:]
    check("l) the run ends done with one [WORKFLOW DONE]",
          wf["status"] == "done" and len(msgs) == 1 and msgs[0].startswith("[WORKFLOW DONE]"), (wf["status"], msgs))

    # No commit (work done only through MCP tools): it passes, the role is told, the run moves on.
    await define("nocommit", ["m1", "m2"])
    PLAN["m1"] = [deleg(brief="NOCOMMIT: change it on the service"), merged]
    PLAN["m2"] = [deleg(), merged]
    n_msgs, k = len(wf_msgs()), len(SPAWNED)
    await run("nocommit", "no commit job")
    await drain()
    wf, mr = latest(), runs_of("m")
    st, st2 = team.get_subtask(SPAWNED[k]), team.get_subtask(SPAWNED[k + 1])
    check("l) a role's feature sub-agent with no commit passes on its verifier's word",
          st["status"] == "passed" and st["worktree"] and st["head_sha"] == st["base_sha"],
          (st["status"], st["head_sha"], st["base_sha"]))
    check("l) …the role gets [SUB-AGENT PASSED] and its finish_step advances the run to step 2",
          [x[0] for x in mr] == ["m1", "m1", "m2", "m2"] and f"[SUB-AGENT PASSED] {st['id']}" in mr[1][1]
          and st2["status"] == "passed", [x[0] for x in mr])
    msgs = wf_msgs()[n_msgs:]
    check("l) two steps, each delegating one sub-agent that passed: done, one [WORKFLOW DONE], m2's work merged",
          wf["status"] == "done" and len(msgs) == 1 and msgs[0].startswith("[WORKFLOW DONE]")
          and st2["id"] in (REPO / "feature.py").read_text(encoding="utf-8"), (wf["status"], msgs))

    # A role's own failure after it delegated: the retry is a fresh session that starts from zero.
    await define("retry", ["o1", "o2"])
    PLAN["o1"] = [deleg(), "crash", deleg(brief="CRASH: add feature.py", fin="retry"), merged]
    n_resp, k = len(RESPONSES), len(SPAWNED)
    await run("retry", "retry job")
    await drain()
    wf, o1 = latest(), [n for role, n, _ in ROLE_RUNS if role == "o1"]
    st, st2 = team.get_subtask(SPAWNED[k]), team.get_subtask(SPAWNED[k + 1])
    check("l) the first o1 session delegated one that passed, then failed on its own",
          st["status"] == "passed" and len(o1) == 4 and len(set(o1)) == 2 and o1[0] == o1[1] != o1[2] == o1[3], o1)
    check("l) the retried session's finish_step is refused until it delegates again",
          FIN.get("retry", ("",))[0].startswith("⛔") and "not delegated" in FIN["retry"][0], FIN.get("retry"))
    resp = [x for x in RESPONSES[n_resp:] if x[0] == "o1"]
    check("l) finish_step after a failed sub-agent is accepted and the run goes on",
          st2["status"] == "failed" and [(x[1], x[3][:4]) for x in resp] == [("done", "Step")]
          and wf["status"] == "done" and runs_of("o")[-1][0] == "o2", (st2["status"], resp, wf["status"]))

    # A director's own sub-agent with no commit: as before (not a workflow role).
    n0 = len(to_director())
    res = team.spawn_subagent("dir-1", "feature", "NOCOMMIT: change it on the service", "observed")
    await drain()
    st = team.get_subtask(res["id"])
    check("l) a director's sub-agent with no commit passes and the director is told, as before",
          st["status"] == "passed" and st["head_sha"] == st["base_sha"]
          and any(m.startswith(f"[SUB-AGENT PASSED] {st['id']}") for m in to_director()[n0:]),
          (st["status"], to_director()[n0:]))

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

    # ── j) the Team overview and the dashboard's Stop ───────────────────────
    def seed(rid, did, status, step, reports, reason="", ended=""):
        steps = [{"role": r, "job": f"{r} job", "playbook": "SECRET PLAYBOOK TEXT", "tools": []}
                 for r in ("w1", "w2", "w3")]
        team._exec("INSERT INTO workflow_runs (id, director_id, workspace_id, name, purpose, job, steps, status, "
                   "step, reports, reason, created_at, updated_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rid, did, WS, "seeded", "p", "make it", json.dumps(steps), status, step,
                    json.dumps(reports), reason, "2026-01-01T00:00:00", "2026-01-01T00:00:00", ended))
    team._exec("DELETE FROM workflow_runs")
    seed("wf_run00001", "dir-1", "running", 1, {"w1": "w1 did it"})
    seed("wf_run00002", "peer-1", "stopped", 2, {"w1": "a", "w2": "b"}, "w3 failed twice", "2026-01-01T01:00:00")
    t = (await api.get("/api/team", params={"workspace_id": WS})).json()
    byid = {r["id"]: r for r in t["workflows"]}
    run_, stop_ = byid.get("wf_run00001", {}), byid.get("wf_run00002", {})
    check("j) overview lists the running and the stopped run", set(byid) == {"wf_run00001", "wf_run00002"}, list(byid))
    check("j) running run: steps done, current, pending in order, step 1's report, job and director",
          [x["state"] for x in run_.get("steps", [])] == ["done", "current", "pending"]
          and run_["steps"][0]["report"] == "w1 did it" and run_["steps"][1]["job"] == "w2 job"
          and run_["director_name"] == "lead" and run_["job"] == "make it", run_)
    check("j) stopped run: reason and the step it stopped at",
          stop_.get("reason") == "w3 failed twice"
          and [x["state"] for x in stop_["steps"]] == ["done", "done", "stopped"], stop_)
    check("j) no playbook text anywhere in the payload", "SECRET PLAYBOOK" not in json.dumps(t))
    for i in range(6):
        seed(f"wf_old0000{i}", "peer-1", "done", 2, {}, "", f"2026-01-02T0{i}:00:00")
    t = (await api.get("/api/team", params={"workspace_id": WS})).json()
    ended = [r["id"] for r in t["workflows"] if r["status"] != "running"]
    check("j) only the 5 most recently ended runs are listed", len(ended) == 5 and "wf_run00002" not in ended, ended)
    check("j) a run in another workspace is not listed",
          (await api.get("/api/team", params={"workspace_id": "ws_other"})).json()["workflows"] == [])

    before = len(to_director())
    r = await api.post("/api/workflows/runs/wf_run00001/cancel")
    msgs = to_director()[before:]
    check("j) Stop: 200, run cancelled, the director told once and asked not to reply",
          r.status_code == 200 and workflow.get_run("wf_run00001")["status"] == "cancelled"
          and workflow.get_run("wf_run00001")["reason"] == "cancelled by the user"
          and len(msgs) == 1 and "by the user" in msgs[0] and msgs[0].endswith("Do not reply to this."),
          (r.status_code, msgs))
    r = await api.post("/api/workflows/runs/wf_run00001/cancel")
    check("j) Stop twice: an error and no second message", r.status_code == 400 and len(to_director()[before:]) == 1,
          (r.status_code, to_director()[before:]))
    check("j) Stop of an unknown run is 404", (await api.post("/api/workflows/runs/wf_nope/cancel")).status_code == 404)

    check("j) a card given by a workflow role names the role, not the session",
          team._giver_name({"name": "sub-ab12cd34-2-code-reviewer", "team_role": "role"}) == "code-reviewer"
          and team._giver_name({"name": "lead", "team_role": ""}) == "lead")

    # ── k) saved workflows on the Team tab ──────────────────────────────────
    SENT = "PLAYBOOK-SENTINEL-7f3a"
    so.register_session("dir-a", "alpha", cwd=str(REPO), model="", workspace_id="ws_k_a")
    so.register_session("dir-b", "beta", cwd=str(REPO), model="", workspace_id="ws_k_b")

    def stp(role, tools):
        return {"role": role, "job": f"{role} job <b>x</b>", "playbook": f"{SENT} {role}", "tools": tools}
    for did, ws, nm, steps in (
            ("dir-a", "ws_k_a", "zeta", [stp("a1", ["Edit"]), stp("a2", [])]),
            ("dir-a", "ws_k_a", "alpha-flow", [stp(f"r{i}", ["Write"] if i == 1 else []) for i in range(4)]),
            ("dir-b", "ws_k_b", "other", [stp("b1", [])])):
        check(f"k) define {nm}", workflow.define(ws, did, nm, f"{nm} purpose", steps).get("status") == "created")
    raw = (await api.get("/api/team", params={"workspace_id": "ws_k_a"})).text
    t = json.loads(raw)
    defs = t["workflow_defs"]
    check("k) A's definitions in name order with director, purpose, steps in order",
          [d["name"] for d in defs] == ["alpha-flow", "zeta"] and defs[0]["director_name"] == "alpha"
          and defs[1]["purpose"] == "zeta purpose" and [len(d["steps"]) for d in defs] == [4, 2]
          and defs[1]["steps"] == [{"role": "a1", "job": "a1 job <b>x</b>", "tools": ["Edit"]},
                                   {"role": "a2", "job": "a2 job <b>x</b>", "tools": []}]
          and [s["role"] for s in defs[0]["steps"]] == ["r0", "r1", "r2", "r3"]
          and not any(d["running"] for d in defs), str(defs)[:300])
    check("k) B's definition is absent; no playbook text in the payload; run list key unchanged",
          "other" not in raw and SENT not in raw and t["workflows"] == [] and "directors" in t)
    seed("wf_run0000k", "dir-a", "running", 0, {})
    team._exec("UPDATE workflow_runs SET workspace_id = 'ws_k_a', name = 'zeta' WHERE id = 'wf_run0000k'")
    t = (await api.get("/api/team", params={"workspace_id": "ws_k_a"})).json()
    check("k) a running run marks its definition", [d["running"] for d in t["workflow_defs"]] == [False, True]
          and [r["id"] for r in t["workflows"]] == ["wf_run0000k"])
    r = await api.get("/api/workflows/dir-a/zeta")
    check("k) detail: steps with playbooks", r.status_code == 200
          and [s["playbook"] for s in r.json()["steps"]] == [f"{SENT} a1", f"{SENT} a2"], r.text[:200])
    check("k) detail of an unknown name or director is 404",
          (await api.get("/api/workflows/dir-a/nope")).status_code == 404
          and (await api.get("/api/workflows/nobody/zeta")).status_code == 404)
    await api.post("/api/sessions/dir-a/unregister")
    t = (await api.get("/api/team", params={"workspace_id": "ws_k_a"})).json()
    check("k) after the director is removed its definitions are gone", t["workflow_defs"] == [], t["workflow_defs"])



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
