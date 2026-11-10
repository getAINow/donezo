#!/usr/bin/env python3
"""A background run must end at claude's first `result`, even when the agent left a dev server up.

(a) the fake claude prints "REAL REPORT", keeps a child holding its stdout/stderr, then a "LATE TURN":
    the run returns within the grace with the first result, and the fake and its child are stopped
    (one run event names them).
(b) the fake prints a result and exits: the run returns at once, no "stopped" event.
(c) a fake mid-turn with a child is cancelled through team._kill: both are gone.
Everything lives under a throwaway HOME; no real claude is started.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import psutil

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="check_leftovers_"))
os.environ.update({"HOME": str(TMP), "USERPROFILE": str(TMP), "ORCH_DB": "check_leftovers",
                   "ORCH_DRY_RUN": "1", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"),
                   "CLAUDE_CONFIG_DIR": str(TMP / "claude-config")})
sys.path.insert(0, str(HERE))
import session_orchestrator as so     # noqa: E402
import team                           # noqa: E402

assert so.DB_DIR.is_relative_to(TMP), so.DB_DIR    # never the real ~/.session_orch_db
so._ensure_db()
so.register_session("card-l", "leftovers", cwd=str(TMP), model="", workspace_id=so.DEFAULT_WORKSPACE)

GRACE = getattr(so, "RESULT_GRACE_S", 10)
FAKE = TMP / "fake_claude.py"
FAKE.write_text(f"""import json, os, subprocess, sys, time
mode = sys.stdin.read().strip()
def emit(text):
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": text,
                      "num_turns": 1, "usage": {{"output_tokens": 1}}}}), flush=True)
if mode == "normal":
    emit("NORMAL")
    sys.exit(0)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                         stdout=sys.stdout, stderr=sys.stderr)
with open(os.path.join({str(TMP)!r}, mode + ".pids"), "w") as f:
    json.dump([os.getpid(), child.pid], f)
if mode == "leftover":
    emit("REAL REPORT")
    time.sleep(3)
    emit("LATE TURN")
time.sleep(120)
""", encoding="utf-8")
if os.name == "nt":
    so.CLAUDE_BIN = str(TMP / "fake_claude.cmd")
    Path(so.CLAUDE_BIN).write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
else:
    so.CLAUDE_BIN = str(TMP / "fake_claude")
    Path(so.CLAUDE_BIN).write_text(f"#!{sys.executable}\n" + FAKE.read_text(encoding="utf-8"),
                                   encoding="utf-8")
    os.chmod(so.CLAUDE_BIN, 0o755)
so.DRY_RUN = False
so.STREAM = True

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + f" — {detail}", flush=True)


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def pids(mode):
    p = TMP / f"{mode}.pids"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []


def gone_soon(ps, timeout=5):
    end = time.monotonic() + timeout
    while any(alive(p) for p in ps) and time.monotonic() < end:
        time.sleep(0.1)
    return [p for p in ps if alive(p)]


def kill_all(mode):
    for p in pids(mode):
        try:
            psutil.Process(p).kill()
        except psutil.Error:
            pass


async def run(prompt, timeout):
    """On timeout the check kills the fake itself (master never returns on its own: its finally
    re-awaits proc.wait() after a cancel), so the check always ends and shows what came back."""
    events = []

    async def on_event(kind, summary, payload):
        events.append((kind, summary))

    t0 = time.monotonic()
    task = asyncio.create_task(so._run_claude(so.get_session("card-l"), prompt, on_event=on_event))
    done, _ = await asyncio.wait({task}, timeout=timeout)
    dt = time.monotonic() - t0
    if not done:
        kill_all(prompt)
    r = await task
    if not done:
        r = {**r, "result": f"did not return in {timeout}s; after a forced kill: {r['result']!r}"}
    return r, dt, [s for _, s in events if "stopped" in s]


async def main():
    r, dt, stopped = await run("leftover", GRACE + 5)
    check("(a) run returns within grace + 5s", dt < GRACE + 5, f"{dt:.1f}s")
    check("(a) result is the first one", r["result"] == "REAL REPORT", r["result"])
    ps = pids("leftover")
    check("(a) fake and child are gone", len(ps) == 2 and not gone_soon(ps), f"pids {ps}")
    check("(a) exactly one 'stopped' event", len(stopped) == 1, stopped)
    if stopped:
        check("(a) it names the pids", all(str(p) in stopped[0] for p in ps), stopped[0])

    r, dt, stopped = await run("normal", GRACE + 5)
    check("(b) normal exit returns at once", dt < 2, f"{dt:.1f}s")
    check("(b) result", r["result"] == "NORMAL", r["result"])
    check("(b) no 'stopped' event", not stopped, stopped)

    task = asyncio.create_task(run("cancel", 10))
    end = time.monotonic() + 10
    while len(pids("cancel")) < 2 and time.monotonic() < end:
        await asyncio.sleep(0.1)
    ps = pids("cancel")
    team._kill("card-l")
    left = await asyncio.to_thread(gone_soon, ps)
    check("(c) fake and child are gone after team._kill", len(ps) == 2 and not left,
          f"pids {ps}, still alive {left}")
    r, dt, _ = await task
    check("(c) cancelled run returns", dt < 10, f"{dt:.1f}s, {r['result'][:80]}")


try:
    asyncio.run(main())
finally:
    for m in ("leftover", "cancel"):     # never leave sleepers behind, even when a case failed
        kill_all(m)
    shutil.rmtree(TMP, ignore_errors=True)
if fails:
    print(f"\n{len(fails)} check(s) failed")
    sys.exit(1)
print("\nall checks passed")
