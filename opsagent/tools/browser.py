"""Browser tool: structured observation + semantic element actions (no pixel clicking).

Observation: the page is rendered as compact text in reading order; every interactive element gets
an id, e.g.  [7 textbox "Invoice number" value=""]  — the model acts on ids, not coordinates.

After every action we:
  1. let the page settle,
  2. diff the new observation against the previous one (state-delta, cf. Desktop-Delta Bench),
  3. report navigation + HTTP status of the main document, so failures are explicit.

Safety (defence in depth):
  * pre-flight: before a click/Enter that would submit a form, we predict the exact request
    (method, action URL, form fields) and run it through the policy engine — asking a human if required;
  * enforcement: every request leaving the browser is intercepted; non-read requests that were not
    pre-cleared with an identical payload are re-evaluated and blocked unless policy allows them;
  * only origins of registered company systems are reachable.
"""

import difflib
import re
from urllib.parse import parse_qsl, urlparse

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from opsagent import config
from opsagent.context import RunContext
from opsagent.policy import READ_METHODS, WriteRequest
from opsagent.tools.base import Tool, ToolResult, params

OBSERVE_JS = r"""
() => {
  const INTERACTIVE = 'a[href],button,input,select,textarea,[role=button],[role=link],[contenteditable=true]';
  const BLOCK = new Set(['div','p','tr','li','ul','ol','table','thead','tbody','form','section','header','main',
                         'footer','nav','h1','h2','h3','h4','h5','h6','br','hr','pre','article','aside']);
  document.querySelectorAll('[data-oid]').forEach(e => e.removeAttribute('data-oid'));
  let n = 0; const parts = [];
  const visible = el => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const q = s => JSON.stringify((s || '').replace(/\s+/g, ' ').trim().slice(0, 80));
  const labelOf = el => {
    if (el.labels && el.labels.length) return el.labels[0].innerText;
    return el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || '';
  };
  const describe = (el, id) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return `[${id} link ${q(el.innerText || el.getAttribute('aria-label'))}${el.hasAttribute('download') ? ' (download)' : ''}]`;
    if (tag === 'button' || el.getAttribute('role') === 'button' || (tag === 'input' && ['submit','button'].includes(el.type)))
      return `[${id} button ${q(el.innerText || el.value || el.getAttribute('aria-label'))}]`;
    if (tag === 'select') {
      const sel = el.options[el.selectedIndex];
      const opts = Array.from(el.options).map(o => q(o.text)).join(' | ');
      return `[${id} select ${q(labelOf(el))} selected=${q(sel ? sel.text : '')} options: ${opts}]`;
    }
    if (tag === 'textarea') return `[${id} textarea ${q(labelOf(el))} value=${q(el.value)}]`;
    if (tag === 'input') {
      if (el.type === 'password') return `[${id} password ${q(labelOf(el))} ${el.value ? 'filled' : 'empty'}]`;
      if (el.type === 'checkbox' || el.type === 'radio') return `[${id} ${el.type} ${q(labelOf(el))} checked=${el.checked}]`;
      const kind = el.type === 'text' || !el.type ? 'textbox' : el.type;
      return `[${id} ${kind} ${q(labelOf(el))} value=${q(el.value)}]`;
    }
    return `[${id} ${tag} ${q(el.innerText)}]`;
  };
  const walk = node => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) { const t = child.textContent.replace(/\s+/g, ' '); if (t.trim()) parts.push(t); continue; }
      if (child.nodeType !== 1) continue;
      const el = child, tag = el.tagName.toLowerCase();
      if (['script','style','noscript','template','head'].includes(tag)) continue;
      if (tag === 'input' && el.type === 'hidden') continue;
      if (!visible(el)) continue;
      if (tag === 'label' && el.htmlFor) continue;              // reported with its control
      if (el.matches(INTERACTIVE)) {
        const id = ++n; el.setAttribute('data-oid', String(id));
        parts.push(' ' + describe(el, id) + ' ');
        continue;
      }
      if (tag === 'td' || tag === 'th') parts.push(' | ');
      const block = BLOCK.has(tag);
      if (block) parts.push('\n');
      if (/^h[1-6]$/.test(tag)) parts.push('#'.repeat(+tag[1]) + ' ');
      if (el.getAttribute('role') === 'alert') parts.push('ALERT: ');
      walk(el);
      if (block) parts.push('\n');
    }
  };
  walk(document.body);
  const text = parts.join('').split('\n').map(l => l.replace(/\s+/g, ' ').replace(/^\s*\|\s*/, '').trim())
                    .filter(Boolean).join('\n');
  return {url: location.href, title: document.title, text};
}
"""

PREDICT_SUBMIT_JS = r"""
([oid, viaEnter]) => {
  const el = document.querySelector(`[data-oid="${oid}"]`);
  if (!el) return null;
  const tag = el.tagName.toLowerCase();
  let form = null;
  if (viaEnter) form = el.form || null;
  else if ((tag === 'button' && (el.type || 'submit') === 'submit') || (tag === 'input' && el.type === 'submit')) form = el.form;
  if (!form) return null;
  const fields = {};
  for (const [k, v] of new FormData(form).entries()) fields[k] = typeof v === 'string' ? v : '[file]';
  return {method: (form.getAttribute('method') || 'GET').toUpperCase(), action: form.action, fields};
}
"""

MAX_OBS_CHARS = 9000


def _norm(text: str) -> list[str]:
    """Drop element ids so the diff shows what changed on the page, not id renumbering."""
    return [re.sub(r"\[\d+ ", "[", line) for line in text.splitlines()]


class Browser:
    def __init__(self, ctx: RunContext, read_only: bool = False, share: "Browser | None" = None):
        self.ctx = ctx
        self.read_only = read_only
        self.share = share  # reuse another Browser's Chromium (sync Playwright allows one driver per thread)
        self.allowed_origins = {f"{urlparse(config.SANDBOX_URL).scheme}://{urlparse(config.SANDBOX_URL).netloc}"}
        self.cleared: list[tuple[str, str, dict]] = []   # pre-approved (method, path, fields)
        self.blocked: list[str] = []                     # requests the network layer refused this action
        self.notices: list[str] = []                     # policy/approval outcomes to surface in the next observation
        self.last_obs = None
        self.last_status: int | None = None
        self.pending_write: str | None = None
        self._pw = self._browser = self._context = self.page = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.page:
            return
        if self.share:
            self.share.start()
            self._pw, self._browser = self.share._pw, self.share._browser
        else:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self.ctx.headless)
        state = self.ctx.store.browser_state
        self._context = self._browser.new_context(
            accept_downloads=True, viewport={"width": 1280, "height": 900},
            storage_state=str(state) if state.exists() else None)  # verifier reuses sessions, never saves
        self._context.route("**/*", self._route)
        self.page = self._context.new_page()
        self.page.on("response", self._on_response)

    def close(self) -> None:
        try:
            if self._context and not self.read_only:
                self._context.storage_state(path=str(self.ctx.store.browser_state))
            if self.share:  # only our own context; the shared browser belongs to its owner
                if self._context:
                    self._context.close()
                self.page = None
                return
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except PWError:
            pass
        self.page = None

    def save_state(self) -> None:
        if self._context and not self.read_only:
            self._context.storage_state(path=str(self.ctx.store.browser_state))

    # ------------------------------------------------------------------ enforcement
    def _on_response(self, response) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.last_status = response.status
        except PWError:
            pass

    def _route(self, route, request) -> None:
        url = urlparse(request.url)
        origin = f"{url.scheme}://{url.netloc}"
        if url.scheme in ("data", "blob", "about"):
            return route.continue_()
        if origin not in self.allowed_origins:
            self.blocked.append(f"{request.method} {request.url} (origin not a registered company system)")
            self.ctx.store.log("network_block", url=request.url, reason="origin not allowed")
            return route.abort("blockedbyclient")
        fields = {}
        if request.method.upper() not in READ_METHODS and request.post_data:
            fields = dict(parse_qsl(request.post_data, keep_blank_values=True))
        for i, (m, p, f) in enumerate(self.cleared):
            if m == request.method.upper() and p == url.path and f == fields:
                self.cleared.pop(i)
                return route.continue_()
        decision = self.ctx.policy.evaluate(WriteRequest(request.method, request.url, fields))
        if decision.effect == "allow":
            return route.continue_()
        self.blocked.append(f"{request.method} {url.path}: {decision.effect} by rule '{decision.rule_id}' — {decision.reason}")
        self.ctx.store.log("network_block", url=request.url, method=request.method, rule=decision.rule_id,
                           effect=decision.effect, actor=self.ctx.actor)
        return route.abort("blockedbyclient")

    def _preflight(self, oid: int, via_enter: bool) -> ToolResult | None:
        """If this action would submit a form, check policy (and get approval) for that exact request."""
        pred = self.page.evaluate(PREDICT_SUBMIT_JS, [oid, via_enter])
        if not pred or pred["method"] in READ_METHODS:
            return None
        req = WriteRequest(pred["method"], pred["action"], pred["fields"])
        decision = self.ctx.policy.evaluate(req)
        self.ctx.store.log("policy", request=req.describe(), effect=decision.effect, rule=decision.rule_id,
                           reason=decision.reason, actor=self.ctx.actor)
        if decision.effect == "deny":
            return ToolResult(False, f"BLOCKED by company policy (rule '{decision.rule_id}'): {decision.reason} "
                                     "This is a hard rule — do not try another route to the same action.",
                              f"blocked by policy {decision.rule_id}", data={"policy": decision.rule_id})
        if decision.effect == "require_approval":
            details = {"rule": decision.rule_id, "why": decision.reason, "action": f"{req.method} {req.path}",
                       **{k: v for k, v in req.fields.items() if "password" not in k}}
            details.update(_enrich(req))
            reply = self.ctx.human.approve(f"Operator wants to: {req.method} {req.path}", details, decision.approver)
            self.ctx.store.log("approval", request=req.describe(), rule=decision.rule_id, approved=reply.approved,
                               by=reply.by, note=reply.text)
            if not reply.approved:
                return ToolResult(False, f"{decision.approver or 'Human'} DECLINED this action ({decision.rule_id}). "
                                         f"Note: {reply.text or '—'}. Do not retry it; report this in your summary.",
                                  f"approval declined ({decision.rule_id})", data={"approval": False})
            # The agent must know approval happened — otherwise a granted approval looks like a policy bypass.
            self.notices.append(f"POLICY: '{decision.rule_id}' required {decision.approver or 'human'} approval for "
                                f"{req.method} {req.path} — APPROVED by {reply.by}"
                                + (f" (note: {reply.text})" if reply.text else "") + ". The request was allowed.")
        self.cleared.append((req.method, urlparse(req.url).path, req.fields))
        self.pending_write = f"{req.method} {req.path}"
        return None

    def _evidence(self, result: ToolResult) -> ToolResult:
        """After a state-changing submit, keep a screenshot of the resulting page as evidence."""
        if self.pending_write:
            n = len(list((self.ctx.store.dir / "screens").iterdir()))
            slug = re.sub(r"[^a-z0-9]+", "-", self.pending_write.lower()).strip("-")
            result.evidence.append(self.screenshot(f"{n:03d}-{slug}"))
            self.pending_write = None
        return result

    # ------------------------------------------------------------------ observation
    def observe(self) -> dict:
        try:
            obs = self.page.evaluate(OBSERVE_JS)
        except PWError:
            self.page.wait_for_timeout(500)
            obs = self.page.evaluate(OBSERVE_JS)
        obs["text"] = self.ctx.redact(obs["text"])
        return obs

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=6000)
        except PWTimeout:
            pass
        self.page.wait_for_timeout(150)

    def _after(self, action_desc: str) -> ToolResult:
        before = self.last_obs
        self._settle()
        obs = self.observe()
        self.last_obs = obs
        lines = [f"Action: {action_desc}"]
        if self.notices:
            lines += self.notices
            self.notices = []
        if self.blocked:
            lines.append("NETWORK LAYER BLOCKED: " + "; ".join(self.blocked))
            self.blocked = []
        if before and before["url"] != obs["url"]:
            lines.append(f"Navigated: {before['url']} -> {obs['url']} (HTTP {self.last_status})")
        elif before:
            diff = [l for l in difflib.unified_diff(_norm(before["text"]), _norm(obs["text"]), lineterm="", n=0)
                    if l[:1] in "+-" and not l.startswith(("+++", "---"))]
            lines.append("Page changes: " + ("\n" + "\n".join(diff[:30]) if diff else "none visible"))
        if self.last_status and self.last_status >= 400:
            lines.append(f"WARNING: last page load returned HTTP {self.last_status}")
        page_text = obs["text"]
        if len(page_text) > MAX_OBS_CHARS:
            page_text = page_text[:MAX_OBS_CHARS] + "\n…(truncated)"
        content = "\n".join(lines) + f"\n\nPage now — {obs['title']} — {obs['url']}\n{page_text}"
        ok = not (self.last_status and self.last_status >= 400)
        return ToolResult(ok, content, f"{action_desc} -> {obs['url']} (HTTP {self.last_status})",
                          untrusted_source=f"browser:{obs['url']}", data={"url": obs["url"], "status": self.last_status})

    def _el(self, element: int):
        loc = self.page.locator(f'[data-oid="{int(element)}"]')
        if loc.count() == 0:
            raise LookupError(f"element {element} not found on the current page (ids change after every action — "
                              "use the ids from the latest observation)")
        return loc.first

    # ------------------------------------------------------------------ actions
    def open_url(self, url: str) -> ToolResult:
        self.start()
        if url.startswith("/"):
            url = config.SANDBOX_URL + url
        self.last_status = None
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except PWError as e:
            msg = str(e).splitlines()[0]
            blocked = "; ".join(self.blocked)
            self.blocked = []
            return ToolResult(False, f"Could not open {url}: {msg}. {blocked}", f"open {url} failed")
        return self._after(f"open {url}")

    def read_page(self) -> ToolResult:
        self.start()
        self.last_obs = None
        return self._after("read page")

    def click(self, element: int) -> ToolResult:
        self.start()
        el = self._el(element)
        label = self._label(element)
        blocked = self._preflight(int(element), via_enter=False)
        if blocked:
            return blocked
        self.last_status = None
        if el.get_attribute("download") is not None:
            with self.page.expect_download(timeout=10000) as dl_info:
                el.click()
            dl = dl_info.value
            dest = self.ctx.store.dir / "downloads" / dl.suggested_filename
            dl.save_as(dest)
            self.ctx.store.log("download", file=str(dest))
            return ToolResult(True, f"Downloaded {dl.suggested_filename} to workspace as '{dest.name}'. "
                                    f"Use read_file('{dest.name}') to read it.",
                              f"downloaded {dest.name}", data={"file": dest.name}, evidence=[str(dest)])
        el.click(timeout=8000)
        return self._evidence(self._after(f"click {label}"))

    def type_text(self, element: int, text: str, press_enter: bool = False) -> ToolResult:
        self.start()
        el = self._el(element)
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        if tag == "select":
            return self.select_option(element, text)
        el.fill(str(text), timeout=8000)
        if press_enter:
            return self.press_key("Enter", element)
        return self._after(f"type {text!r} into {self._label(element)}")

    def select_option(self, element: int, option: str) -> ToolResult:
        self.start()
        el = self._el(element)
        options = el.evaluate("e => Array.from(e.options).map(o => ({label: o.text.trim(), value: o.value}))")
        match = next((o for o in options if o["label"].lower() == option.lower() or o["value"] == option), None)
        if not match:
            match = next((o for o in options if option.lower() in o["label"].lower()), None)
        if not match:
            return ToolResult(False, f"No option matching {option!r}. Options: {[o['label'] for o in options]}",
                              "select failed: no such option")
        el.select_option(value=match["value"])
        return self._after(f"select {match['label']!r} in {self._label(element)}")

    def fill_form(self, fields: list) -> ToolResult:
        """Grouped action (cf. OSWorld-Human): fill several fields from one observation in a single step,
        then observe once. Does not submit — submitting stays a separate, policy-checked click."""
        self.start()
        done, errors = [], []
        for f in fields:
            try:
                el = self._el(f["element"])
                value = str(f["value"])
                if el.evaluate("e => e.tagName.toLowerCase()") == "select":
                    options = el.evaluate("e => Array.from(e.options).map(o => ({label: o.text.trim(), value: o.value}))")
                    match = next((o for o in options if o["label"].lower() == value.lower() or o["value"] == value), None) \
                        or next((o for o in options if value.lower() in o["label"].lower()), None)
                    if not match:
                        raise LookupError(f"no option {value!r}; options: {[o['label'] for o in options]}")
                    el.select_option(value=match["value"])
                else:
                    el.fill(value, timeout=8000)
                done.append(f"{self._label(f['element'])}={value!r}")
            except Exception as e:
                errors.append(f"element {f.get('element')}: {e}")
        result = self._after(f"fill {len(done)} field(s): " + "; ".join(done))
        if errors:
            result.ok = False
            result.content = "SOME FIELDS FAILED: " + " | ".join(errors) + "\n" + result.content
        return result

    def fill_secret(self, element: int, secret_name: str) -> ToolResult:
        """Credentials are typed by the tool; the model only names which secret to use."""
        self.start()
        if secret_name not in config.allowed_secret_names():
            return ToolResult(False, f"Unknown secret '{secret_name}'. Known: {sorted(config.allowed_secret_names())}",
                              "unknown secret")
        value = config.secret(secret_name)
        self.ctx.secret_values.add(value)
        self._el(element).fill(value, timeout=8000)
        return self._after(f"fill secret {secret_name} into {self._label(element)}")

    def press_key(self, key: str, element: int | None = None) -> ToolResult:
        self.start()
        if element is not None and key == "Enter":
            blocked = self._preflight(int(element), via_enter=True)
            if blocked:
                return blocked
        self.last_status = None
        if element is not None:
            self._el(element).press(key)
        else:
            self.page.keyboard.press(key)
        return self._evidence(self._after(f"press {key}"))

    def go_back(self) -> ToolResult:
        self.start()
        self.page.go_back(wait_until="domcontentloaded")
        return self._after("go back")

    def screenshot(self, name: str) -> str:
        self.start()
        path = self.ctx.store.dir / "screens" / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return str(path)

    def _label(self, element: int) -> str:
        if self.last_obs:
            m = re.search(rf"\[{int(element)} [^\]]*\]", self.last_obs["text"])
            if m:
                return m.group(0)
        return f"[{element}]"


def _enrich(req: WriteRequest) -> dict:
    """Give the human approver the business context, not just a URL."""
    from opsagent.policy import _erp_bill
    m = re.search(r"/erp/bills/([^/]+)/", req.path)
    if m:
        bill = _erp_bill(m.group(1))
        if bill:
            return {"bill": bill["id"], "vendor_id": bill["vendor_id"], "invoice": bill["invoice_number"],
                    "amount_inr": f"{bill['amount']:,.2f}", "due_date": bill["due_date"]}
    return {}


def browser_tools(ctx: RunContext, read_only: bool = False, share: Browser | None = None) -> list[Tool]:
    def b() -> Browser:
        if ctx.browser is None:
            ctx.browser = Browser(ctx, read_only=read_only, share=share)
        return ctx.browser

    tools = [
        Tool("open_url", "Open a URL of a company system in the browser (paths like /erp/bills are relative to the "
             "company intranet). Returns the page as text with numbered interactive elements.",
             params(["url"], url=("string", "absolute URL or path")), lambda url: b().open_url(url), "read"),
        Tool("read_page", "Re-read the current page (fresh element ids).", params(), lambda: b().read_page(), "read"),
        Tool("click", "Click an element by its id from the latest observation. Links with (download) save the file "
             "to the workspace.", params(["element"], element=("integer", "element id")),
             lambda element: b().click(element), "write"),
    ]
    if not read_only:
        tools += [
            Tool("type_text", "Replace the value of a text field (or choose an option of a select) by element id.",
                 params(["element", "text"], element=("integer", "element id"), text=("string", "value to enter"),
                        press_enter=("boolean", "press Enter afterwards (submits the form)")),
                 lambda element, text, press_enter=False: b().type_text(element, text, press_enter), "write"),
            Tool("fill_form", "Fill several fields of the current page in one step (text inputs, dates, textareas "
                 "and selects by option label). Prefer this over one-field-at-a-time typing. Does not submit.",
                 params(["fields"], fields={"type": "array", "description": "fields to fill", "items": {
                     "type": "object", "required": ["element", "value"],
                     "properties": {"element": {"type": "integer"}, "value": {"type": "string"}}}}),
                 lambda fields: b().fill_form(fields), "write"),
            Tool("select_option", "Choose an option in a select/dropdown by its visible label.",
                 params(["element", "option"], element=("integer", "element id"), option=("string", "option label")),
                 lambda element, option: b().select_option(element, option), "write"),
            Tool("fill_secret", "Type a stored credential into a field without revealing it. Use for usernames and "
                 "passwords on login pages.",
                 params(["element", "secret_name"], element=("integer", "element id"),
                        secret_name=("string", "secret name from the systems registry, e.g. ERP_PASSWORD")),
                 lambda element, secret_name: b().fill_secret(element, secret_name), "write"),
            Tool("press_key", "Press a keyboard key (e.g. Enter, Tab, Escape), optionally focused on an element.",
                 params(["key"], key=("string", "key name"), element=("integer", "element id (optional)")),
                 lambda key, element=None: b().press_key(key, element), "write"),
            Tool("go_back", "Browser back.", params(), lambda: b().go_back(), "read"),
        ]
    return tools
