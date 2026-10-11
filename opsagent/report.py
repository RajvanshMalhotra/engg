"""Human-readable run report: what was asked, what was done, proof, and what needs attention."""

import os
from collections import defaultdict
from pathlib import Path

LATENCY_LABELS = {
    "contract": "Understanding the request (contract)", "memory_recall": "Recalling lessons",
    "worker_model": "Operator thinking (model)", "worker_tools": "Operator acting (browser, files, API)",
    "verify_model": "Verifier thinking (model)", "verify_tools": "Verifier checking (browser, API)",
    "snapshots": "Record snapshots (before/after)", "reflect": "Distilling lessons", "report": "Writing the report",
}


def latency_breakdown(events: list[dict], total_seconds: float | None = None) -> dict:
    """Seconds per phase, from the run's own millisecond timings (spans, model calls, tool calls)."""
    spans: dict = defaultdict(int)
    for e in events:
        if e["kind"] == "span":
            spans[e["name"]] += e["ms"]
    ms = lambda kind, key="ms": sum(e.get(key, 0) for e in events if e["kind"] == kind)
    out = {"contract": spans["contract"], "memory_recall": spans["recall"],
           "worker_model": ms("llm"), "worker_tools": ms("tool"),
           "verify_model": ms("verify_llm"), "verify_tools": ms("verify_step"),
           "snapshots": spans["snapshot"], "reflect": spans["reflect"], "report": spans["report"]}
    out = {k: round(v / 1000, 1) for k, v in out.items()}
    turns = [e for e in events if e["kind"] == "llm" and "depth" in e]
    out["turns_deep"] = sum(e["depth"] == "deep" for e in turns)
    out["turns_light"] = sum(e["depth"] == "light" for e in turns)
    if total_seconds is not None:
        out["other"] = round(max(0.0, total_seconds - sum(out[k] for k in LATENCY_LABELS)), 1)
    return out

STATUS_TEXT = {
    "verified": "✅ Completed and independently verified",
    "justified_stop": "🛑 Correctly stopped / escalated (verified)",
    "not_verified": "❌ Not verified — needs human review",
    "budget_exhausted": "⏱ Stopped: step budget exhausted — needs human review",
    "error": "💥 Run error — resumable from checkpoint",
}


def write_report(store, request, contract, status, claim, verdict, changes, trace, evidence, usage, seconds) -> Path:
    events = store.events()
    approvals = [e for e in events if e["kind"] == "approval"]
    questions = [e for e in events if e["kind"] == "ask_human"]
    blocks = [e for e in events if e["kind"] == "policy" and e["effect"] != "allow"] + \
             [e for e in events if e["kind"] == "network_block"]
    lines = [f"# Run {store.run_id}", "", f"**Request:** {request}", "",
             f"**Outcome:** {STATUS_TEXT.get(status, status)}", ""]
    if claim.get("summary"):
        lines += ["## Summary (operator)", "", claim["summary"], ""]
    if verdict:
        lines += ["## Independent verification", "", f"Verdict: **{verdict.overall}**", "",
                  "| Criterion | Met | Evidence |", "|---|---|---|"]
        crit_text = {c["id"]: c["criterion"] for c in (contract.success_criteria if contract else [])}
        for c in verdict.criteria:
            label = f"{c['id']} {crit_text.get(c['id'], '')}".replace("|", "/")
            lines.append(f"| {label} | {'✔' if c.get('met') else '✘'} | {str(c.get('evidence', '')).replace('|', '/')} |")
        if verdict.unintended_changes:
            lines += ["", "**Unintended changes:**"] + [f"- {u}" for u in verdict.unintended_changes]
        if verdict.notes:
            lines += ["", f"**Auditor notes:** {verdict.notes}"]
        lines.append("")
    lines += ["## What changed in company systems (objective diff)", ""]
    lines += [f"- {c}" for c in changes] or ["- (no record changes)"]
    lines.append("")
    if approvals or questions or blocks:
        lines += ["## Human-in-the-loop & policy", ""]
        lines += [f"- Approval {'GRANTED' if a['approved'] else 'DECLINED'} by {a['by']} for `{a['request']}` "
                  f"(rule {a['rule']}){': ' + a['note'] if a.get('note') else ''}" for a in approvals]
        lines += [f"- Asked: {q['question']} → *{q['reply']}*" for q in questions]
        lines += [f"- Policy {b.get('effect')} ({b.get('rule')}): `{b.get('request') or b.get('url')}`" for b in blocks]
        lines.append("")
    if contract:
        lines += ["## Task contract", "", "```", contract.render(), "```", ""]
    lines += ["## Steps", ""] + [f"{t}" for t in trace] + [""]
    if evidence:
        lines += ["## Evidence files", ""]
        lines += [f"- [{Path(e).name}]({os.path.relpath(e, store.dir)})" for e in dict.fromkeys(evidence)]
        lines.append("")
    lines += ["## Cost", "", f"{usage.calls} LLM calls · {usage.prompt_tokens:,} prompt tokens "
              f"({usage.cached_tokens:,} cached) · {usage.completion_tokens:,} completion tokens · "
              f"≈ ${usage.cost_usd:.3f} · {seconds:.0f}s", ""]
    lat = latency_breakdown(events, seconds)
    lines += ["## Where the time went", "", "| Phase | Seconds |", "|---|---|"]
    lines += [f"| {label} | {lat[k]:.1f} |" for k, label in LATENCY_LABELS.items() if lat.get(k)]
    lines += [f"| Other (orchestration, waits for humans) | {lat.get('other', 0):.1f} |", "",
              f"Operator turns: {lat['turns_deep']} deep reasoning, {lat['turns_light']} light. "
              f"Retry back-off: {usage.retry_wait_seconds:.0f}s."]
    path = store.dir / "report.md"
    path.write_text("\n".join(lines))
    return path
