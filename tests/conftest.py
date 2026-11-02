import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from ghost_agent.agent import Agent
from ghost_agent.models import Router
from ghost_agent.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "home")


@pytest.fixture()
def agent(store):
    # Mock-only router: deterministic, no network, no key.
    return Agent(store, router=Router(prefer_real=False))
