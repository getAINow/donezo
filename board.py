"""Board: the backlog of a director (any agent card that is not a team session).

The board only HOLDS work. Work is done by the director's sub-agents (team.spawn_subagent); a card
linked to one follows that task's status automatically (on_subtask, called from team._update).
Like team.py, this module never imports session_orchestrator at module level.
"""

import team

STATUSES = ("proposed", "todo", "in_progress", "done", "dropped")
FINAL = ("done", "dropped")      # set by hand, these win over any later automatic change
NOTE_MAX = 200


def migrate(conn):
    """Called from init_db. Idempotent. Not 'boards'/'board_tasks': _drop_boards drops those."""
    # Cards are kept when their owner is unregistered; the Board tab just lists live agents.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS board_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            owner_id TEXT NOT NULL,                   -- the agent card (session id) whose backlog it is
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'todo',      -- one of STATUSES
            source TEXT NOT NULL DEFAULT 'user',      -- user | agent | planner
            subtask_id TEXT NOT NULL DEFAULT '',      -- the sub-agent task working on it
            note TEXT NOT NULL DEFAULT '',            -- one line about the last automatic change
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_board_cards_owner ON board_cards(workspace_id, owner_id);
        CREATE INDEX IF NOT EXISTS idx_board_cards_task ON board_cards(subtask_id);
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(board_cards)")}
    for col in ("url", "decision", "decision_reason"):    # planner proposals
        if col not in cols:
            conn.execute(f"ALTER TABLE board_cards ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _publish(card):
    team._so().publish({"type": "board", "card": card["id"], "owner_id": card["owner_id"],
                        "workspace_id": card["workspace_id"]})


def get(ws, card_id):
    return team._row("SELECT * FROM board_cards WHERE id = ? AND workspace_id = ?", (int(card_id), ws))


def list_cards(ws, owner_id, status=""):
    """Newest first, each with the linked task's live status (task_status, '' if none)."""
    q = ("SELECT c.*, COALESCE(s.status, '') task_status FROM board_cards c "
         "LEFT JOIN subtasks s ON s.id = c.subtask_id AND c.subtask_id != '' "
         "WHERE c.workspace_id = ? AND c.owner_id = ?")
    args = [ws, owner_id]
    if status:
        q += " AND c.status = ?"
        args.append(status)
    return team._rows(q + " ORDER BY c.id DESC", args)


def open_counts(ws):
    """{owner_id: cards still to do or in progress} for the agent picker's default."""
    return {r["owner_id"]: r["n"] for r in team._rows(
        "SELECT owner_id, COUNT(*) n FROM board_cards WHERE workspace_id = ? "
        "AND status IN ('todo', 'in_progress') GROUP BY owner_id", (ws,))}


def proposed_counts(ws=""):
    """Waiting proposals of live agents: {owner_id: n} in ws, or {workspace_id: n} with no ws."""
    key = "c.owner_id" if ws else "c.workspace_id"
    return {r["k"]: r["n"] for r in team._rows(
        f"SELECT {key} k, COUNT(*) n FROM board_cards c JOIN sessions s ON s.id = c.owner_id "
        "WHERE c.status = 'proposed' AND (? = '' OR c.workspace_id = ?) GROUP BY k", (ws, ws))}


def add(ws, owner_id, title, body="", source="user"):
    so = team._so()
    owner = so.get_session(owner_id)
    if not owner or (owner.get("workspace_id") or so.DEFAULT_WORKSPACE) != ws:
        return {"error": "no such agent in this workspace"}
    if team.is_team_session(owner):
        return {"error": "sub-agents and verifiers have no board"}
    title = str(title or "").strip()
    if not title:
        return {"error": "title is required"}
    now = so._now()
    cid = team._exec("INSERT INTO board_cards (workspace_id, owner_id, title, body, source, created_at, "
                     "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (ws, owner_id, title[:200], str(body or ""), source, now, now))
    card = get(ws, cid)
    _publish(card)
    return card


def propose(ws, owner_id, title, body="", url=""):
    """A planner's idea: a card in 'proposed' that waits for the user's decision."""
    url = str(url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        return {"error": "url must start with http:// or https://"}
    card = add(ws, owner_id, title, body, source="planner")
    if card.get("error"):
        return card
    team._exec("UPDATE board_cards SET status = 'proposed', url = ? WHERE id = ?", (url[:500], card["id"]))
    card = get(ws, card["id"])
    _publish(card)
    return card


def decide(ws, card_id, approve, reason=""):
    """The user's answer to a proposal: approved → todo, rejected → dropped. Only the HTTP route
    calls this; no MCP tool does, so no agent can approve its own idea."""
    card = get(ws, card_id)
    if not card:
        return {"error": "not found"}
    if card["status"] != "proposed":
        return {"error": f"card {card['id']} is {card['status']}, not a proposal"}
    reason = " ".join(str(reason or "").split())[:1000]
    verdict = "approved" if approve else "rejected"
    team._exec("UPDATE board_cards SET status = ?, decision = ?, decision_reason = ?, note = ?, updated_at = ? "
               "WHERE id = ?", ("todo" if approve else "dropped", verdict, reason,
                                (f"{verdict}: {reason}" if reason else verdict)[:NOTE_MAX], team._so()._now(),
                                card["id"]))
    card = get(ws, card["id"])
    _publish(card)
    return card


def update(ws, card_id, owner_id="", status=None, title=None, body=None, note=None, subtask_id=None):
    """owner_id set = only that agent's card. None/'' leaves a field as it is (body: None only)."""
    card = get(ws, card_id)
    if not card or (owner_id and card["owner_id"] != owner_id):
        return {"error": "not found"}
    fields = {}
    if status:
        if status not in STATUSES:
            return {"error": f"status must be one of: {', '.join(STATUSES)}"}
        if "proposed" in (status, card["status"]):
            return {"error": "only the user approves or rejects a proposal"}
        fields["status"] = status
    if title is not None and str(title).strip():
        fields["title"] = str(title).strip()[:200]
    if body is not None:
        fields["body"] = str(body)
    if note is not None:
        fields["note"] = " ".join(str(note).split())[:NOTE_MAX]
    if subtask_id is not None:
        fields["subtask_id"] = subtask_id
    if not fields:
        return card
    fields["updated_at"] = team._so()._now()
    team._exec(f"UPDATE board_cards SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
               (*fields.values(), card["id"]))
    card = get(ws, card["id"])
    _publish(card)
    return card


def delete(ws, card_id):
    card = get(ws, card_id)
    if not card:
        return {"error": "not found"}
    team._exec("DELETE FROM board_cards WHERE id = ?", (card["id"],))
    _publish(card)
    return {"ok": True}


def check_link(ws, card_id, director_id):
    """spawn_subagent(card_id=…): '' if the director may start work on it, else why not."""
    card = get(ws, card_id)
    if not card or card["owner_id"] != director_id:
        return f"card {card_id} is not on your board"
    if card["status"] == "proposed":
        return f"card {card_id} is a proposal waiting for the user's decision"
    if card["status"] in FINAL:
        return f"card {card_id} is {card['status']}: move it back to todo first"
    return ""


def _auto(st):
    """(status, note) a card linked to subtask st should have."""
    sid, s = st["id"], st["status"]
    if st.get("cleaned"):
        return "done", f"{sid} merged"
    if s in team.ACTIVE:
        return "in_progress", f"{sid} is working on it"
    if s in ("failed", "cancelled"):
        return "todo", f"{sid} {s}: {st.get('reason') or s}"
    if st.get("discarded"):
        return "todo", f"{sid} passed, but its work was discarded"
    if not st.get("branch"):
        return "done", f"{sid} passed"
    return "in_progress", f"{sid} verified, waiting for merge"


def on_subtask(st):
    """team._update calls this after every write to a subtask: spawn, finish, cancel, discard and
    sweep_merged's cleanup all pass through there."""
    card = team._row("SELECT * FROM board_cards WHERE subtask_id = ?", (st["id"],))
    if not card or card["status"] in FINAL:
        return
    status, note = _auto(st)
    if (card["status"], card["note"]) != (status, " ".join(note.split())[:NOTE_MAX]):
        update(card["workspace_id"], card["id"], status=status, note=note)


# ─── HTTP routes (team.routes adds them) ─────────────────────────────────────

def routes(body, reply, ws_of):
    import asyncio                                   # noqa: PLC0415
    from starlette.responses import JSONResponse     # noqa: PLC0415
    from starlette.routing import Route              # noqa: PLC0415

    async def api_list(request):
        ws, q = ws_of(request), request.query_params
        return JSONResponse({"cards": await asyncio.to_thread(list_cards, ws, q.get("owner_id", ""),
                                                              q.get("status", "")),
                             "open": await asyncio.to_thread(open_counts, ws),
                             "proposed": await asyncio.to_thread(proposed_counts, ws)})

    async def api_proposed(request):
        return JSONResponse(await asyncio.to_thread(proposed_counts))

    async def api_decide(request):
        b = await body(request)
        if not isinstance(b.get("approve"), bool):
            return reply({"error": "approve must be true or false"})
        return reply(decide(ws_of(request, b), request.path_params["cid"], b["approve"], b.get("reason", "")))

    async def api_add(request):
        b = await body(request)
        return reply(add(ws_of(request, b), b.get("owner_id", ""), b.get("title", ""),
                         b.get("body", ""), "user"), created=True)

    async def api_card(request):
        cid = request.path_params["cid"]
        if request.method == "DELETE":
            return reply(delete(ws_of(request), cid))
        b = await body(request)
        return reply(update(ws_of(request, b), cid, status=b.get("status") or None,
                            title=b.get("title"), body=b.get("body")))

    return [Route("/api/board", api_list),
            Route("/api/board", api_add, methods=["POST"]),
            Route("/api/board/proposed", api_proposed),
            Route("/api/board/{cid:int}", api_card, methods=["POST", "DELETE"]),
            Route("/api/board/{cid:int}/decide", api_decide, methods=["POST"])]
