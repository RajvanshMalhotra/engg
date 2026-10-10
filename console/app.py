"""Operator console: start runs, watch them live, answer approvals, inspect verdicts and evidence.

    uv run uvicorn console.app:app --port 8766

Runs execute as separate processes (`opsagent run ... --approvals console`), so the console is just a view over
`runs/<id>/` plus the file-based approval channel — the same artifacts the CLI produces.
"""

import json
import secrets
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from opsagent import config

app = FastAPI(title="Operator console")
STATIC = Path(__file__).parent / "static"
PROCS: dict[str, subprocess.Popen] = {}
FAULTS = ["flaky_submit", "layout_b", "toasts", "session_expiry", "iframe_payments", "shadow_forms", "confirm_dialogs"]


def _run_dir(run_id: str) -> Path:
    path = (config.RUNS_DIR / run_id).resolve()
    if not path.is_relative_to(config.RUNS_DIR.resolve()) or not path.is_dir():
        raise HTTPException(404, "No such run")
    return path


def _events(path: Path) -> list[dict]:
    f = path / "events.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # a line being written right now
    return out


def _status(run_id: str, events: list[dict]) -> str:
    done = next((e for e in reversed(events) if e["kind"] == "complete"), None)
    if done:
        return done["status"]
    proc = PROCS.get(run_id)
    if proc and proc.poll() is None:
        return "running"
    log = config.RUNS_DIR / run_id / "events.jsonl"
    if log.exists() and datetime.now().timestamp() - log.stat().st_mtime < 180:
        return "running"  # started outside the console (CLI / evals) and still writing
    return "interrupted" if events else "starting"


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text()


@app.get("/api/runs")
def list_runs():
    rows = []
    for d in sorted(config.RUNS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        ev = _events(d)
        if not ev or not ev[0].get("request"):
            continue
        done = next((e for e in reversed(ev) if e["kind"] == "complete"), {})
        cost = done.get("cost_usd") or next((e.get("cost_usd") for e in reversed(ev) if "cost_usd" in e), 0)
        rows.append({"id": d.name, "request": ev[0].get("request", ""), "started": ev[0]["at"],
                     "status": _status(d.name, ev), "cost_usd": cost or 0, "steps": done.get("steps")})
    return rows[:60]


class NewRun(BaseModel):
    request: str
    faults: list[str] = []
    reset: bool = True
    memory: bool = True


@app.post("/api/runs")
def start_run(body: NewRun):
    if not body.request.strip():
        raise HTTPException(422, "Write a request first")
    if body.reset:
        httpx.post(f"{config.SANDBOX_URL}/__admin/reset", timeout=30).raise_for_status()
    httpx.post(f"{config.SANDBOX_URL}/__admin/faults", json={"faults": [f for f in body.faults if f in FAULTS]},
               timeout=10)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    run_path = config.RUNS_DIR / run_id
    run_path.mkdir(parents=True)
    cmd = [sys.executable, "-m", "opsagent.cli", "run", body.request.strip(), "--approvals", "console",
           "--run-id", run_id] + ([] if body.memory else ["--no-memory"])
    log = open(run_path / "stdout.log", "w")
    PROCS[run_id] = subprocess.Popen(cmd, cwd=config.ROOT, stdout=log, stderr=subprocess.STDOUT)
    return {"id": run_id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    path = _run_dir(run_id)
    events = _events(path)
    cp = {}
    if (path / "checkpoint.json").exists():
        try:
            cp = json.loads((path / "checkpoint.json").read_text())
        except json.JSONDecodeError:
            pass
    pending = []
    for req in sorted((path / "approvals").glob("*.request.json")):
        rid = req.name.split(".")[0]
        if not (path / "approvals" / f"{rid}.decision.json").exists():
            pending.append(json.loads(req.read_text()))
    contract = cp.get("contract") or next((e["contract"] for e in events if e["kind"] == "contract"), None)
    return {
        "id": run_id, "status": _status(run_id, events), "events": events, "contract": contract,
        "notes": cp.get("notes", {}), "pending": pending,
        "screens": sorted(p.name for p in (path / "screens").glob("*.png")),
        "downloads": sorted(p.name for p in (path / "downloads").iterdir()) if (path / "downloads").exists() else [],
        "report": (path / "report.md").read_text() if (path / "report.md").exists() else "",
    }


class Decision(BaseModel):
    approved: bool
    text: str = ""


@app.post("/api/runs/{run_id}/approvals/{rid}")
def decide(run_id: str, rid: str, body: Decision):
    path = _run_dir(run_id) / "approvals"
    if not (path / f"{rid}.request.json").exists():
        raise HTTPException(404, "No such request")
    (path / f"{rid}.decision.json").write_text(json.dumps({"approved": body.approved, "text": body.text,
                                                           "by": "console reviewer"}))
    return {"ok": True}


@app.get("/files/{run_id}/{kind}/{name}")
def run_file(run_id: str, kind: str, name: str):
    if kind not in ("screens", "downloads"):
        raise HTTPException(404, "No such file")
    path = (_run_dir(run_id) / kind / Path(name).name)
    if not path.exists():
        raise HTTPException(404, "No such file")
    return FileResponse(path)
