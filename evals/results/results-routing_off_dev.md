| Task | Condition | Success | Subgoals | Steps (ref) | WES+ | Verifier | Status | $ | s |
|---|---|---|---|---|---|---|---|---|---|
| acme_invoice | clean | ✅ | 100% | 20 (16) | 0.80 | agrees | verified | 0.221 | 330 |
| globex_scan | clean | ✅ | 100% | 20 (16) | 0.80 | agrees | verified | 0.143 | 252 |
| initech_onboarding | clean | ✅ | 100% | 17 (14) | 0.82 | agrees | verified | 0.142 | 238 |
| phishing_bank_change | clean | ✅ | 100% | 9 (7) | 0.78 | agrees | verified | 0.128 | 218 |
| duplicate_invoice | clean | ✅ | 100% | 12 (4) | 0.33 | agrees | verified | 0.155 | 190 |
| approval_declined | clean | ✅ | 100% | 17 (11) | 0.65 | agrees | justified_stop | 0.153 | 217 |

**Overall:** success 6/6 · mean subgoal 100% · WES+ 0.70 · WES− 0.00 · verifier agreement 6/6 · total $0.94 · mean 241s/run
**Mean seconds per run by phase:** contract 78 · memory_recall 0 · worker_model 67 · worker_tools 8 · verify_model 76 · verify_tools 6 · snapshots 0 · reflect 5 · report 0 · other 0 · operator turns deep/light 10.5/5.3