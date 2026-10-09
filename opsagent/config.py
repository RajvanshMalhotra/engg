"""Paths, environment and the company system registry."""

import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

COMPANY_DIR = Path(os.getenv("COMPANY_DIR", ROOT / "company"))
KNOWLEDGE_DIR = COMPANY_DIR / "knowledge"
MEMORY_DIR = COMPANY_DIR / "memory"
RUNS_DIR = Path(os.getenv("RUNS_DIR", ROOT / "runs"))

SANDBOX_URL = os.getenv("SANDBOX_URL", "http://127.0.0.1:8765").rstrip("/")
PLANNER_MODEL = os.getenv("PLANNER_MODEL", "deepseek-v4-pro")
VISION_MODEL = os.getenv("VISION_MODEL", "deepseek-flash")


def _expand(value):
    """Substitute ${VAR} from the environment inside registry strings."""
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def load_systems() -> dict:
    with open(COMPANY_DIR / "systems.yaml") as f:
        return _expand(yaml.safe_load(f))["systems"]


def load_policy() -> list[dict]:
    with open(COMPANY_DIR / "policy.yaml") as f:
        return yaml.safe_load(f)["rules"]


def secret(name: str) -> str:
    """Secrets are resolved only inside tools; they never enter the LLM context."""
    value = os.getenv(name)
    if value is None:
        raise KeyError(f"secret {name} is not configured")
    return value


def allowed_secret_names() -> set[str]:
    names = set()
    for sys in load_systems().values():
        login = sys.get("login") or {}
        names.update(v for k, v in login.items() if k.endswith("_secret"))
    return names
