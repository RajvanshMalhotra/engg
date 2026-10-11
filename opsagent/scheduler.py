"""Standing instructions: work the operator starts on its own, on a schedule or when something happens.

A standing instruction is a request plus a trigger:
    every_minutes: 60            recurring
    daily_at: "09:00"            once a day (local time)
    on: "mail.new_message"       an event a company system declares under `events:` in company/systems.yaml

Each firing is an ordinary run (contract, policy, independent verification, report) tagged with its origin, so
unattended work gets exactly the same guarantees as a run a person started. Design rules:
  - A trigger carries no environment content into the request. An email subject could carry a prompt injection
    into the trusted request text; the run reads the inbox itself, where content is wrapped as untrusted data.
  - Runs of one instruction never overlap. A missed tick is not replayed: the next run sees the current state, and
    already-handled work is caught against the system of record (duplicate checks).
  - Approvals in unattended runs go to the console queue; the run waits for the Finance Manager like any other.
  - State is one JSON file updated under a file lock: the console and the scheduler daemon can both edit it.
"""

import fcntl
import json
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from opsagent import config

STATE_DIR = config.ROOT / ".opsagent"
STATE = STATE_DIR / "standing.json"
HEARTBEAT = STATE_DIR / "scheduler.heartbeat"
STALE_RUN_SECONDS = 1800   # a run whose log hasn't moved for this long is treated as finished (crashed)


@dataclass
class Instruction:
    id: str
    request: str
    every_minutes: int | None = None
    daily_at: str | None = None          # "HH:MM"
    on: str | None = None                # "<system>.<event>"
    enabled: bool = True
    created_at: str = ""
    last_fired: str | None = None
    cursor: int | None = None            # last event id seen (event triggers)
    runs: list = field(default_factory=list)

    def trigger(self) -> str:
        if self.on:
            system, event = self.on.split(".", 1)
            return f"when a {event.replace('_', ' ')} arrives ({system})"
        if self.daily_at:
            return f"every day at {self.daily_at}"
        return f"every {self.every_minutes} min"


# ------------------------------------------------------------------ state (file + lock)

@contextmanager
def _locked():
    STATE_DIR.mkdir(exist_ok=True)
    with open(STATE_DIR / "standing.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        items = [Instruction(**d) for d in json.loads(STATE.read_text())] if STATE.exists() else []
        yield items
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(i) for i in items], indent=1))
        tmp.replace(STATE)


def load() -> list[Instruction]:
    with _locked() as items:
        return list(items)


def add(request: str, *, every_minutes: int | None = None, daily_at: str | None = None, on: str | None = None,
        now: datetime | None = None, cursor_fn=None) -> Instruction:
    if sum(x is not None for x in (every_minutes, daily_at, on)) != 1:
        raise ValueError("give exactly one trigger: every_minutes, daily_at or on")
    now = now or datetime.now()
    ins = Instruction(id="si-" + secrets.token_hex(3), request=request.strip(), every_minutes=every_minutes,
                      daily_at=daily_at, on=on, created_at=now.isoformat(timespec="seconds"))
    if on:
        ins.cursor = (cursor_fn or event_cursor)(on)       # only events *after* creation count
    elif every_minutes or (daily_at and now.strftime("%H:%M") >= daily_at):
        ins.last_fired = ins.created_at                    # first firing is the next due time, not now
    with _locked() as items:
        items.append(ins)
    return ins


def update(ins_id: str, **changes) -> Instruction | None:
    with _locked() as items:
        for i in items:
            if i.id == ins_id:
                for k, v in changes.items():
                    setattr(i, k, v)
                return i
    return None


def remove(ins_id: str) -> bool:
    with _locked() as items:
        n = len(items)
        items[:] = [i for i in items if i.id != ins_id]
        return len(items) < n


# ------------------------------------------------------------------ triggers

def event_cursor(name: str) -> int | None:
    """Newest event id a system exposes (see `events:` in systems.yaml); None if the system is unreachable."""
    system, event = name.split(".", 1)
    spec = config.load_systems()[system]["events"][event]
    headers = dict([spec["auth_header"].split(": ", 1)]) if spec.get("auth_header") else {}
    try:
        return int(httpx.get(spec["url"], headers=headers, timeout=5).json()["latest_id"])
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def due(ins: Instruction, now: datetime, cursor: int | None) -> str | None:
    """Why this instruction should fire now, or None. Event triggers may move the cursor (see tick)."""
    if not ins.enabled:
        return None
    if ins.on:
        if cursor is None or ins.cursor is None or cursor <= ins.cursor:
            return None
        n, first = cursor - ins.cursor, ins.cursor + 1
        ids = f"id {cursor}" if n == 1 else f"ids {first}–{cursor}"
        # ids are the system's own numbers, not content, so they're safe to put in the request
        return f"{n} new {ins.on} event{'s' if n > 1 else ''} ({ids}) at {now:%H:%M}"
    last = datetime.fromisoformat(ins.last_fired) if ins.last_fired else None
    if ins.every_minutes:
        if last is None or (now - last).total_seconds() >= ins.every_minutes * 60:
            return f"scheduled every {ins.every_minutes} min, at {now:%H:%M}"
    elif ins.daily_at and now.strftime("%H:%M") >= ins.daily_at and (last is None or last.date() < now.date()):
        return f"scheduled daily at {ins.daily_at}"
    return None


def run_active(run_id: str) -> bool:
    log = config.RUNS_DIR / run_id / "events.jsonl"
    if not log.exists():
        return (config.RUNS_DIR / run_id).exists()   # launched, not started writing yet
    if '"kind": "complete"' in log.read_text():
        return False
    return time.time() - log.stat().st_mtime < STALE_RUN_SECONDS


def launch(ins: Instruction, reason: str) -> str:
    """Start an ordinary run for this instruction, in its own process (approvals go to the console)."""
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    run_dir = config.RUNS_DIR / run_id
    run_dir.mkdir(parents=True)
    request = f"{ins.request}\n\n(Standing instruction {ins.id}, started automatically: {reason}.)"
    cmd = [sys.executable, "-m", "opsagent.cli", "run", request, "--approvals", "console",
           "--run-id", run_id, "--origin", f"standing:{ins.id}"]
    subprocess.Popen(cmd, cwd=config.ROOT, stdout=open(run_dir / "stdout.log", "w"), stderr=subprocess.STDOUT,
                     start_new_session=True)
    return run_id


def tick(now: datetime | None = None, launcher=launch, cursor_fn=event_cursor) -> list[tuple[str, str, str]]:
    """Fire every due instruction once. Returns (instruction id, run id, reason) for each firing."""
    now = now or datetime.now()
    fired = []
    with _locked() as items:
        cursors = {name: cursor_fn(name) for name in {i.on for i in items if i.on and i.enabled}}
        for ins in items:
            cursor = cursors.get(ins.on) if ins.on else None
            if ins.on and cursor is not None and (ins.cursor is None or cursor < ins.cursor):
                ins.cursor = cursor                 # first sight, or the system was reset: sync, don't fire
                continue
            reason = due(ins, now, cursor)
            if not reason:
                continue
            if ins.runs and run_active(ins.runs[-1]):
                continue                            # never overlap; it fires again once the run finishes
            run_id = launcher(ins, reason)
            ins.runs.append(run_id)
            ins.last_fired = now.isoformat(timespec="seconds")
            if ins.on:
                ins.cursor = cursor
            fired.append((ins.id, run_id, reason))
    return fired


# ------------------------------------------------------------------ daemon

def alive() -> bool:
    return HEARTBEAT.exists() and time.time() - HEARTBEAT.stat().st_mtime < 30


def run_forever(interval: float = 5.0, log=lambda m: print(m, flush=True)) -> None:
    if alive():
        raise SystemExit("A scheduler is already running.")
    STATE_DIR.mkdir(exist_ok=True)
    while True:
        HEARTBEAT.touch()
        for ins_id, run_id, reason in tick():
            log(f"{datetime.now():%H:%M:%S} {ins_id} → run {run_id} ({reason})")
        time.sleep(interval)
