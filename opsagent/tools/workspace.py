"""Files, vision, the read-only system APIs, and company knowledge."""

import json
import re
from pathlib import Path

import httpx
import yaml
from pypdf import PdfReader

from opsagent import config
from opsagent.context import RunContext
from opsagent.policy import WriteRequest
from opsagent.tools.base import Tool, ToolResult, params

TRANSCRIBE_PROMPT = ("Transcribe this document image faithfully. First list every key field you can read "
                     "(document type, issuer, numbers, dates, line items, totals, bank details) as 'field: value', "
                     "then note anything illegible or uncertain. Do not guess unreadable digits — mark them [?].")


# ----------------------------------------------------------------------------- files

def file_tools(ctx: RunContext) -> list[Tool]:
    workspace = ctx.store.dir / "downloads"

    def resolve(name: str) -> Path:
        path = (workspace / Path(name).name).resolve()
        if not path.is_relative_to(workspace.resolve()):
            raise PermissionError("files outside the run workspace are not accessible")
        return path

    def list_files() -> ToolResult:
        files = sorted(p.name for p in workspace.iterdir() if p.is_file())
        return ToolResult(True, "Workspace files: " + (", ".join(files) or "(none)"), f"{len(files)} files")

    def read_file(name: str) -> ToolResult:
        path = resolve(name)
        if not path.exists():
            return ToolResult(False, f"No file '{name}' in workspace. Use list_files.", "file not found")
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages).strip()
            if len(text) < 20:  # scanned PDF without a text layer -> fall back to vision would go here
                return ToolResult(False, "PDF has no text layer (scanned). Ask for a readable copy.", "pdf no text")
            method = "pdf text layer"
        elif suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            text = ctx.llm.look(path.read_bytes(), TRANSCRIBE_PROMPT)
            method = f"vision model ({ctx.llm.vision_model}) — verify critical digits if anything looks uncertain"
        else:
            text = path.read_text(errors="replace")
            method = "plain text"
        return ToolResult(True, f"Contents of {path.name} (read via {method}):\n{text[:8000]}",
                          f"read {path.name} via {method.split(' —')[0]}", untrusted_source=f"file:{path.name}",
                          evidence=[str(path)])

    return [
        Tool("list_files", "List files in this run's workspace (downloaded attachments).", params(), list_files),
        Tool("read_file", "Read a workspace file. PDFs are read from their text layer; images (scans, photos) are "
             "transcribed with a vision model.", params(["name"], name=("string", "file name")), read_file),
    ]


# ----------------------------------------------------------------------------- vision on the live screen

def vision_tools(ctx: RunContext) -> list[Tool]:
    def look_at_screen(question: str) -> ToolResult:
        if ctx.browser is None or ctx.browser.page is None:
            return ToolResult(False, "No page open.", "no page")
        n = len(list((ctx.store.dir / "screens").iterdir()))
        shot = ctx.browser.screenshot(f"look-{n:03d}")
        answer = ctx.llm.look(Path(shot).read_bytes(), question)
        return ToolResult(True, f"Vision answer: {answer}", f"looked at screen: {question[:60]}",
                          untrusted_source="screenshot", evidence=[shot])

    return [Tool("look_at_screen", "Ask a vision model a question about a screenshot of the current page. Use only "
                 "when the text observation is insufficient (charts, images, layout).",
                 params(["question"], question=("string", "what to look for")), look_at_screen)]


# ----------------------------------------------------------------------------- system APIs (read-only)

def api_tools(ctx: RunContext) -> list[Tool]:
    systems = config.load_systems()
    apis = {name: a for name, s in systems.items() for a in s.get("access", []) if a["kind"] == "api"}

    def api_get(system: str, path: str) -> ToolResult:
        api = apis.get(system)
        if not api:
            return ToolResult(False, f"System '{system}' has no API. Systems with APIs: {list(apis)}. Use the GUI.",
                              "no api")
        url = api["base_url"].rstrip("/") + "/" + path.lstrip("/")
        decision = ctx.policy.evaluate(WriteRequest("GET", url))
        if decision.effect == "deny":
            return ToolResult(False, f"BLOCKED by policy ({decision.rule_id}): {decision.reason}", "blocked")
        name, _, value = api["auth_header"].partition(": ")  # credential injected here, never shown to the model
        resp = httpx.get(url, headers={name: value}, timeout=15)
        body = resp.text
        try:
            body = json.dumps(resp.json(), indent=1)
        except ValueError:
            pass
        ok = resp.status_code < 400
        return ToolResult(ok, f"GET {url} -> HTTP {resp.status_code}\n{body[:6000]}",
                          f"api {system} GET {path} -> {resp.status_code}", untrusted_source=f"api:{system}")

    if not apis:
        return []
    desc = "; ".join(f"{n}: {', '.join(a.get('endpoints', []))}" for n, a in apis.items())
    return [Tool("api_get", f"Call a company system's read-only API (preferred over the GUI for lookups). "
                 f"Available: {desc}",
                 params(["system", "path"], system=("string", "system name"),
                        path=("string", "endpoint path incl. query string")), api_get)]


# ----------------------------------------------------------------------------- company knowledge (trusted)

def _frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    return (yaml.safe_load(m.group(1)), m.group(2)) if m else ({}, text)


def knowledge_index() -> str:
    lines = []
    for p in sorted(config.KNOWLEDGE_DIR.glob("*.md")):
        meta, _ = _frontmatter(p)
        lines.append(f"- {p.stem}: {meta.get('title', p.stem)} (applies to: {meta.get('applies_to', '—')})")
    return "\n".join(lines)


def systems_overview() -> str:
    out = []
    for name, s in config.load_systems().items():
        paths = []
        for a in s.get("access", []):
            if a["kind"] == "api":
                paths.append(f"api ({a.get('mode', '')}): " + "; ".join(a.get("endpoints", [])))
            elif a["kind"] == "gui":
                paths.append(f"gui: {a.get('notes', '')}")
            else:
                paths.append(a["kind"])
        login = " Login with fill_secret using " + ", ".join(
            v for k, v in s["login"].items() if k.endswith("_secret")) + "." if s.get("login") else ""
        out.append(f"- {name}: {s['description']}. URL: {s.get('base_url', 'n/a')}.{login}\n    " + "\n    ".join(paths))
    return "\n".join(out)


def knowledge_tools(ctx: RunContext) -> list[Tool]:
    def read_company_doc(name: str) -> ToolResult:
        path = config.KNOWLEDGE_DIR / f"{Path(name).stem}.md"
        if not path.exists():
            return ToolResult(False, f"No document '{name}'. Index:\n{knowledge_index()}", "doc not found")
        return ToolResult(True, path.read_text(), f"read company doc {path.stem}")

    return [Tool("read_company_doc", "Read a company SOP/policy/directory document by name (see the index in your "
                 "instructions). These are authoritative company procedures.",
                 params(["name"], name=("string", "document name")), read_company_doc)]
