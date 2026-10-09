"""Objective state diff: snapshot every system's declared read endpoints before and after a run.

This gives the verifier (and the run report) ground truth about what actually changed, independent
of anything the worker agent claims. Systems opt in via `snapshot:` in company/systems.yaml.
"""

import httpx

from opsagent import config


def take() -> dict:
    snap = {}
    for name, sysdef in config.load_systems().items():
        for access in sysdef.get("access", []):
            if access["kind"] != "api" or not access.get("snapshot"):
                continue
            header, _, value = access["auth_header"].partition(": ")
            for endpoint in access["snapshot"]:
                try:
                    resp = httpx.get(access["base_url"].rstrip("/") + endpoint, headers={header: value}, timeout=10)
                    snap[f"{name}{endpoint}"] = resp.json() if resp.status_code == 200 else {"error": resp.status_code}
                except httpx.HTTPError as e:
                    snap[f"{name}{endpoint}"] = {"error": str(e)}
    return snap


def diff(before: dict, after: dict) -> list[str]:
    changes = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if not isinstance(old, list) or not isinstance(new, list):
            if old != new:
                changes.append(f"{key}: snapshot unavailable or changed shape")
            continue
        old_by_id = {r.get("id"): r for r in old}
        new_by_id = {r.get("id"): r for r in new}
        for rid in new_by_id.keys() - old_by_id.keys():
            changes.append(f"{key}: CREATED {rid} {_brief(new_by_id[rid])}")
        for rid in old_by_id.keys() - new_by_id.keys():
            changes.append(f"{key}: DELETED {rid}")
        for rid in old_by_id.keys() & new_by_id.keys():
            fields = [f"{f}: {old_by_id[rid].get(f)!r} -> {v!r}" for f, v in new_by_id[rid].items()
                      if old_by_id[rid].get(f) != v]
            if fields:
                changes.append(f"{key}: UPDATED {rid} " + "; ".join(fields))
    return changes


def _brief(record: dict) -> str:
    return "{" + ", ".join(f"{k}={v!r}" for k, v in record.items() if k not in ("id", "created_at")) + "}"
