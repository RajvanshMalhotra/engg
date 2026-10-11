"""Model layer: role-based routing over provider adapters, with one cost meter and a cost cap.

Roles (configured in models.yaml, profile chosen with MODEL_PROFILE):
  worker    the operator's agent loop
  verifier  the independent auditor (ideally a different model family from the worker)
  utility   contract drafting, reflection — short structured-output calls
  vision    reading scans and screenshots

Nothing outside this module and providers.py knows which vendor or model is in use.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import yaml

from opsagent import config
from opsagent.providers import (AnthropicClient, AnthropicConversation, ModelSpec, OpenAICompatClient,
                                OpenAIConversation)

# USD per 1M tokens: (cache-read input, uncached input, output, cache-write input).
# DeepSeek: api-docs.deepseek.com/quick_start/pricing (peak rates; off-peak is half). Claude: Anthropic list prices.
# Checked 2026-10-10.
PRICES = {
    "deepseek-v4-pro": (0.044, 1.32, 3.96, 1.32),
    "deepseek-flash": (0.006, 0.30, 1.20, 0.30),
    "claude-sonnet-5-5": (0.20, 2.00, 10.00, 2.50),
    "claude-opus-5-5": (0.20, 4.00, 20.00, 5.00),
    "claude-haiku-4-5": (0.10, 1.00, 5.00, 1.25),
}
DEFAULT_PRICE = (0.20, 4.00, 20.00, 5.00)  # unknown model: assume expensive
PEAK_UTC_HOURS = set(range(1, 4)) | set(range(6, 10))   # DeepSeek peak, Mon–Fri; otherwise half price


def price_multiplier(model: str = "deepseek", now: datetime | None = None) -> float:
    """A cost guard must never under-count: charge full price unless DEEPSEEK_OFFPEAK_DISCOUNT=1.
    (Assuming the off-peak discount made the meter read ~half the real spend — the account balance is the truth.)"""
    if not model.startswith("deepseek") or os.getenv("DEEPSEEK_OFFPEAK_DISCOUNT") != "1":
        return 1.0
    now = now or datetime.now(timezone.utc)
    return 1.0 if now.weekday() < 5 and now.hour in PEAK_UTC_HOURS else 0.5


def deepseek_balance() -> float | None:
    """Authoritative remaining DeepSeek balance in USD (free endpoint); None if unavailable."""
    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        return None
    try:
        r = httpx.get("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"}, timeout=15)
        infos = [b for b in r.json().get("balance_infos", []) if b.get("currency") == "USD"]
        return float(infos[0]["total_balance"]) if infos else None
    except Exception:
        return None


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict = field(default_factory=dict)
    model_seconds: dict = field(default_factory=dict)   # wall time inside model calls, per model (incl. retries)
    retry_wait_seconds: float = 0.0                       # of which: backoff sleeps after rate limits / 5xx

    def add_latency(self, model: str, seconds: float) -> None:
        self.model_seconds[model] = round(self.model_seconds.get(model, 0.0) + seconds, 2)

    def add_wait(self, seconds: float) -> None:
        self.retry_wait_seconds += seconds

    def _add(self, model: str, cached: int, uncached: int, written: int, completion: int) -> None:
        p_hit, p_miss, p_out, p_write = PRICES.get(model, DEFAULT_PRICE)
        cost = price_multiplier(model) * (cached * p_hit + uncached * p_miss + written * p_write
                                          + completion * p_out) / 1e6
        self.calls += 1
        self.prompt_tokens += cached + uncached + written
        self.cached_tokens += cached
        self.completion_tokens += completion
        self.cost_usd += cost
        self.by_model[model] = round(self.by_model.get(model, 0.0) + cost, 5)

    def add_openai(self, u, model: str) -> None:
        if u:
            hit = getattr(u, "prompt_cache_hit_tokens", 0) or 0
            self._add(model, hit, (u.prompt_tokens or 0) - hit, 0, u.completion_tokens or 0)

    def add_anthropic(self, u, model: str) -> None:
        if u:
            self._add(model, u.cache_read_input_tokens or 0, u.input_tokens or 0,
                      u.cache_creation_input_tokens or 0, u.output_tokens or 0)


class BudgetExceeded(RuntimeError):
    pass


def load_profile(name: str | None = None) -> dict[str, ModelSpec]:
    name = name or os.getenv("MODEL_PROFILE", "deepseek")
    spec = yaml.safe_load((config.ROOT / "models.yaml").read_text())["profiles"][name]
    return {role: ModelSpec(**cfg) for role, cfg in spec.items()}


class LLM:
    def __init__(self, profile: str | None = None):
        self.roles = load_profile(profile)
        self.usage = Usage()
        self.max_cost_usd = float(os.getenv("MAX_COST_PER_RUN_USD", "0.50"))
        self._clients: dict = {}

    @property
    def vision_model(self) -> str:
        return self.roles["vision"].model

    def describe(self) -> dict:
        return {role: f"{s.provider}:{s.model}" for role, s in self.roles.items()}

    def check_budget(self) -> None:
        if self.usage.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded(f"run cost ${self.usage.cost_usd:.3f} reached the cap ${self.max_cost_usd:.2f}")

    def _client(self, provider: str):
        if provider not in self._clients:
            cls = {"deepseek": OpenAICompatClient, "anthropic": AnthropicClient}[provider]
            self._clients[provider] = cls(self.usage, guard=self.check_budget)
        return self._clients[provider]

    def conversation(self, role: str, system: str, tools: list[dict], state: dict | None = None):
        spec = self.roles[role]
        cls = {"deepseek": OpenAIConversation, "anthropic": AnthropicConversation}[spec.provider]
        return cls(self._client(spec.provider), spec, system, tools, state)

    def json(self, system: str, user: str, role: str = "utility", depth: str = "deep") -> dict:
        spec = self.roles[role]
        client = self._client(spec.provider)
        for _ in range(3):
            text = client.json(spec, system, user, depth)
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                user = user + "\n\n(Your previous reply was not valid JSON. Reply with only the JSON object.)"
        raise ValueError("model did not return valid JSON")

    def look(self, image_png: bytes, question: str) -> str:
        spec = self.roles["vision"]
        return self._client(spec.provider).look(spec, image_png, question)
