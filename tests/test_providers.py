"""Provider conversations without network calls: history rules per provider."""

import copy
import json
from types import SimpleNamespace as NS

from opsagent.llm import LLM, Usage, price_multiplier
from opsagent.providers import (CLEAR_TRIGGER_CHARS, AnthropicConversation, ModelSpec, OpenAIConversation,
                                ToolResultIn)

TOOLS = [{"type": "function", "function": {"name": "click", "description": "click", "parameters": {
    "type": "object", "properties": {"element": {"type": "integer"}}, "required": ["element"]}}}]


class Block(NS):
    def to_dict(self):
        return dict(self.__dict__)


class FakeAnthropic:
    def __init__(self):
        self.requests = []
        self.n = 0

    def create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        self.n += 1
        return NS(stop_reason="tool_use", stop_details=None, content=[
            Block(type="thinking", thinking="", signature=f"sig{self.n}"),
            Block(type="tool_use", id=f"tu{self.n}", name="click", input={"element": self.n})])

    _text = staticmethod(lambda resp: "")


def test_anthropic_history_is_append_only():
    client = FakeAnthropic()
    conv = AnthropicConversation(client, ModelSpec("anthropic", "claude-sonnet-5-5", "medium"), "SYS", TOOLS)
    conv.add_user("do the task")
    for i in range(4):
        reply = conv.step(f"<working_state step={i}>")
        assert reply["tool_calls"][0]["name"] == "click"
        conv.add_tool_results([ToolResultIn(c["id"], "x" * 40_000, "clicked") for c in reply["tool_calls"]])
    # every request's messages must be a strict prefix of the next one (nothing edited or removed)
    for a, b in zip(client.requests, client.requests[1:]):
        assert b["messages"][:len(a["messages"])] == a["messages"]
        assert a["system"] == b["system"] and a["tools"] == b["tools"]
    last = client.requests[-1]
    assert last["context_management"]["edits"][0]["type"] == "clear_tool_uses_20250919"  # server-side clearing
    assert any(m["role"] == "system" for m in last["messages"])  # working state as appended system messages
    assert last["messages"][2]["content"][0]["signature"] == "sig1"  # thinking blocks passed back unchanged


class FakeOpenAI:
    def __init__(self):
        self.requests = []

    def create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        n = len(self.requests)
        msg = NS(content="", reasoning_content="why", tool_calls=[
            NS(id=f"c{n}", function=NS(name="click", arguments=json.dumps({"element": n})))])
        return NS(choices=[NS(message=msg)])


def test_openai_batch_clearing_keeps_prefix_stable_between_clears():
    client = FakeOpenAI()
    conv = OpenAIConversation(client, ModelSpec("deepseek", "deepseek-v4-pro"), "SYS", TOOLS)
    conv.add_user("task")
    big = "y" * (CLEAR_TRIGGER_CHARS // 4)
    for _ in range(8):
        reply = conv.step("<ws>")
        conv.add_tool_results([ToolResultIn(c["id"], big, "did a thing") for c in reply["tool_calls"]])
    reqs = [r["messages"] for r in client.requests]
    cleared_at = [i for i, m in enumerate(reqs) if any("[older result cleared]" in (x.get("content") or "") for x in m)]
    assert cleared_at, "should have cleared once output grew past the trigger"
    first = cleared_at[0]
    # after the clear, the next request extends the same prefix (minus the transient working state at the end)
    assert reqs[first + 1][:len(reqs[first]) - 1] == reqs[first][:-1]
    assert all(m.get("reasoning_content") == "why" for m in conv.messages if m["role"] == "assistant")


def test_models_yaml_profiles_and_pricing():
    llm = LLM("claude")
    assert llm.describe()["worker"] == "anthropic:claude-sonnet-5-5"
    assert llm.describe()["verifier"].startswith("deepseek")  # different family audits the worker
    u = Usage()
    u.add_anthropic(NS(cache_read_input_tokens=1_000_000, input_tokens=0, cache_creation_input_tokens=0,
                       output_tokens=0), "claude-sonnet-5-5")
    assert abs(u.cost_usd - 0.20) < 1e-9
    assert price_multiplier("claude-sonnet-5-5") == 1.0


def test_reasoning_depth_maps_per_provider():
    from opsagent.providers import ModelSpec, _depth_kwargs
    ds, cl = ModelSpec("deepseek", "deepseek-v4-pro"), ModelSpec("anthropic", "claude-sonnet-5-5", effort="medium")
    assert _depth_kwargs("deepseek", ds, "light") == {"reasoning_effort": "low"}
    assert _depth_kwargs("deepseek", ds, "deep") == {}          # provider default (high)
    assert _depth_kwargs("anthropic", cl, "light") == {"output_config": {"effort": "low"}}
    assert _depth_kwargs("anthropic", cl, "deep") == {"output_config": {"effort": "medium"}}


def test_latency_breakdown_from_spans_and_calls(tmp_path, monkeypatch):
    from opsagent import config
    from opsagent.report import latency_breakdown
    from opsagent.store import RunStore
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    store = RunStore("lat")
    with store.span("contract"):
        pass
    store.log("llm", depth="deep", ms=4000)
    store.log("llm", depth="light", ms=1500)
    store.log("tool", ms=300)
    store.log("verify_llm", depth="deep", ms=2000)
    lat = latency_breakdown(store.events(), total_seconds=10)
    assert lat["worker_model"] == 5.5 and lat["worker_tools"] == 0.3 and lat["verify_model"] == 2.0
    assert lat["turns_deep"] == 1 and lat["turns_light"] == 1 and lat["other"] >= 2.0
    assert store.counts["llm"] == 2
