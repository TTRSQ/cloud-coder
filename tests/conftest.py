import pytest


def pytest_addoption(parser):
    parser.addoption("--run-e2e", action="store_true", help="run tests against real GCP")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-e2e"):
        return
    skip = pytest.mark.skip(reason="e2e: pass --run-e2e to run against real GCP")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)
