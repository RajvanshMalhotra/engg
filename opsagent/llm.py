"""Thin model adapter. DeepSeek speaks the OpenAI chat-completions protocol, so swapping
providers means changing base_url/model — nothing else in the runtime knows which model it talks to."""

import base64
import json
import os
import time
from dataclasses import dataclass, field

from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

from opsagent import config


# USD per 1M tokens: (cache-hit input, cache-miss input, output). Peak-hour rates (the conservative bound)
# from api-docs.deepseek.com/quick_start/pricing, checked 2026-10-10. Override via PRICES_<MODEL> if they change.
PRICES = {
    "deepseek-v4-pro": (0.044, 1.32, 3.96),
    "deepseek-flash": (0.006, 0.30, 1.20),
}
DEFAULT_PRICE = (0.044, 1.32, 3.96)


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, u, model: str) -> None:
        if not u:
            return
        hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
        prompt, completion = u.prompt_tokens or 0, u.completion_tokens or 0
        p_hit, p_miss, p_out = PRICES.get(model, DEFAULT_PRICE)
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cached_tokens += hit
        self.cost_usd += (hit * p_hit + (prompt - hit) * p_miss + completion * p_out) / 1e6


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class LLM:
    model: str = field(default_factory=lambda: config.PLANNER_MODEL)
    vision_model: str = field(default_factory=lambda: config.VISION_MODEL)
    usage: Usage = field(default_factory=Usage)
    max_cost_usd: float = field(default_factory=lambda: float(os.getenv("MAX_COST_PER_RUN_USD", "0.75")))

    def __post_init__(self):
        self.client = OpenAI(api_key=os.getenv("DEEPSEEK_API_KEY"),
                             base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                             timeout=180, max_retries=0)

    def _call(self, **kwargs):
        if self.usage.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded(f"run cost ${self.usage.cost_usd:.3f} reached the cap ${self.max_cost_usd:.2f}")
        delay = 2.0
        for attempt in range(5):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                self.usage.add(resp.usage, kwargs["model"])
                return resp
            except (RateLimitError, APIConnectionError) as e:
                err = e
            except APIStatusError as e:
                if e.status_code < 500:
                    raise
                err = e
            time.sleep(delay)
            delay *= 2
        raise RuntimeError(f"LLM call failed after retries: {err}")

    def chat(self, messages: list[dict], tools: list[dict] | None = None, model: str | None = None):
        """One agent turn. Returns the assistant message as a dict ready to append to history —
        including reasoning_content, which DeepSeek requires to be passed back when tools are in play."""
        kwargs = dict(model=model or self.model, messages=messages)
        if tools:
            kwargs["tools"] = tools
        msg = self._call(**kwargs).choices[0].message
        out = {"role": "assistant", "content": msg.content or ""}
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            out["reasoning_content"] = reasoning
        if msg.tool_calls:
            out["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        return out

    def json(self, system: str, user: str, model: str | None = None) -> dict:
        """Structured output call (contract building, verification verdicts, reflection)."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in range(3):
            resp = self._call(model=model or self.model, messages=messages,
                              response_format={"type": "json_object"})
            text = resp.choices[0].message.content or ""
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": "That was not valid JSON. Reply with only the JSON object."}]
        raise ValueError("model did not return valid JSON")

    def look(self, image_png: bytes, question: str) -> str:
        """Vision: only the flash model accepts images."""
        b64 = base64.b64encode(image_png).decode()
        resp = self._call(model=self.vision_model, messages=[{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}])
        return resp.choices[0].message.content or ""
