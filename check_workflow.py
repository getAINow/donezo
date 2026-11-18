#!/usr/bin/env python3
"""Guards for workflow definitions (a director's ordered list of roles), through the real MCP tools
and module against a throwaway database. What can go wrong quietly:
  - a bad definition half-stored, or replacing a good one;
  - one director seeing or overwriting another director's workflow;
  - a sub-agent or verifier defining workflows.

    python3 check_workflow.py
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import os  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="check_workflow_"))
os.environ.update({"ORCH_DB": "check_workflow", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"), "ORCH_DRY_RUN": "1"})
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
so.init_db()
signal_mcp._INPROC = True
WS = so.DEFAULT_WORKSPACE
for sid, name in (("dir-a", "lead-a"), ("dir-b", "lead-b"), ("sub-x", "sub-x-feature"), ("ver-x", "ver-x-verify")):
    so.register_session(sid, name, cwd=str(TMP), model="", workspace_id=WS)
team._set_team("sub-x", "dir-a", "subagent")
team._set_team("ver-x", "dir-a", "verifier")


def step(role, **kw):
    return {"role": role, "job": f"{role} job", "playbook": f"Do {role}.", "tools": [], **kw}


def three():
    return [step("scene-creator"), step("reference-makeup", tools=["t1"]), step("reviewer")]


def rows():
    return team._rows("SELECT * FROM workflows ORDER BY director_id, name")


async def main():
    r = await signal_mcp.define_workflow(from_role="lead-a", name="shoot-scene", purpose="Make a scene", steps=three())
    check("a) define says created with the role order",
          r == "Workflow 'shoot-scene' created: scene-creator → reference-makeup → reviewer", r)
    ls = await signal_mcp.list_workflows(from_role="lead-a")
    check("a) list shows name, purpose, roles in order, no playbook",
          "shoot-scene — Make a scene" in ls and "Do scene-creator" not in ls
          and ls.index("scene-creator") < ls.index("reference-makeup") < ls.index("reviewer"), ls)
    check("a) module read-back keeps order and tools",
          [s["role"] for s in workflow.get("dir-a", "shoot-scene")["steps"]] == ["scene-creator", "reference-makeup", "reviewer"]
          and workflow.get("dir-a", "shoot-scene")["steps"][1]["tools"] == ["t1"])
    check("a) other director sees nothing", workflow.list_for("dir-b") == []
          and "no workflows" in await signal_mcp.list_workflows(from_role="lead-b"))

    r = await signal_mcp.define_workflow(from_role="lead-a", name="shoot-scene", purpose="v2",
                                         steps=[step("alpha"), step("beta")])
    got = [x for x in rows() if (x["director_id"], x["name"]) == ("dir-a", "shoot-scene")]
    check("b) redefining replaces: one row, new steps", "replaced" in r and len(got) == 1
          and [s["role"] for s in workflow.get("dir-a", "shoot-scene")["steps"]] == ["alpha", "beta"], r)

    await signal_mcp.define_workflow(from_role="lead-b", name="shoot-scene", purpose="B's own", steps=[step("only")])
    check("c) same name for two directors, each sees only their own",
          len(rows()) == 2 and workflow.get("dir-b", "shoot-scene")["purpose"] == "B's own"
          and workflow.get("dir-a", "shoot-scene")["purpose"] == "v2")

    before = rows()
    bad = {
        "zero steps": dict(steps=[]),
        "13 steps": dict(steps=[step(f"r{i}") for i in range(13)]),
        "duplicate roles": dict(steps=[step("x"), step("x")]),
        "role feature": dict(steps=[step("feature")]),
        "role verify": dict(steps=[step("verify")]),
        "empty job": dict(steps=[step("x", job="  ")]),
        "empty playbook": dict(steps=[step("x", playbook="")]),
        "playbook 20001": dict(steps=[step("x", playbook="p" * 20001)]),
        "uppercase name": dict(name="Shoot"),
        "space in name": dict(name="shoot scene"),
        "uppercase role": dict(steps=[step("Reviewer")]),
        "empty purpose": dict(purpose=" "),
        "long purpose": dict(purpose="p" * 301),
        "empty tool string": dict(steps=[step("x", tools=["ok", ""])]),
        "too many tools": dict(steps=[step("x", tools=[f"t{i}" for i in range(101)])]),
        "tools not a list": dict(steps=[step("x", tools="a")]),
    }
    for label, over in bad.items():
        args = {"from_role": "lead-a", "name": "other-flow", "purpose": "p", "steps": [step("ok")], **over}
        r = await signal_mcp.define_workflow(**args)
        check(f"d) rejects {label} with a message, stores nothing",
              r.startswith("⛔") and len(r) > 20 and rows() == before, r)
    check("d) 20000-char playbook and empty tools are accepted",
          "created" in await signal_mcp.define_workflow(from_role="lead-a", name="edge", purpose="p",
                                                        steps=[step("x", playbook="p" * 20000)]))

    n = len(rows())
    for who in ("sub-x-feature", "ver-x-verify"):
        a = await signal_mcp.define_workflow(from_role=who, name="sneak", purpose="p", steps=[step("x")])
        b = await signal_mcp.list_workflows(from_role=who)
        check(f"e) {who} is refused", a.startswith("⛔") and b.startswith("⛔") and len(rows()) == n, f"{a} | {b}")


try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} FAILED:\n  " + "\n  ".join(fails))
    sys.exit(1)
print("\nall workflow checks passed")
