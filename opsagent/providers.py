"""Provider adapters. Each owns a *conversation* in its provider's native format and its own history rules.

Why conversations, not a shared message list: providers disagree about history.
  * DeepSeek (OpenAI protocol) requires `reasoning_content` echoed back with tools, and lets us rewrite old tool
    results — we batch-clear them to save tokens while keeping the cached prefix stable.
  * Claude (Anthropic protocol) binds thinking blocks to the exact conversation prefix ("preserved thinking"):
    history must be append-only. Old tool results are cleared *server-side* (context editing), and the per-turn
    working state is appended as a mid-conversation system message instead of being injected and removed.
The runtime only sees: add_user / add_tool_results / step(working_state) / state().
"""

import base64
import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

KEEP_FULL_RESULTS = 3          # newest tool results always kept verbatim (OpenAI-protocol clearing)
CLEAR_TRIGGER_CHARS = 60_000   # clear older results in one batch once uncleared output exceeds this


@dataclass
class ModelSpec:
    provider: str              # deepseek | anthropic
    model: str
    effort: str | None = None  # anthropic output_config.effort


def _depth_kwargs(provider: str, spec: ModelSpec, depth: str) -> dict:
    """Reasoning depth chosen by the harness: "deep" (decide/interpret) or "light" (carry out a decided step).
    DeepSeek V4 ignores reasoning_effort=low (measured: same thinking time), so light = thinking off for that call
    (~1.4 s vs ~4-6 s per turn). Mixing modes in one tool conversation is valid as long as every assistant message
    carries reasoning_content (empty for non-thinking turns) — see OpenAIConversation.step."""
    if provider == "deepseek":
        return {"extra_body": {"thinking": {"type": "disabled"}}} if depth == "light" else {}
    return {"output_config": {"effort": "low" if depth == "light" else (spec.effort or "medium")}}


def _hedged(fn, after: float | None, usage):
    """Tail-latency hedge (Dean & Barroso, "The Tail at Scale"): if the request hasn't answered within `after`
    seconds, send an identical second one and take whichever finishes first. Only for single stateless calls on the
    critical path; the loser finishes in the background and is still metered."""
    if not after:
        return fn()
    pool = ThreadPoolExecutor(2)
    try:
        first = pool.submit(fn)
        if wait([first], timeout=after).done:
            return first.result()
        if usage is not None:
            usage.hedged += 1
        second = pool.submit(fn)
        done, _ = wait([first, second], return_when=FIRST_COMPLETED)
        winner = done.pop()
        try:
            return winner.result()
        except Exception:
            return (second if winner is first else first).result()
    finally:
        pool.shutdown(wait=False)


HEDGE_TURN_SECONDS = float(os.getenv("HEDGE_TURN_SECONDS", "30"))  # typical turn 2-15 s; tails seen at 60-160 s


@dataclass
class ToolResultIn:
    id: str
    content: str
    summary: str
    is_error: bool = False


def _retry(fn, retryable: tuple, is_server_error, attempts: int = 5, on_wait=lambda s: None):
    delay, err = 2.0, None
    for _ in range(attempts):
        try:
            return fn()
        except retryable as e:
            err = e
        except Exception as e:
            if not is_server_error(e):
                raise
            err = e
        time.sleep(delay)
        on_wait(delay)
        delay *= 2
    raise RuntimeError(f"LLM call failed after retries: {err}")


# ============================================================================ DeepSeek / OpenAI-compatible

class OpenAICompatClient:
    def __init__(self, usage, guard=lambda: None):
        from openai import OpenAI
        self.usage, self.guard = usage, guard  # guard() runs before every request (cost cap)
        self.client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"),
                             base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                             timeout=180, max_retries=0)

    def create(self, **kwargs):
        from openai import APIConnectionError, APIStatusError, RateLimitError
        self.guard()
        t0 = time.perf_counter()
        resp = _retry(lambda: self.client.chat.completions.create(**kwargs), (RateLimitError, APIConnectionError),
                      lambda e: isinstance(e, APIStatusError) and e.status_code >= 500, on_wait=self.usage.add_wait)
        self.usage.add_openai(resp.usage, kwargs["model"])
        self.usage.add_latency(kwargs["model"], time.perf_counter() - t0)
        return resp

    def json(self, spec: ModelSpec, system: str, user: str, depth: str = "deep") -> str:
        resp = self.create(model=spec.model, response_format={"type": "json_object"}, **_depth_kwargs("deepseek", spec, depth),
                           messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return resp.choices[0].message.content or ""

    def look(self, spec: ModelSpec, png: bytes, question: str) -> str:
        b64 = base64.b64encode(png).decode()
        resp = self.create(model=spec.model, messages=[{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}])
        return resp.choices[0].message.content or ""


class OpenAIConversation:
    def __init__(self, client: OpenAICompatClient, spec: ModelSpec, system: str, tools: list[dict], state=None):
        self.client, self.spec, self.tools = client, spec, tools
        state = state or {}
        self.messages: list[dict] = state.get("messages") or [{"role": "system", "content": system}]
        self.summaries: dict[str, str] = state.get("summaries", {})
        self.cleared: set[str] = set(state.get("cleared", []))
        self.on_clear = None

    def state(self) -> dict:
        return {"messages": self.messages, "summaries": self.summaries, "cleared": sorted(self.cleared)}

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_tool_results(self, results: list[ToolResultIn]) -> None:
        for r in results:
            self.summaries[r.id] = r.summary
            self.messages.append({"role": "tool", "tool_call_id": r.id, "content": r.content})

    def _request(self, working_state: str | None) -> list[dict]:
        """Old tool results are cleared in batches, not every turn: rewriting an old message changes the request
        prefix and discards the prompt cache from that point. The working state rides on the last message only."""
        live = [m for m in self.messages if m["role"] == "tool" and m["tool_call_id"] not in self.cleared]
        if sum(len(m["content"]) for m in live) > CLEAR_TRIGGER_CHARS:
            self.cleared.update(m["tool_call_id"] for m in live[:-KEEP_FULL_RESULTS])
            if self.on_clear:
                self.on_clear(len(self.cleared))
        out = []
        for m in self.messages:
            if m["role"] == "tool" and m["tool_call_id"] in self.cleared:
                m = {**m, "content": f"[older result cleared] {self.summaries.get(m['tool_call_id'], '')}"}
            out.append(m)
        if working_state:
            if out[-1]["role"] == "user":
                out[-1] = {**out[-1], "content": out[-1]["content"] + "\n\n" + working_state}
            else:
                out.append({"role": "user", "content": working_state})
        return out

    def step(self, working_state: str | None = None, depth: str = "deep") -> dict:
        kwargs = dict(model=self.spec.model, messages=self._request(working_state), **_depth_kwargs("deepseek", self.spec, depth))
        if self.tools:
            kwargs["tools"] = self.tools
        # The request is complete before sending and history is only appended after a reply, so a slow turn can be
        # hedged with an identical duplicate.
        msg = _hedged(lambda: self.client.create(**kwargs), HEDGE_TURN_SECONDS, getattr(self.client, "usage", None)).choices[0].message
        out = {"role": "assistant", "content": msg.content or ""}
        reasoning = getattr(msg, "reasoning_content", None) or ""
        # Must be echoed back on later turns when tools are used — on every assistant message, including turns run
        # with thinking off (empty string), or the next thinking turn is rejected with a 400.
        out["reasoning_content"] = reasoning
        calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                 for tc in (msg.tool_calls or [])]
        if calls:
            out["tool_calls"] = [{"id": c["id"], "type": "function",
                                  "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]
        self.messages.append(out)
        return {"text": out["content"], "reasoning": reasoning or "", "tool_calls": calls}


# ============================================================================ Anthropic (Claude)

ANTHROPIC_BETAS = ["context-management-2025-06-27", "server-side-fallback-2026-07-01"]


class AnthropicClient:
    def __init__(self, usage, guard=lambda: None):
        import anthropic
        self.usage, self.guard = usage, guard
        self.client = anthropic.Anthropic(max_retries=0, timeout=300)

    def create(self, **kwargs):
        import anthropic
        self.guard()
        t0 = time.perf_counter()
        resp = _retry(lambda: self.client.beta.messages.create(**kwargs),
                      (anthropic.RateLimitError, anthropic.APIConnectionError),
                      lambda e: isinstance(e, anthropic.APIStatusError) and e.status_code >= 500,
                      on_wait=self.usage.add_wait)
        self.usage.add_anthropic(resp.usage, kwargs["model"])
        self.usage.add_latency(kwargs["model"], time.perf_counter() - t0)
        return resp

    @staticmethod
    def _text(resp) -> str:
        if resp.stop_reason == "refusal":
            cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            return f"[model declined this request: {cat or 'unspecified'}]"
        return "".join(b.text for b in resp.content if b.type == "text")

    def json(self, spec: ModelSpec, system: str, user: str, depth: str = "deep") -> str:
        resp = self.create(model=spec.model, max_tokens=16000, betas=ANTHROPIC_BETAS, fallbacks="default",
                           output_config={"effort": "low" if depth == "light" else (spec.effort or "low")},
                           system=system + "\n\nReply with a single JSON object and nothing else.",
                           messages=[{"role": "user", "content": user}])
        text = self._text(resp).strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        return text

    def look(self, spec: ModelSpec, png: bytes, question: str) -> str:
        resp = self.create(model=spec.model, max_tokens=4000, betas=ANTHROPIC_BETAS, fallbacks="default",
                           output_config={"effort": "low"},
                           messages=[{"role": "user", "content": [
                               {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                            "data": base64.b64encode(png).decode()}},
                               {"type": "text", "text": question}]}])
        return self._text(resp)


class AnthropicConversation:
    """Append-only history. Nothing already sent is ever edited or removed."""

    def __init__(self, client: AnthropicClient, spec: ModelSpec, system: str, tools: list[dict], state=None):
        self.client, self.spec = client, spec
        self.system = system  # never rebuilt mid-conversation (would invalidate thinking blocks)
        self.tools = [{"name": t["function"]["name"], "description": t["function"]["description"],
                       "input_schema": t["function"]["parameters"]} for t in tools]
        self.messages: list[dict] = (state or {}).get("messages", [])
        self.on_clear = None

    def state(self) -> dict:
        return {"messages": self.messages}

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})

    def add_tool_results(self, results: list[ToolResultIn]) -> None:
        # all results of one turn go back in a single user message
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.id, "content": r.content, "is_error": r.is_error}
            for r in results]})

    def step(self, working_state: str | None = None, depth: str = "deep") -> dict:
        if working_state:
            # Operator channel, appended and kept (earlier copies stay in place) — append-only.
            self.messages.append({"role": "system", "content": working_state})
        kwargs = dict(model=self.spec.model, max_tokens=16000, betas=ANTHROPIC_BETAS, fallbacks="default",
                      system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                      messages=self.messages, cache_control={"type": "ephemeral"},
                      **_depth_kwargs("anthropic", self.spec, depth),
                      context_management={"edits": [{"type": "clear_tool_uses_20250919"}]})
        if self.tools:
            kwargs["tools"] = self.tools
        resp = self.client.create(**kwargs)
        # pass every block back unchanged (thinking blocks carry signatures bound to this exact prefix)
        self.messages.append({"role": "assistant", "content": [b.to_dict() for b in resp.content]})
        calls = [{"id": b.id, "name": b.name, "arguments": json.dumps(b.input)}
                 for b in resp.content if b.type == "tool_use"]
        thinking = "".join(getattr(b, "thinking", "") or "" for b in resp.content if b.type == "thinking")
        return {"text": self.client._text(resp), "reasoning": thinking, "tool_calls": calls}
