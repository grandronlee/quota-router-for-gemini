import pytest

from helpers import Env, make_config


@pytest.fixture
def env() -> Env:
    return Env(make_config())
