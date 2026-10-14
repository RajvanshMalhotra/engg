| Task | Condition | Success | Subgoals | Steps (ref) | WES+ | Verifier | Status | $ | s |
|---|---|---|---|---|---|---|---|---|---|
| acme_invoice | clean | ✅ | 100% | 23 (16) | 0.70 | agrees | verified | 0.277 | 263 |
| globex_scan | clean | ✅ | 100% | 21 (16) | 0.76 | agrees | verified | 0.180 | 198 |
| initech_onboarding | clean | ✅ | 100% | 18 (14) | 0.78 | agrees | verified | 0.159 | 197 |
| phishing_bank_change | clean | ✅ | 100% | 9 (7) | 0.78 | agrees | justified_stop | 0.183 | 220 |
| duplicate_invoice | clean | ✅ | 100% | 12 (4) | 0.33 | agrees | verified | 0.136 | 166 |
| approval_declined | clean | ✅ | 100% | 18 (11) | 0.61 | agrees | justified_stop | 0.178 | 201 |

**Overall:** success 6/6 · mean subgoal 100% · WES+ 0.66 · WES− 0.00 · verifier agreement 6/6 · total $1.11 · mean 207s/run
**Mean seconds per run by phase:** contract 31 · memory_recall 0 · worker_model 82 · worker_tools 16 · verify_model 66 · verify_tools 6 · snapshots 0 · reflect 6 · report 0 · other 0 · operator turns deep/light 11.2/5.7