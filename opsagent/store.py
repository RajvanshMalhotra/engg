"""Durable run state.

Each run is a directory:   runs/<run_id>/
    events.jsonl      append-only audit log (every LLM turn, tool call, policy decision, approval, verdict)
    checkpoint.json   latest resumable state (messages, contract, plan, notes, step) — rewritten each step
    browser.json      Playwright storage state (cookies) so a resumed run keeps its sessions
    downloads/        files the agent fetched
    screens/          screenshots taken as evidence
    approvals/        pending/decided human requests (file-based channel used by the console)
    report.md         final human-readable report

An append-only log + checkpoint is the same shape a production queue/worker system would use:
any worker can pick a run up from its checkpoint.
"""

import json
import secrets
from datetime import datetime
from pathlib import Path

from opsagent import config


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class RunStore:
    def __init__(self, run_id: str | None = None):
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        self.dir: Path = config.RUNS_DIR / self.run_id
        for sub in ("downloads", "screens", "approvals"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)

    def log(self, kind: str, **data) -> None:
        with open(self.dir / "events.jsonl", "a") as f:
            f.write(json.dumps({"at": now(), "kind": kind, **data}, default=str) + "\n")

    def events(self) -> list[dict]:
        path = self.dir / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def checkpoint(self, state: dict) -> None:
        tmp = self.dir / "checkpoint.json.tmp"
        tmp.write_text(json.dumps(state, default=str, indent=1))
        tmp.replace(self.dir / "checkpoint.json")

    def load_checkpoint(self) -> dict | None:
        path = self.dir / "checkpoint.json"
        return json.loads(path.read_text()) if path.exists() else None

    @property
    def browser_state(self) -> Path:
        return self.dir / "browser.json"
