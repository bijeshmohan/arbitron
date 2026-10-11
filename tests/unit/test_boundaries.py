"""Architectural boundaries (plan §3.3).

Each check runs in a fresh interpreter so modules imported by other tests
cannot mask a forbidden import.
"""

import subprocess
import sys
import textwrap

import pytest

_CHECK = textwrap.dedent(
    """
    import importlib, pkgutil, sys

    preloaded = set(sys.modules)  # interpreter startup (e.g. sitecustomize)
    package = importlib.import_module({package!r})
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(info.name)

    allowed_internal = {allowed!r}
    bad = []
    for name in sorted(set(sys.modules) - preloaded):
        top = name.split(".")[0]
        if top == "arbitron":
            if name != "arbitron" and not name.startswith(allowed_internal):
                bad.append(name)
        elif top not in sys.stdlib_module_names and not top.startswith("_"):
            bad.append(name)
    print("\\n".join(bad))
    """
)


@pytest.mark.parametrize(
    ("package", "allowed"),
    [
        ("arbitron.domain", ("arbitron.domain",)),
        ("arbitron.ports", ("arbitron.ports", "arbitron.domain")),
    ],
)
def test_package_imports_only_stdlib_and_allowed_internals(
    package: str, allowed: tuple[str, ...]
) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, "-c", _CHECK.format(package=package, allowed=allowed)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "", f"{package} imported forbidden modules:\n{result.stdout}"
