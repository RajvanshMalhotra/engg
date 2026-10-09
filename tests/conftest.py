"""Spin up an isolated sandbox company on a separate port + state dir for the test session."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

PORT = 8799
TMP = Path(__file__).parent / ".state"
os.environ["SANDBOX_URL"] = f"http://127.0.0.1:{PORT}"
os.environ["RUNS_DIR"] = str(TMP / "runs")


@pytest.fixture(scope="session", autouse=True)
def sandbox_server():
    env = {**os.environ, "SANDBOX_STATE_DIR": str(TMP / "sandbox")}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "sandbox.app:app", "--port", str(PORT),
                             "--log-level", "warning"], env=env, cwd=Path(__file__).parent.parent)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{PORT}"
    proc.terminate()
    proc.wait()


@pytest.fixture
def sandbox(sandbox_server):
    httpx.post(f"{sandbox_server}/__admin/reset", timeout=30).raise_for_status()
    return sandbox_server
