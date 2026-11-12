"""Pytest glue for the offline agent harness (see offline_env.py).

`offline_env` is imported first so moto, the AWS hooks and the local STAC API are
active before any `geospatial` / `geospatial_agent` module is imported by the tests.
"""
import pytest

import offline_env


def pytest_unconfigure(config):
    offline_env.stop()


@pytest.fixture
def aws_calls():
    offline_env.AWS_CALLS.clear()
    return offline_env.AWS_CALLS


@pytest.fixture
def stac_searches():
    offline_env.STAC_SEARCHES.clear()
    return offline_env.STAC_SEARCHES
