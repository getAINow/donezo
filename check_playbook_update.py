#!/usr/bin/env python3
"""Guard for "Update playbook": rewrite an agent's SKILL from the CURRENT role template.

A playbook is copied from templates/roles/<t>/SKILL.md once, when the agent is created, so a
section the template gains later never reaches it. _merge_playbook takes the template's sections
and keeps what the agent wrote: a section it filled (the template's copy has a <PLACEHOLDER>) and
its own sections (headings the template does not have). POST /api/sessions/{id}/playbook/update
previews without writing; with apply it saves SKILL.md.bak beside each copy and writes all roots.

Runs the real app in-process over ASGI, dry-run, HOME pointed at a temp folder.

    python3 check_playbook_update.py
"""
import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HOME = tempfile.mkdtemp()
os.environ["HOME"] = os.environ["USERPROFILE"] = HOME   # before import: DB_DIR = Path.home()/...
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["ORCH_DB"] = "check_playbook_update"
os.environ["ORCH_WORKSPACES_ROOT"] = tempfile.mkdtemp()
os.environ["ORCH_DRY_RUN"] = "1"

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402
import team                           # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
so._ensure_db()

fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


def heads(text):
    return [h for h, _ in so._md_sections(text)[1]]


TEMPLATE = """---
name: <ROLE_NAME>
---

# <ROLE_NAME> — director

Intro v2.

## The project

- **Stack:** <STACK>

## Delegating (MCP `signal`)

- `spawn_subagent(from_role="<ROLE_NAME>")` v2

## Workflows

- `run_workflow(from_role="<ROLE_NAME>")`

## Taking the work

Same.
"""

# What an agent made of the v1 template: filled The project, added its own section.
CURRENT = """---
name: dir
---

# dir — director

Intro v1.

## The project

- **Stack:** Python, filled by the agent

## Delegating (MCP `signal`)

- `spawn_subagent(from_role="dir")` v1

```md
## not a heading, inside a fence
```

## Taking the work

Same.

## Releases and docs

Own text.
"""

# ── 1. merge rules ───────────────────────────────────────────────────────────
merged, s = so._merge_playbook(CURRENT, TEMPLATE, "dir")
check("frontmatter and preamble come from the template, name filled",
      merged.startswith("---\nname: dir\n---\n\n# dir — director\n\nIntro v2.\n"), merged[:80])
check("no <ROLE_NAME> left", "<ROLE_NAME>" not in merged)
check("a section the agent filled is kept byte-for-byte",
      "## The project\n\n- **Stack:** Python, filled by the agent\n\n" in merged and "<STACK>" not in merged)
check("a template section the agent never touched takes the template's text",
      'spawn_subagent(from_role="dir")` v2' in merged and "v1" not in merged)
check("a new template section is added once, name filled",
      merged.count("## Workflows") == 1 and 'run_workflow(from_role="dir")' in merged)
check("order follows the template, own section after its neighbour",
      heads(merged) == ["## The project", "## Delegating (MCP `signal`)", "## Workflows",
                        "## Taking the work", "## Releases and docs"], heads(merged))
check("summary", s == {"updated": ["Delegating (MCP `signal`)"], "added": ["Workflows"],
                       "kept": ["The project", "Releases and docs"],
                       "unchanged": ["Taking the work"]}, s)
check("merging the result again changes nothing",
      so._merge_playbook(merged, TEMPLATE, "dir")[0] == merged)
check("'## ' inside a fenced block does not split a section",
      "## not a heading, inside a fence" not in heads(CURRENT)
      and "## not a heading, inside a fence" not in merged)

# Own section right after the preamble stays first; one whose anchor chain is own stays chained.
cur2 = "# x\n\n## Mine first\n\na\n\n## Taking the work\n\nSame.\n\n## A\n\n1\n\n## B\n\n2\n"
m2, s2 = so._merge_playbook(cur2, TEMPLATE, "x")
check("own sections keep their anchors (preamble, chained)",
      heads(m2) == ["## Mine first", "## The project", "## Delegating (MCP `signal`)",
                    "## Workflows", "## Taking the work", "## A", "## B"], heads(m2))
check("an unfilled placeholder section is added from the template",
      "<STACK>" in m2 and "The project" in s2["added"], s2)

check("template guess: director by title", so._playbook_template_guess(CURRENT) == "director")
check("template guess: director by the Delegating section",
      so._playbook_template_guess("# x\n\n## Delegating (MCP `signal`)\n") == "director")
check("template guess: agent otherwise", so._playbook_template_guess("# x\n\n## Scope\n") == "agent")

# Real files: the director template before ## Workflows existed, filled by an agent.
old = subprocess.run(["git", "show", "v2026.11.11:templates/roles/director/SKILL.md"], cwd=HERE,
                     capture_output=True, text=True, encoding="utf-8").stdout
if old and "## Workflows" not in old:
    real = so._template_skill("director")
    filled = old.replace("<ROLE_NAME>", "demo-dir")
    for ph in ("<STACK>", "<BUILD_TEST_CMD>", "<VERIFY_HOW>"):
        filled = filled.replace(ph, "real text")
    filled = filled.replace("## Talking to the user", "## Releases and docs\n\nOwn.\n\n## Talking to the user")
    rm, rs = so._merge_playbook(filled, real, "demo-dir")
    project = dict(so._md_sections(filled)[1])["## The project"]
    check("real director template: Workflows added once, The project kept, own section kept",
          rm.count("## Workflows") == 1 and project in rm and "<" + "ROLE_NAME>" not in rm
          and "Workflows" in rs["added"] and {"The project", "Releases and docs"} <= set(rs["kept"]),
          rs)
    check("real director template: merge is stable", so._merge_playbook(rm, real, "demo-dir")[0] == rm)
else:
    print("skip real-template merge (tag v2026.11.11 not in this clone)")


# ── 2. the route ─────────────────────────────────────────────────────────────
CWD = tempfile.mkdtemp()


def copies(name):
    return [so._skill_path(CWD, name, r) for r in so.CLI_SKILL_ROOTS]


def snap(name):
    return [(p.read_bytes(), p.stat().st_mtime_ns) for p in copies(name)]


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        sid = str(uuid.uuid4())
        so.register_session(sid, "dir", cwd=CWD, model="claude-opus-5")
        for p in copies("dir"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(CURRENT, encoding="utf-8")
        url = f"/api/sessions/{sid}/playbook/update"
        before = snap("dir")

        r = await c.post(url, json={"template": "director"})
        j = r.json()
        check("preview returns the summary and the guess",
              r.status_code == 200 and j["changed"] and j["template_guess"] == "director"
              and "Workflows" in j["summary"]["added"], r.text[:300])
        check("preview writes nothing", snap("dir") == before
              and not any(p.with_name("SKILL.md.bak").exists() for p in copies("dir")))

        r = await c.post(url, json={"template": "director", "apply": True})
        new = so._merge_playbook(CURRENT, so._template_skill("director"), "dir")[0]
        check("apply writes the merged text to every root",
              r.status_code == 200 and all(p.read_text(encoding="utf-8") == new for p in copies("dir")),
              r.text[:300])
        bak = [p.with_name("SKILL.md.bak") for p in copies("dir")]
        check("apply saves the old bytes as SKILL.md.bak beside each copy",
              all(b.read_text(encoding="utf-8") == CURRENT for b in bak))
        bak_snap = [(b.read_bytes(), b.stat().st_mtime_ns) for b in bak]
        after = snap("dir")
        r = await c.post(url, json={"template": "director", "apply": True})
        check("second apply: changed false, nothing rewritten",
              r.status_code == 200 and r.json()["changed"] is False and snap("dir") == after
              and [(b.read_bytes(), b.stat().st_mtime_ns) for b in bak] == bak_snap, r.text[:200])

        r = await c.post(url, json={"template": "nope"})
        check("unknown template refused", r.status_code == 400 and "unknown template" in r.text, r.text)
        r = await c.post(url, json={"template": "../director"})
        check("traversal template refused", r.status_code == 400, r.text)

        sub = str(uuid.uuid4())
        so.register_session(sub, "dir-sub", cwd=CWD, model="claude-opus-5")
        team._set_team(sub, sid, "sub-agent")
        r = await c.post(f"/api/sessions/{sub}/playbook/update", json={"template": "agent"})
        check("team session refused", r.status_code == 400 and "team" in r.text, r.text)

        blank = str(uuid.uuid4())
        so.register_session(blank, "blank", cwd=CWD, model="claude-opus-5")
        r = await c.post(f"/api/sessions/{blank}/playbook/update",
                         json={"template": "agent", "apply": True})
        check("no playbook refused, nothing created",
              r.status_code == 400 and not any(p.exists() for p in copies("blank")), r.text)

        r = await c.post(f"/api/sessions/{uuid.uuid4()}/playbook/update", json={"template": "agent"})
        check("unknown session is 404", r.status_code == 404, r.text)

        written = sorted(str(p.relative_to(CWD)) for p in Path(CWD).rglob("*") if p.is_file())
        check("only the playbook copies and their backups were written",
              written == sorted(f"{r}/skills/dir/{f}" for r in so.CLI_SKILL_ROOTS
                                for f in ("SKILL.md", "SKILL.md.bak")), written)


asyncio.run(main())
print()
print(f"{len(fails)} failed" if fails else "all good")
sys.exit(1 if fails else 0)
