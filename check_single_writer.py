#!/usr/bin/env python3
"""One writer per agent transcript: an open terminal OR a background run, never both.

A real server, a FAKE claude in a REAL PTY. The fake behaves like Claude Code where it matters:
it reads raw keys, understands bracketed paste, runs the hook commands from the --settings file
Donezo wrote (through the real shell, against the real server), and in `-p` mode prints
stream-json. Every writer it becomes logs start/end with timestamps to one shared file, which
is what the cases below judge. team.after_run is wrapped by the launcher to log what it gets.

    python check_single_writer.py                 # this branch's server
    SW_CODE=/tmp/master python check_single_writer.py   # someone else's (e.g. master's) server

Throwaway HOME, DB, Claude config and a free port; never 8992, never a real claude.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
CODE = Path(os.environ.get("SW_CODE") or HERE).resolve()
NT = os.name == "nt"
TMP = Path(tempfile.mkdtemp(prefix="check_single_writer_"))
with socket.socket() as _sock:
    _sock.bind(("127.0.0.1", 0))
    PORT = _sock.getsockname()[1]
assert PORT != 8992
LOG = TMP / "writers.jsonl"
FAKE = TMP / "fake_claude.py"
for d in ("tmux", "claude-config", "work"):
    (TMP / d).mkdir()
BIN = TMP / ("fake_claude.cmd" if NT else "fake_claude")
ENV = {"HOME": str(TMP), "USERPROFILE": str(TMP), "ORCH_PORT": str(PORT), "ORCH_HOST": "127.0.0.1",
       "ORCH_DB": "check_single_writer", "ORCH_DRY_RUN": "0", "ORCH_POLL_INTERVAL": "1",
       "ORCH_TYPING_QUIET": "1.5", "ORCH_WORKSPACES_ROOT": str(TMP / "ws"),
       "TMUX_TMPDIR": str(TMP / "tmux"), "CLAUDE_CONFIG_DIR": str(TMP / "claude-config"),
       "CLAUDE_BIN": str(BIN), "AGY_BIN": str(TMP / "no-agy"), "FAKE_LOG": str(LOG)}
os.environ.update(ENV)
sys.path.insert(0, str(CODE))

import httpx                          # noqa: E402
import session_orchestrator as so     # noqa: E402

assert so.DB_DIR.is_relative_to(TMP), so.DB_DIR    # never the real ~/.session_orch_db
so._ensure_db()

FAKE.write_text(r'''import codecs, json, os, re, signal, subprocess, sys, time
LOG = os.environ["FAKE_LOG"]
args = sys.argv[1:]
sid = next((args[i + 1] for i, a in enumerate(args[:-1]) if a in ("--resume", "--session-id")), "")


def log(**kw):
    kw.update(t=time.time(), pid=os.getpid(), session=sid)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(kw) + "\n")


def secs(text):
    m = re.search(r"work=([0-9.]+)", text)
    return float(m.group(1)) if m else 0.3


if "-p" in args:                                   # background run, stream-json like claude -p
    prompt = sys.stdin.read()
    log(who="run", ev="start", prompt=prompt)
    time.sleep(secs(prompt))
    log(who="run", ev="end")
    for ev in ({"type": "system", "subtype": "init", "model": "fake", "tools": [], "session_id": sid},
               {"type": "assistant", "message": {"content": [{"type": "text", "text": "bg reply"}]}},
               {"type": "result", "subtype": "success", "is_error": False, "result": "bg reply",
                "num_turns": 1, "session_id": sid, "usage": {"output_tokens": 1}}):
        print(json.dumps(ev), flush=True)
    sys.exit(0)

import tty
hooks = {}
if "--settings" in args:
    hooks = json.load(open(args[args.index("--settings") + 1], encoding="utf-8")).get("hooks", {})


def hook(ev, **kw):
    """Exactly what Claude Code does: each command through the shell, the event JSON on stdin."""
    for group in hooks.get(ev, []):
        for h in group.get("hooks", []):
            subprocess.run(h["command"], shell=True, capture_output=True, timeout=10,
                           input=json.dumps({"session_id": sid, "hook_event_name": ev, **kw}).encode())


def bye(*_):
    log(who="term", ev="exit")
    os._exit(0)


signal.signal(signal.SIGTERM, bye)
signal.signal(signal.SIGHUP, bye)
tty.setraw(0)
log(who="term", ev="open")
hook("SessionStart", source="resume")
log(who="term", ev="ready")


def submit(text):
    if text.startswith("/compact"):                # a slash command: no UserPromptSubmit / Stop
        log(who="term", ev="start", prompt=text)
        hook("PreCompact", trigger="manual")
        time.sleep(0.3)
        log(who="term", ev="end")
        hook("SessionStart", source="compact")
        return
    log(who="term", ev="start", prompt=text)
    hook("UserPromptSubmit", prompt=text)
    time.sleep(secs(text))
    log(who="term", ev="end")
    hook("Stop", last_assistant_message="reply to: " + text[-30:])


dec = codecs.getincrementaldecoder("utf-8")()
buf, paste, raw = "", False, ""
while True:
    data = os.read(0, 65536)
    if not data:
        bye()
    raw += dec.decode(data)
    while raw:
        if raw.startswith("\x1b[200~"):
            paste, raw = True, raw[6:]
            log(who="term", ev="paste_start", draft=buf)
        elif raw.startswith("\x1b[201~"):
            paste, raw = False, raw[6:]
        elif raw[0] == "\x1b" and len(raw) < 6:
            break                                  # an escape split across reads
        else:
            ch, raw = raw[0], raw[1:]
            if paste:
                buf += "\n" if ch == "\r" else ch
            elif ch == "\r":
                if buf:
                    submit(buf)
                buf = ""
            elif ch == "\x7f":
                buf = buf[:-1]
            elif ch >= " ":
                buf += ch
''', encoding="utf-8")
if NT:
    BIN.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
else:
    BIN.write_text(f"#!{sys.executable}\n" + FAKE.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(BIN, 0o755)

# The launcher wraps team.after_run so the check sees what the team flow is handed.
LAUNCH = TMP / "launch.py"
LAUNCH.write_text(f'''import json, os, sys, time
sys.path.insert(0, {str(CODE)!r})
import session_orchestrator as so, team
_orig = team.after_run
async def _rec(sig, r):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({{"who": "after_run", "t": time.time(), "signal": sig["id"],
                            "session": sig["to_session"], "status": r.get("status"),
                            "result": r.get("result")}}) + "\\n")
    await _orig(sig, r)
team.after_run = _rec
sys.argv = ["session_orchestrator", "serve"]
so.main()
''', encoding="utf-8")

WS = so.DEFAULT_WORKSPACE
CARDS = ["sw-one", "sw-two", "sw-three", "sw-five", "sw-six", "sw-seven"]
for c in CARDS:
    so.register_session(c, c, cwd=str(TMP / "work"), model="", workspace_id=WS)

results = {}   # case → list of failures


def case(n, name, ok, detail=""):
    results.setdefault(n, []).extend([] if ok else [f"{name}: {detail}"])
    print(("ok   " if ok else "FAIL ") + f"[{n}] {name}" + ("" if ok else f" — {detail}"), flush=True)


def events(**match):
    if not LOG.exists():
        return []
    out = [json.loads(ln) for ln in LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [e for e in out if all(e.get(k) == v for k, v in match.items())]


async def until(pred, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(0.1)
    return pred()


BASE = f"http://127.0.0.1:{PORT}"


def send(card, message):
    r = httpx.post(BASE + "/api/signals", json={"to_role": card, "message": message,
                                                 "from_role": "human", "workspace_id": WS})
    r.raise_for_status()
    return r.json()["id"]


def sig_status(sig_id):
    conn = so._conn()
    row = conn.execute("SELECT status FROM signals WHERE id = ?", (sig_id,)).fetchone()
    conn.close()
    return row["status"] if row else None


class Term:
    """The dashboard's side of /ws/terminal: same JSON frames xterm.js sends."""

    async def open(self, card, retry=False):
        import websockets
        while True:
            self.ws = await websockets.connect(f"ws://127.0.0.1:{PORT}/ws/terminal?session={card}")
            self.code = None
            try:
                await asyncio.wait_for(self.ws.recv(), 1.0)
            except asyncio.TimeoutError:
                pass
            except websockets.ConnectionClosed as e:
                self.code, self.reason = e.rcvd.code if e.rcvd else None, e.rcvd.reason if e.rcvd else ""
            if self.code is None or not retry:
                break
            await asyncio.sleep(0.5)                # what the card does on 4409: wait, reconnect
        if self.code is None:
            self.drain = asyncio.create_task(self._drain())
        return self

    async def _drain(self):
        try:
            async for _ in self.ws:
                pass
        except Exception:  # noqa: BLE001
            pass

    async def type(self, text):
        for ch in text:                             # one key per frame, like a person
            await self.ws.send(json.dumps({"t": "i", "d": ch}))
            await asyncio.sleep(0.02)

    async def close(self):
        await self.ws.close()


def writer_intervals(card):
    """Every writer of this card: terminal processes (open → exit) and background runs."""
    out, opened = [], {}
    for e in events(session=card):
        key, step = (e["who"], e.get("pid")), (e["who"], e.get("ev"))
        if step in (("run", "start"), ("term", "open")):
            opened[key] = e["t"]
        elif step in (("run", "end"), ("term", "exit")) and key in opened:
            out.append((e["who"], opened.pop(key), e["t"]))
    out += [(who, t0, float("inf")) for (who, _), t0 in opened.items()]
    return sorted(out, key=lambda x: x[1])


def overlaps(card):
    iv = writer_intervals(card)
    return [(a, b) for i, a in enumerate(iv) for b in iv[i + 1:] if b[1] < a[2]]


async def main():
    # 1 · a signal during a terminal turn waits, then is typed in after the turn ends
    t = await Term().open("sw-one")
    await until(lambda: events(session="sw-one", ev="ready"))
    await t.type("work=3 user turn\r")
    await until(lambda: events(session="sw-one", who="term", ev="start"))
    s1 = send("sw-one", "case one report")
    await until(lambda: sig_status(s1) == "done", 25)
    starts = events(session="sw-one", who="term", ev="start")
    ends = events(session="sw-one", who="term", ev="end")
    case(1, "the user's turn ran its full 3 s, uninterrupted",
         len(ends) >= 1 and ends[0]["t"] - starts[0]["t"] >= 2.9
         and ends[0]["pid"] == starts[0]["pid"] and not events(session="sw-one", ev="exit"),
         f"starts={len(starts)} ends={len(ends)}")
    case(1, "the signal was typed into the terminal after that turn ended",
         len(starts) == 2 and "case one report" in starts[1]["prompt"]
         and starts[1]["t"] >= ends[0]["t"] and not events(session="sw-one", who="run"),
         [(e["who"], e.get("ev"), (e.get("prompt") or "")[-30:]) for e in events(session="sw-one")])
    ar = events(who="after_run", signal=s1)
    case(1, "team.after_run got the turn's result", ar and ar[0]["status"] == "done"
         and ar[0]["result"].startswith("reply to: "), ar)
    await t.close()

    # 2 · two signals (and a /compact) are delivered in order, one at a time
    t = await Term().open("sw-two")
    await until(lambda: events(session="sw-two", ev="ready"))
    ids = [send("sw-two", "case two first work=1.5"), send("sw-two", "case two second"),
           send("sw-two", "/compact")]
    await until(lambda: all(sig_status(i) == "done" for i in ids), 30)
    starts = events(session="sw-two", who="term", ev="start")
    ends = events(session="sw-two", who="term", ev="end")
    order = [("first" in e["prompt"]) * 1 + ("second" in e["prompt"]) * 2 + (e["prompt"] == "/compact") * 3
             for e in starts]
    case(2, "all three were typed into the terminal, in signal order", order == [1, 2, 3]
         and not events(session="sw-two", who="run"), [e.get("prompt", "")[-40:] for e in starts])
    case(2, "each started only after the previous turn ended",
         len(ends) == 3 and all(starts[i + 1]["t"] >= ends[i]["t"] for i in range(2)),
         [(round(s["t"], 2), round(e["t"], 2)) for s, e in zip(starts, ends)])
    case(2, "every one ended 'done' and reached team.after_run",
         [sig_status(i) for i in ids] == ["done"] * 3
         and all(events(who="after_run", signal=i, status="done") for i in ids),
         [sig_status(i) for i in ids])
    await t.close()

    # 3 · opening a terminal during a background run waits, then opens after it ends
    s3 = send("sw-three", "case three work=4")
    await until(lambda: events(session="sw-three", who="run", ev="start"))
    first = await Term().open("sw-three")
    case(3, "the terminal is refused while the run holds the transcript",
         first.code == 4409 and "working" in (first.reason or ""), (first.code, getattr(first, "reason", "")))
    if first.code is None:
        await first.close()
    t = await Term().open("sw-three", retry=True)
    await until(lambda: events(session="sw-three", ev="open"))
    run_end = events(session="sw-three", who="run", ev="end")
    term_open = events(session="sw-three", who="term", ev="open")
    case(3, "it opened by itself once the run ended",
         t.code is None and run_end and term_open and term_open[0]["t"] >= run_end[0]["t"],
         (run_end, term_open))
    await until(lambda: sig_status(s3) == "done")
    await t.close()

    # 5 · a long multi-line report arrives in one piece
    t = await Term().open("sw-five")
    await until(lambda: events(session="sw-five", ev="ready"))
    report = "\n".join(f"line {i:04d} of the long report — với chữ có dấu, tabs\tand \"quotes\""
                       for i in range(400))
    s5 = send("sw-five", report)
    await until(lambda: sig_status(s5) == "done", 25)
    starts = events(session="sw-five", who="term", ev="start")
    want = so.inject_prompt(so.get_session("sw-five"), report, "human", 1)
    case(5, f"one prompt, byte-for-byte the background prompt ({len(want)} chars)",
         len(starts) == 1 and starts[0]["prompt"] == want,
         f"{len(starts)} prompts, lengths {[len(s['prompt']) for s in starts]} vs {len(want)}")
    await t.close()

    # 6 · a half-typed draft is never mixed with the message
    t = await Term().open("sw-six")
    await until(lambda: events(session="sw-six", ev="ready"))
    await t.type("half draft")
    s6 = send("sw-six", "case six message")
    await asyncio.sleep(4)
    case(6, "nothing is typed while the user's draft sits in the box",
         not events(session="sw-six", ev="paste_start") and sig_status(s6) == "pending"
         and not events(session="sw-six", who="run"), events(session="sw-six"))
    await t.type(" finished\r")
    await until(lambda: sig_status(s6) == "done", 25)
    prompts = [e["prompt"] for e in events(session="sw-six", who="term", ev="start")]
    pastes = events(session="sw-six", ev="paste_start")
    case(6, "the draft went in as typed, then the message alone",
         len(prompts) == 2 and prompts[0] == "half draft finished" and "case six message" in prompts[1]
         and "draft" not in prompts[1] and pastes and pastes[0]["draft"] == "", prompts)
    await t.close()

    # 7 · no terminal open: background runs as before; closing a terminal hands its queue back
    s7 = send("sw-seven", "case seven alone")
    await until(lambda: sig_status(s7) == "done")
    ar = events(who="after_run", signal=s7)
    case(7, "a background run handled it, after_run got its result",
         events(session="sw-seven", who="run", ev="end") and ar and ar[0]["result"] == "bg reply", ar)
    t = await Term().open("sw-seven")
    await until(lambda: events(session="sw-seven", ev="ready"))
    await t.type("draft")
    s7b = send("sw-seven", "case seven queued")
    await asyncio.sleep(2.5)
    queued = {s["id"]: s.get("term_queue") for s in httpx.get(BASE + "/api/sessions").json()}
    case(7, "the card reports 1 message waiting for the open terminal", queued.get("sw-seven") == 1,
         queued.get("sw-seven"))
    await t.close()
    await until(lambda: sig_status(s7b) == "done")
    runs = events(session="sw-seven", who="run", ev="start")
    case(7, "closing the terminal sends the queued signal to a background run",
         len(runs) == 2 and "case seven queued" in runs[1]["prompt"]
         and runs[1]["t"] >= events(session="sw-seven", ev="exit")[0]["t"], runs)

    # 4 · across every case above, no two writers of one card ever overlapped
    for c in CARDS:
        bad = overlaps(c)
        case(4, f"{c}: writers never overlap ({len(writer_intervals(c))} writers)", not bad, bad)


def hooks_through_shells():
    """The hook command in Donezo's settings file, through each shell this OS has, is accepted."""
    argv = so.terminal_launch_argv(so.get_session("sw-one"), "claude")
    f = Path(argv[2]) if argv[1:2] == ["--settings"] else None
    body = json.loads(f.read_text(encoding="utf-8")) if f else {}
    cmd = ((body.get("hooks") or {}).get("Stop") or [{}])[0].get("hooks", [{}])[0].get("command", "")
    case(0, "Donezo's own settings file carries the turn hooks",
         set(body.get("hooks") or {}) >= {"UserPromptSubmit", "Stop"} and cmd, body)
    shells = [["cmd", "/c", cmd], ["powershell", "-NoProfile", "-Command", cmd]] if NT else [["sh", "-c", cmd]]
    for sh in shells if cmd else []:
        r = subprocess.run(sh, input=json.dumps({"session_id": "nope", "hook_event_name": "Stop"}).encode(),
                           capture_output=True, timeout=30)
        case(0, f"{sh[0]}: the hook command runs and prints nothing into claude's context",
             r.returncode == 0 and not r.stdout.strip(), (r.returncode, r.stdout[:200]))


proc = subprocess.Popen([sys.executable, str(LAUNCH)], cwd=str(CODE), env={**os.environ, **ENV},
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    for _ in range(120):
        try:
            httpx.get(BASE + "/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.25)
    else:
        raise AssertionError("server never came up: " + proc.stderr.read1().decode("utf-8", "replace"))
    hooks_through_shells()
    if NT:
        # The fake reads raw keys with tty.setraw; ConPTY turns input into console events instead.
        print("SKIP cases 1-7 on Windows: the fake CLI needs a POSIX raw tty "
              "(the hook command above still ran through cmd and PowerShell)")
    else:
        asyncio.run(main())
finally:
    proc.kill()
    proc.wait()

shutil.rmtree(TMP, ignore_errors=True)
print()
for n in sorted(results):
    label = "hooks" if n == 0 else f"case {n}"
    print(f"{'PASS' if not results[n] else 'FAIL'}  {label}")
if any(results.values()):
    sys.exit(1)
print("\nall checks passed")
