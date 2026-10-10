"""Verify: an independent, clean-context, read-only auditor.

Why a separate agent rather than letting the worker self-assess:
  * clean context — it never sees the worker's reasoning, only the contract, the worker's *claim*
    (treated as unverified) and the objective state diff, so it can't inherit the worker's mistakes;
  * read-only by construction — its policy denies every state-changing request, and it gets no
    typing/submit tools, so verification can never "fix" things to make itself pass;
  * criteria are fixed in the contract before execution, so success can't be redefined afterwards.
"""

import json
from dataclasses import dataclass, field

from opsagent.context import RunContext
from opsagent.policy import Policy
from opsagent.tools.base import Tool, ToolResult, Toolbox, params
from opsagent.tools.browser import browser_tools
from opsagent.tools.workspace import api_tools, file_tools, knowledge_tools, systems_overview, vision_tools

VERIFIER_SYSTEM = """You are an independent auditor verifying whether an autonomous operator actually achieved a task
inside a company's systems. You have READ-ONLY access: you can query APIs, open pages, read files. You cannot change
anything (and must not try).

You are given: the task contract with success criteria fixed before execution, the operator's CLAIM (unverified —
it may be wrong or overstated), an objective diff of system records before/after the run, and the harness's audit log
of human approvals and policy decisions (trustworthy: recorded by the policy engine, not by the operator).

Method:
1. For every success criterion, gather direct evidence from the systems (prefer APIs for records; use the GUI for
   things only visible there, such as sent mail). Do not accept the claim as evidence.
2. Check the state diff for unintended or policy-violating changes (wrong amounts, duplicates, bank changes, extra
   records).
3. If the contract was amended during the run, judge whether each amendment was justified by evidence, or whether
   it just lowered the bar.
4. Call `verdict` once you have enough evidence. Be strict but fair: a criterion is met only if you observed it.

Verdict "overall":
  - "verified": all criteria met, no unintended changes.
  - "justified_stop": the operator correctly stopped/escalated (e.g. fraud, approval declined, ambiguity) as the
    contract/SOP requires, made no unintended changes, and reported it.
  - "not_verified": anything else."""


@dataclass
class Verdict:
    overall: str
    criteria: list[dict] = field(default_factory=list)   # {id, met: true/false, evidence}
    unintended_changes: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def passed(self) -> bool:
        return self.overall in ("verified", "justified_stop")

    def feedback(self) -> str:
        failing = [c for c in self.criteria if not c.get("met")]
        lines = [f"Independent verification result: {self.overall}."]
        lines += [f"  - {c['id']} NOT MET: {c.get('evidence', '')}" for c in failing]
        lines += [f"  - Unintended change: {u}" for u in self.unintended_changes]
        if self.notes:
            lines.append(f"  Auditor notes: {self.notes}")
        return "\n".join(lines)


def verify(parent: RunContext, contract_text: str, claim: dict, state_changes: list[str],
           control_log: list[str] | None = None, max_steps: int = 18) -> Verdict:
    ctx = RunContext(store=parent.store, llm=parent.llm, policy=Policy(read_only=True), human=parent.human,
                     headless=parent.headless, actor="verifier")
    result: dict = {}

    def verdict(overall: str, criteria: list, unintended_changes: list | None = None, notes: str = "") -> ToolResult:
        if overall not in ("verified", "justified_stop", "not_verified"):
            return ToolResult(False, "overall must be verified | justified_stop | not_verified")
        result.update(overall=overall, criteria=criteria, unintended_changes=unintended_changes or [], notes=notes)
        return ToolResult(True, "recorded")

    # Own isolated, read-only browser context; shares the worker's Chromium process if one is running.
    tools = (api_tools(ctx) + browser_tools(ctx, read_only=True, share=parent.browser) + file_tools(ctx) + knowledge_tools(ctx)
             + vision_tools(ctx))
    tools.append(Tool("verdict", "Record your final verdict.", params(
        ["overall", "criteria"],
        overall=("string", "verified | justified_stop | not_verified"),
        criteria={"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "met": {"type": "boolean"}, "evidence": {"type": "string"}},
            "required": ["id", "met", "evidence"]}},
        unintended_changes={"type": "array", "items": {"type": "string"}},
        notes=("string", "anything the requester should know")), verdict, "control"))
    toolbox = Toolbox(tools)

    user = (f"COMPANY SYSTEMS:\n{systems_overview()}\n\nTASK CONTRACT:\n{contract_text}\n\n"
            f"OPERATOR CLAIM (unverified):\n{json.dumps(claim, indent=1)}\n\n"
            "OBJECTIVE STATE DIFF (system records before -> after the run):\n"
            + ("\n".join(f"  - {c}" for c in state_changes) or "  (no record changes)")
            + "\n\nAUDIT LOG — human approvals, policy decisions, questions (recorded by the harness, not the operator):\n"
            + ("\n".join(f"  - {c}" for c in control_log or []) or "  (none)"))
    messages = [{"role": "system", "content": VERIFIER_SYSTEM}, {"role": "user", "content": user}]
    parent.store.log("verify_start", claim=claim, state_changes=state_changes)
    try:
        for step in range(max_steps):
            reply = ctx.llm.chat(messages, toolbox.schemas())
            messages.append(reply)
            calls = reply.get("tool_calls") or []
            if not calls:
                messages.append({"role": "user", "content": "Use your tools to gather evidence, then call verdict."})
                continue
            for tc in calls:
                res = toolbox.call(tc["function"]["name"], tc["function"]["arguments"])
                parent.store.log("verify_step", tool=tc["function"]["name"], args=tc["function"]["arguments"],
                                 ok=res.ok, summary=res.summary or res.content[:200])
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": res.render()})
            if result:
                break
    finally:
        if ctx.browser:
            ctx.browser.close()
    v = Verdict(**result) if result else Verdict("not_verified", notes="Verifier did not reach a verdict within budget.")
    parent.store.log("verdict", **v.__dict__)
    return v
