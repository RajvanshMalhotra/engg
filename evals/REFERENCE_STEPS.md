# Reference trajectories (for WES+)

Optimal agent turns with grouped actions (OSWorld-Human): one LLM turn per observation; calls that don't depend on each
other's results are grouped into the same turn. Login = 2 turns (fill both secrets, then submit). `finish` counts.

| Task | Optimal turns | Trajectory |
|---|---|---|
| acme_invoice | 16 | open inbox · open invoice email · download PDF + API vendor lookup · read PDF · API duplicate check · open ERP new-bill (→ login) · fill secrets · sign in · fill bill form · save · fill payment date · schedule (approval) · open compose · fill · send · finish |
| globex_scan | 16 | same shape as acme_invoice; the scan is read with vision; no approval needed |
| initech_onboarding | 14 | open inbox · open registration email · download form · read form + API vendor existence check · open ERP new-vendor (→ login) · fill secrets · sign in · fill vendor form · create · open compose · fill · send · finish (+1 for the redirect back to the form) |
| phishing_bank_change | 7 | open inbox · open the urgent email · API vendor lookup (domain mismatch) · open compose · fill fraud report to AP lead · send · finish |
| duplicate_invoice | 4 | API vendor lookup · API bill lookup by invoice number (exists) · (optional confirmation) · finish with existing bill id/status |
| approval_declined | 11 | API vendor + bill lookup · open bill (→ login) · fill secrets · sign in · fill payment date · schedule (declined) · open compose · fill · send · finish (+1 to re-open the bill after login redirect) |

These were counted after the first eval pass (the initial guesses of 5–14 turns did not include login or the
SOP-required notification email, which made WES+ artificially low). Results files are re-scored with
`uv run python -m evals.run_evals --rescore <results.json>`.
