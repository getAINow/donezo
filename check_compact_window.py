#!/usr/bin/env python3
"""Every claude Donezo starts gets CLAUDE_CODE_AUTO_COMPACT_WINDOW=300000 unless the user chose.

The default is applied at import, so cases (a)-(d) import the module in a fresh subprocess with a
controlled env. Case (e) drives a real background run against a scripted fake claude that records
its environment. Everything lives under a throwaway HOME / CLAUDE_CONFIG_DIR.

    python3 check_compact_window.py
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

HERE = Path(__file__).resolve().parent
VAR = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
TMP = Path(tempfile.mkdtemp(prefix="check_compact_"))
CFG = TMP / "claude-config"
CFG.mkdir()
SETTINGS = CFG / "settings.json"
ENV = {"HOME": str(TMP), "USERPROFILE": str(TMP), "ORCH_DB": "check_compact", "ORCH_DRY_RUN": "1",
       "ORCH_WORKSPACES_ROOT": str(TMP / "ws"), "CLAUDE_CONFIG_DIR": str(CFG)}
os.environ.pop(VAR, None)
os.environ.update(ENV)

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + f" — {detail}")


def imported_value(preset=None, settings=None):
    if settings is None:
        SETTINGS.unlink(missing_ok=True)
    else:
        SETTINGS.write_text(settings, encoding="utf-8")
    env = dict(os.environ)
    if preset is not None:
        env[VAR] = preset
    r = subprocess.run([sys.executable, "-c",
                        f"import session_orchestrator, os; print(os.environ.get({VAR!r}))"],
                       cwd=str(HERE), env=env, capture_output=True, text=True, timeout=120)
    return r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else f"crash: {r.stderr[-300:]}"


v = imported_value()
check("(a) nothing set → 300000", v == "300000", v)
v = imported_value(preset="500000")
check("(b) preset 500000 wins", v == "500000", v)
v = imported_value(settings=json.dumps({"autoCompactWindow": 400000}))
check("(c) user's autoCompactWindow → variable left unset", v == "None", v)
check("    and their settings file is untouched",
      json.loads(SETTINGS.read_text(encoding="utf-8")) == {"autoCompactWindow": 400000})
v = imported_value(settings="{not json")
check("(d) broken settings.json → 300000, no crash", v == "300000", v)
SETTINGS.unlink()

# ── (e) a background run hands the variable to claude ──
sys.path.insert(0, str(HERE))
import session_orchestrator as so     # noqa: E402

assert so.DB_DIR.is_relative_to(TMP), so.DB_DIR    # never the real ~/.session_orch_db
so._ensure_db()
so.register_session("card-e", "env", cwd=str(TMP), model="", workspace_id=so.DEFAULT_WORKSPACE)

FAKE = TMP / "fake_claude.py"
SEEN = TMP / "fake_env.json"
FAKE.write_text(f"""import json, os, sys
open({str(SEEN)!r}, "w").write(json.dumps({{"v": os.environ.get({VAR!r})}}))
sys.stdin.read()
for ev in [{{"type": "system", "subtype": "init", "model": "fake", "tools": []}},
           {{"type": "result", "subtype": "success", "is_error": False, "result": "done",
             "num_turns": 1, "usage": {{"output_tokens": 1}}}}]:
    print(json.dumps(ev), flush=True)
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


async def noop(*_):
    pass


async def background():
    r = await so._run_claude(so.get_session("card-e"), "hello", on_event=noop)
    check("the fake run succeeds", r["ok"], r.get("ok"))
    seen = json.loads(SEEN.read_text(encoding="utf-8"))["v"] if SEEN.is_file() else "not run"
    check("(e) background claude sees 300000", seen == "300000", seen)


asyncio.run(background())

shutil.rmtree(TMP, ignore_errors=True)
if fails:
    print(f"\n{len(fails)} check(s) failed")
    sys.exit(1)
print("\nall checks passed")
