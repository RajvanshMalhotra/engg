"""Everything a tool needs during a run, passed explicitly (no globals)."""

from dataclasses import dataclass, field

from opsagent.human import HumanChannel
from opsagent.llm import LLM
from opsagent.policy import Policy
from opsagent.store import RunStore


@dataclass
class RunContext:
    store: RunStore
    llm: LLM
    policy: Policy
    human: HumanChannel
    headless: bool = True
    actor: str = "worker"                     # worker | verifier
    notes: dict = field(default_factory=dict)  # working memory: key -> {value, source}
    secret_values: set = field(default_factory=set)
    declined: dict = field(default_factory=dict)  # (rule, method, path) -> who declined, when, note — never re-asked
    browser: object = None                    # lazily started opsagent.tools.browser.Browser

    def redact(self, text: str) -> str:
        for s in self.secret_values:
            if s and len(s) >= 4:
                text = text.replace(s, "•••")
        return text
