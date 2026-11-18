#!/usr/bin/env python3
"""Guard for the director-needs-git rule and POST /api/git/setup.

A director delegates through worktrees cut from HEAD, so a director in a folder without a commit
cannot delegate anything. api_spawn refuses it (400, needs_git) before writing anything; the
dashboard then offers /api/git/setup, which makes the folder a local repo with a first commit.

Runs the real app in-process over ASGI, dry-run, with HOME pointed at a temp folder (the DB, the
git config and the home-folder refusal all live there).

    python3 check_director_git.py
"""
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HOME = tempfile.mkdtemp()
os.environ["HOME"] = os.environ["USERPROFILE"] = HOME   # before import: DB_DIR = Path.home()/...
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["ORCH_DB"] = "check_director_git"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_DRY_RUN"] = "1"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
so._ensure_db()

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


def git(folder, *args):
    return subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True).stdout.strip()


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        async def names():
            return {s["name"] for s in (await c.get("/api/sessions")).json()}

        def spawn(name, template, cwd):
            return c.post("/api/sessions/spawn", json={"name": name, "template": template, "cwd": cwd})

        plain = tempfile.mkdtemp()
        Path(plain, "notes.txt").write_text("hello", encoding="utf-8")
        Path(plain, "secret.log").write_text("x", encoding="utf-8")
        Path(plain, ".gitignore").write_text("*.log\n", encoding="utf-8")

        r = await spawn("boss", "director", plain)
        check("director in a plain folder is refused with needs_git",
              r.status_code == 400 and r.json().get("needs_git") is True, r.text[:200])
        check("refused director created no session", "boss" not in await names())
        check("refused director wrote no playbook", not Path(plain, ".claude").exists())

        r = await spawn("helper", "agent", plain)
        check("agent template in the same folder is accepted", r.status_code == 200, r.text[:200])

        r = await c.post("/api/git/setup", json={"cwd": plain})
        check("setup returns ok", r.status_code == 200 and r.json().get("ok"), r.text[:200])
        check("setup made exactly one commit", len(git(plain, "log", "--oneline").splitlines()) == 1,
              git(plain, "log", "--oneline"))
        files = git(plain, "ls-files").splitlines()
        check("commit holds the folder's files", "notes.txt" in files, str(files))
        check("commit respects .gitignore", "secret.log" not in files, str(files))
        check("no remote added", git(plain, "remote") == "")

        head = git(plain, "rev-parse", "HEAD")
        r = await c.post("/api/git/setup", json={"cwd": plain})
        check("second setup changes nothing",
              r.status_code == 200 and not r.json().get("created") and git(plain, "rev-parse", "HEAD") == head,
              r.text[:200])

        r = await spawn("boss", "director", plain)
        check("director is accepted after setup", r.status_code == 200, r.text[:200])

        r = await c.post("/api/git/setup", json={"cwd": HOME})
        check("home folder is refused", r.status_code == 400, r.text[:200])
        check("no .git in the home folder", not Path(HOME, ".git").exists())
        r = await c.post("/api/git/setup", json={"cwd": str(Path(HOME, "nope"))})
        check("missing folder is refused", r.status_code == 400, r.text[:200])

        empty = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", empty], check=True)
        r = await c.post("/api/git/setup", json={"cwd": empty})
        check("repo without commits gets its first commit",
              r.status_code == 200 and len(git(empty, "log", "--oneline").splitlines()) == 1, r.text[:200])


asyncio.run(main())
print()
print(f"{len(fails)} failed" if fails else "all good")
sys.exit(1 if fails else 0)
