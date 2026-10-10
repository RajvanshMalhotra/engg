"""Goal -> Understand: turn a short request into an explicit Task Contract *before* acting.

The contract is the agent's pinned objective (re-injected every turn, so constraints are not forgotten
mid-task) and the verifier's yardstick (success criteria are fixed up-front, so "done" can't be
redefined after the fact — any amendment is logged with a reason and judged by the verifier).

Understanding is two-step, just-in-time: first decide which company documents matter, then read them
and draft the contract with the actual procedures in view.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import date

from opsagent import config
from opsagent.llm import LLM
from opsagent.memory import Lesson, format_lessons
from opsagent.tools.workspace import knowledge_index, systems_overview


@dataclass
class TaskContract:
    request: str
    interpretation: str
    deliverable: str
    constraints: list[str]
    success_criteria: list[dict]          # {id, criterion, how_to_check}
    subgoals: list[dict]                  # {id, goal, done_when, status, comment}
    open_questions: list[dict] = field(default_factory=list)  # {question, blocking, default_assumption}
    relevant_docs: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    amendments: list[dict] = field(default_factory=list)      # {reason, old, new}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskContract":
        return cls(**{k: d.get(k, [] if k not in ("request", "interpretation", "deliverable") else "")
                      for k in cls.__dataclass_fields__})

    def render_static(self) -> str:
        """The parts fixed at the start — sent once in the first message so they stay in the cached prefix."""
        lines = [f"REQUEST: {self.request}", f"INTENDED OUTCOME: {self.interpretation}",
                 f"DELIVERABLE TO USER: {self.deliverable}", "CONSTRAINTS:"]
        lines += [f"  - {c}" for c in self.constraints]
        lines.append("SUCCESS CRITERIA (verified independently before the task can complete):")
        lines += [f"  {c['id']}. {c['criterion']}" for c in self.success_criteria]
        if self.assumptions:
            lines.append("ASSUMPTIONS:")
            lines += [f"  - {a}" for a in self.assumptions]
        return "\n".join(lines)

    def render_dynamic(self) -> str:
        """The parts that change during the run — re-sent every turn."""
        lines = ["PLAN:"]
        for s in self.subgoals:
            comment = f" — {s['comment']}" if s.get("comment") else ""
            lines.append(f"  [{s.get('status', 'pending')}] {s['id']}: {s['goal']} (done when: {s.get('done_when', '?')}){comment}")
        if self.amendments:
            lines.append("CONTRACT AMENDED — these replace the criteria/constraints in your task brief:")
            last = self.amendments[-1]["new"]
            lines += [f"  constraint: {c}" for c in last["constraints"]]
            lines += [f"  {c.get('id', '?')}. {c.get('criterion', '')}" for c in last["success_criteria"]]
        return "\n".join(lines)

    def render(self, with_status: bool = True) -> str:
        lines = [f"REQUEST: {self.request}", f"INTENDED OUTCOME: {self.interpretation}",
                 f"DELIVERABLE TO USER: {self.deliverable}", "CONSTRAINTS:"]
        lines += [f"  - {c}" for c in self.constraints]
        lines.append("SUCCESS CRITERIA (verified independently before the task can complete):")
        lines += [f"  {c['id']}. {c['criterion']}" for c in self.success_criteria]
        if self.assumptions:
            lines.append("ASSUMPTIONS:")
            lines += [f"  - {a}" for a in self.assumptions]
        lines.append("PLAN:")
        for s in self.subgoals:
            status = f"[{s.get('status', 'pending')}] " if with_status else ""
            comment = f" — {s['comment']}" if s.get("comment") else ""
            lines.append(f"  {status}{s['id']}: {s['goal']} (done when: {s.get('done_when', '?')}){comment}")
        if self.amendments:
            lines.append("AMENDMENTS:")
            lines += [f"  - {a['reason']}" for a in self.amendments]
        return "\n".join(lines)


SELECT_SYSTEM = """You route requests for an autonomous operator working inside a company.
Given a request and an index of company documents, return JSON: {"relevant_docs": [names], "why": "..."}.
Pick every document whose procedures or policies could apply (SOPs, policies, directory). Prefer recall over precision."""

CONTRACT_SYSTEM = """You are the planning stage of an autonomous company operator. Turn the user's short request into a
precise task contract BEFORE any action is taken. Company procedures (SOPs) and policies are binding: fold their
implicit requirements into the contract even if the user did not mention them (e.g. duplicate checks, approvals,
notifications). Do not invent facts you have not seen (amounts, ids) — plan to discover them.

Return JSON with exactly these keys:
{
 "interpretation": "the outcome the user actually wants, in one or two sentences",
 "deliverable": "what the user should receive at the end (e.g. confirmation with bill id + evidence)",
 "constraints": ["hard rules from SOPs/policy/user that must hold throughout"],
 "success_criteria": [{"id": "C1", "criterion": "an observable end-state fact", "how_to_check": "which system/page/API proves it"}],
 "subgoals": [{"id": "S1", "goal": "...", "done_when": "observable condition"}],
 "open_questions": [{"question": "...", "blocking": true|false, "default_assumption": "what you'd assume if not answered"}],
 "assumptions": ["..."]
}
Rules for success criteria: they must be checkable against real system state (not "the agent tried").
Write them so they stay correct if legitimate exceptions occur — e.g. "Exactly one bill exists in the ERP for
invoice X (created now, or already existed and was reported)". Always include a criterion that no unintended or
policy-violating changes were made. If the SOP says to stop and escalate in some situation, make the criteria
cover that branch too.
open_questions are ONLY for things the requester alone can answer — their intent, preferences, or authority
(e.g. "which of these two vendors did you mean?"). Never ask about facts that can be found in the company's systems or
documents (whether an email or form exists, what a field contains): make finding them a subgoal instead.
Only mark a question blocking if the task genuinely cannot proceed safely without the answer.
Describe success criteria by meaning, not by guessed field values: you have not seen the systems' data model yet
(write "bills that are not yet paid", not "bills with status 'unpaid'").
Approvals required by policy are requested by the platform automatically when the operator attempts the action, so
"get approval" is never a separate subgoal: plan the action itself and note that it is approval-gated.
Scope the plan to what THIS request needs: an SOP describes how to do each step correctly, not that every step applies
to every request (e.g. paying a bill that is already recorded does not require finding and re-reading the invoice).
When the request refers to something that may already exist (a bill, a vendor, a payment), the first subgoal is to
look it up in the system of record (read-only API first), and later subgoals depend on what that lookup finds."""


def build_contract(llm: LLM, request: str, lessons: list[Lesson]) -> TaskContract:
    index = knowledge_index()
    picked = llm.json(SELECT_SYSTEM, f"Request: {request}\n\nDocument index:\n{index}")
    docs = [d for d in picked.get("relevant_docs", []) if (config.KNOWLEDGE_DIR / f"{d}.md").exists()]
    doc_text = "\n\n".join(f"=== {d} ===\n{(config.KNOWLEDGE_DIR / f'{d}.md').read_text()}" for d in docs)
    user = (f"Today is {date.today().isoformat()}.\n\nREQUEST: {request}\n\n"
            f"COMPANY SYSTEMS:\n{systems_overview()}\n\n"
            f"RELEVANT COMPANY DOCUMENTS:\n{doc_text or '(none)'}\n\n"
            f"EXPERIENCE FROM PAST RUNS (advisory; SOPs and policy take precedence):\n{format_lessons(lessons)}")
    raw = llm.json(CONTRACT_SYSTEM, user)
    subgoals = [{**s, "status": "pending", "comment": ""} for s in raw.get("subgoals", [])]
    return TaskContract(
        request=request,
        interpretation=raw.get("interpretation", ""),
        deliverable=raw.get("deliverable", ""),
        constraints=raw.get("constraints", []),
        success_criteria=raw.get("success_criteria", []),
        subgoals=subgoals,
        open_questions=raw.get("open_questions", []),
        relevant_docs=docs,
        assumptions=raw.get("assumptions", []),
    )


def contract_json(contract: TaskContract) -> str:
    return json.dumps(contract.to_dict(), indent=1)
