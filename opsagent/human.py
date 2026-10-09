"""Human-in-the-loop channels: approvals (raised by the policy engine) and questions (raised by the agent).

Channels are swappable so the same runtime works in a terminal, from the web console, or unattended
in evals (scripted answers).
"""

import json
import time
import uuid
from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from opsagent.store import RunStore, now


@dataclass
class HumanReply:
    approved: bool
    text: str = ""
    by: str = ""


class HumanChannel:
    def approve(self, title: str, details: dict, approver: str) -> HumanReply:
        raise NotImplementedError

    def ask(self, question: str, options: list[str] | None = None) -> HumanReply:
        raise NotImplementedError


class TerminalChannel(HumanChannel):
    def __init__(self):
        self.console = Console()

    def approve(self, title, details, approver):
        body = "\n".join(f"[bold]{k}[/]: {v}" for k, v in details.items())
        self.console.print(Panel(body, title=f"APPROVAL NEEDED ({approver}) — {title}", border_style="yellow"))
        answer = Prompt.ask("Approve?", choices=["y", "n"], default="n")
        note = Prompt.ask("Note (optional)", default="")
        return HumanReply(answer == "y", note, by=approver or "terminal user")

    def ask(self, question, options=None):
        self.console.print(Panel(question, title="AGENT QUESTION", border_style="cyan"))
        if options:
            for i, o in enumerate(options, 1):
                self.console.print(f"  {i}. {o}")
        answer = Prompt.ask("Your answer")
        if options and answer.isdigit() and 1 <= int(answer) <= len(options):
            answer = options[int(answer) - 1]
        return HumanReply(True, answer, by="terminal user")


class FileChannel(HumanChannel):
    """Writes a request file into runs/<id>/approvals and waits for the console to write a decision."""

    def __init__(self, store: RunStore, timeout_s: int = 1800):
        self.store, self.timeout_s = store, timeout_s

    def _wait(self, kind: str, payload: dict) -> HumanReply:
        rid = uuid.uuid4().hex[:8]
        req = self.store.dir / "approvals" / f"{rid}.request.json"
        req.write_text(json.dumps({"id": rid, "kind": kind, "at": now(), **payload}, indent=1))
        decision = self.store.dir / "approvals" / f"{rid}.decision.json"
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            if decision.exists():
                d = json.loads(decision.read_text())
                return HumanReply(bool(d.get("approved")), d.get("text", ""), d.get("by", "console user"))
            time.sleep(0.5)
        return HumanReply(False, "No response within timeout", by="timeout")

    def approve(self, title, details, approver):
        return self._wait("approval", {"title": title, "details": details, "approver": approver})

    def ask(self, question, options=None):
        return self._wait("question", {"question": question, "options": options or []})


class ScriptedChannel(HumanChannel):
    """For evals: deterministic answers. approvals: {rule_id: bool}; answers: list of (substring, reply)."""

    def __init__(self, approvals: dict | None = None, answers: list | None = None):
        self.approvals = approvals or {}
        self.answers = answers or []
        self.transcript: list[dict] = []

    def approve(self, title, details, approver):
        ok = bool(self.approvals.get(details.get("rule"), False))
        self.transcript.append({"kind": "approval", "title": title, "details": details, "approved": ok})
        return HumanReply(ok, "scripted", by="eval-script")

    def ask(self, question, options=None):
        reply = next((r for sub, r in self.answers if sub.lower() in question.lower()), "I don't know — use your judgement and follow the SOP.")
        self.transcript.append({"kind": "question", "question": question, "reply": reply})
        return HumanReply(True, reply, by="eval-script")
