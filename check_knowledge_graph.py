#!/usr/bin/env python3
"""Guards for GET /api/knowledge — the Knowledge tab's graph of agents, memories and decisions.

What can go wrong quietly:
  - another workspace's agents, memories or decisions showing up in this one;
  - a sub-agent drawn as an agent, or a signal to one drawn as a link to nowhere;
  - a [[name]] that matches no memory turning into a link;
  - two signals between the same pair drawn as two links instead of one with count 2.

Runs against a temp HOME (so the DB is throwaway) and a temp CLAUDE_CONFIG_DIR — never the real ~/.claude.

    python3 check_knowledge_graph.py
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TMP = Path(tempfile.mkdtemp(prefix="check_knowledge_graph_"))
os.environ.update({"HOME": str(TMP), "USERPROFILE": str(TMP), "ORCH_DB": "check_knowledge_graph",
                   "ORCH_WORKSPACES_ROOT": str(TMP / "ws"), "ORCH_DRY_RUN": "1",
                   "CLAUDE_CONFIG_DIR": str(TMP / "claude"), "CLAUDE_BIN": sys.executable,
                   "ORCH_AGY_BIN": sys.executable})
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx                        # noqa: E402

import session_orchestrator as so   # noqa: E402
import team                         # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)
fails = []


def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    print(("FAIL " if not ok else "ok   ") + name + (f" — {detail}" if not ok else ""))


assert str(so.DB_DIR).startswith(str(TMP)), so.DB_DIR
assert str(so.CLAUDE_PROJECTS_DIR).startswith(str(TMP)), so.CLAUDE_PROJECTS_DIR
so.init_db()


def memory(cwd, stem, body=""):
    folder = so.CLAUDE_PROJECTS_DIR / "".join(c if c.isascii() and c.isalnum() else "-" for c in str(cwd)) / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{stem}.md").write_text(f"---\nname: {stem}\ndescription: about {stem}\n---\n\n{body}\n",
                                       encoding="utf-8")
    return "memory:" + str(folder / f"{stem}.md")


def signal(ws, frm, to):
    team._exec("INSERT INTO signals (workspace_id, from_session, to_session, message, created_at) "
               "VALUES (?, ?, ?, 'hi', '2026-01-01')", (ws, frm, to))


W, W2 = "ws_w", "ws_w2"
AB, C, OTHER = TMP / "proj_ab", TMP / "proj_c", TMP / "proj_other"
so.register_session("s-a", "A", cwd=str(AB), workspace_id=W)
so.register_session("s-b", "B", cwd=str(AB), workspace_id=W)
so.register_session("s-c", "C", cwd=str(C), workspace_id=W)
so.register_session("s-s", "S", cwd=str(AB), workspace_id=W)
team._set_team("s-s", "s-a", "subagent")
so.register_session("s-x", "X", cwd=str(OTHER), workspace_id=W2)

m1 = memory(AB, "m1", "See [[m2]] and [[missing]].")
m2 = memory(AB, "m2")
m3 = memory(AB, "m3")
memory(OTHER, "m1", "same filename, other workspace")
team.record_decision("A", "Use the flag", "rollback is one toggle", "", W)
team.record_decision("X", "Other tenant's call", "", "", W2)
signal(W, "s-a", "s-b")
signal(W, "s-a", "s-b")
signal(W, "s-a", "s-s")
signal(W2, "s-x", "s-x")


async def main():
    app = so.build_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/api/knowledge", params={"workspace_id": W})
        check("GET /api/knowledge answers 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        if r.status_code != 200:
            return
        g = r.json()
        for k in ("post", "put", "delete"):
            rr = await getattr(c, k)("/api/knowledge", params={"workspace_id": W})
            check(f"{k.upper()} is not accepted (read-only)", rr.status_code == 405, rr.status_code)
    nodes = {n["id"]: n for n in g["nodes"]}
    print("nodes:", sorted(nodes))
    check("every node has id, kind, label, detail",
          all(set(n) == {"id", "kind", "label", "detail"} for n in g["nodes"]), g["nodes"])
    check("node ids are unique", len(nodes) == len(g["nodes"]), g["nodes"])
    agents = {n["label"] for n in g["nodes"] if n["kind"] == "agent"}
    check("agents are exactly A, B, C (no sub-agent S, no W2 agent)", agents == {"A", "B", "C"}, agents)
    check("memories are exactly m1, m2, m3 of W",
          {i for i, n in nodes.items() if n["kind"] == "memory"} == {m1, m2, m3}, sorted(nodes))
    decs = [n for n in g["nodes"] if n["kind"] == "decision"]
    check("one decision, A's", [d["label"] for d in decs] == ["Use the flag"], decs)
    check("decision detail is its reason", decs and decs[0]["detail"] == "rollback is one toggle", decs)
    check("memory detail is its description", nodes.get(m1, {}).get("detail") == "about m1", nodes.get(m1))
    check("agent detail is its cwd", nodes.get("agent:s-a", {}).get("detail") == str(AB), nodes.get("agent:s-a"))
    check("details stay short", all(len(n["detail"]) <= 200 for n in g["nodes"]))

    links = sorted((lk["kind"], lk["source"], lk["target"], lk["count"]) for lk in g["links"])
    print("links:", links)
    want = sorted([("owns", f"agent:s-{a}", m, 1) for a in "ab" for m in (m1, m2, m3)]
                  + [("links", m1, m2, 1),
                     ("recorded", "agent:s-a", decs[0]["id"] if decs else "?", 1),
                     ("signals", "agent:s-a", "agent:s-b", 2)])
    check("links are exactly owns ×6, m1→m2, A→decision, A→B signals ×2", links == want,
          f"\n  got  {links}\n  want {want}")
    check("every link end is a node", all(lk["source"] in nodes and lk["target"] in nodes for lk in g["links"]))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        e = (await c.get("/api/knowledge", params={"workspace_id": "ws_empty"})).json()
    check("an empty workspace gives empty lists", e == {"nodes": [], "links": []}, e)


asyncio.run(main())
shutil.rmtree(TMP, ignore_errors=True)

if fails:
    print("\n" + "\n".join(fails))
    sys.exit(1)
print("\nall knowledge graph checks passed")
