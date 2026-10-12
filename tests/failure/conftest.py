"""Failure-injection tests (docs/failure-matrix.md) reuse the integration database fixtures."""

import pytest

from tests.integration.conftest import (
    database_url,
    engine,
    postgres_url,
    uow,
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/failure" in str(item.path):
            item.add_marker(pytest.mark.integration)


__all__ = ["database_url", "engine", "postgres_url", "uow"]
