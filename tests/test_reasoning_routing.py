"""Reasoning depth is routed by generic signals: deep to interpret/decide, light to carry out a decided step."""
from opsagent import config
from opsagent.human import ScriptedChannel
from opsagent.runtime import Operator
from opsagent.tools.base import ToolResult


def _op(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    op = Operator("test request", human=ScriptedChannel(), run_id="route")
    op.toolbox.call = lambda name, args: op._next_result   # no browser: feed results directly
    return op


def test_depth_signals(tmp_path, monkeypatch):
    monkeypatch.delenv("REASONING_ROUTING", raising=False)
    op = _op(tmp_path, monkeypatch)
    assert op._depth() == ("deep", "start")

    def act(result, name="click"):
        op.depth_signals = []
        op._next_result = result
        op._execute({"id": "t", "name": name, "arguments": "{}"})
        return op._depth()[0]

    page = lambda url: ToolResult(True, "page", untrusted_source=f"browser:{url}", data={"url": url})
    assert act(page("http://erp/bills")) == "deep"          # unseen page: something new to interpret
    assert act(page("http://erp/bills")) == "light"         # same page again: carrying out the plan
    assert act(ToolResult(False, "boom")) == "deep"         # failure
    assert act(ToolResult(True, "pdf text", untrusted_source="file:inv.pdf"), "read_file") == "deep"   # new document
    assert act(ToolResult(True, "SOP text"), "read_company_doc") == "deep"   # trusted, but new information
    assert act(ToolResult(True, "noted"), "note") == "light"                # agent's own bookkeeping
    monkeypatch.setenv("REASONING_ROUTING", "0")
    assert act(page("http://erp/bills")) == "deep"          # ablation baseline: always deep
