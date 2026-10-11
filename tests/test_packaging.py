"""Packaging: a new template or script must not silently go missing from an install."""
from __future__ import annotations

import importlib
import json
from fnmatch import fnmatch
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")     # standard library from Python 3.11

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_every_template_and_static_file_is_packaged():
    patterns = PYPROJECT["tool"]["setuptools"]["package-data"]["web"]
    web = ROOT / "web"
    files = [p.relative_to(web).as_posix()
             for folder in ("templates", "static") for p in (web / folder).rglob("*") if p.is_file()]

    missing = [f for f in files if not any(fnmatch(f, pattern) for pattern in patterns)]
    assert missing == [], f"add these to [tool.setuptools.package-data]: {missing}"


def test_every_python_package_is_listed():
    listed = set(PYPROJECT["tool"]["setuptools"]["packages"])
    found = {p.parent.relative_to(ROOT).as_posix().replace("/", ".")
             for top in ("agent", "web") for p in (ROOT / top).rglob("__init__.py")}
    assert found - listed == set(), "a new package folder is missing from pyproject.toml"


def test_version_has_one_source():
    assert "version" in PYPROJECT["project"]["dynamic"]
    assert PYPROJECT["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "agent.__version__"


@pytest.mark.parametrize("name", ["postcadence", "postcadence-web"])
def test_commands_point_at_real_functions(name):
    module, func = PYPROJECT["project"]["scripts"][name].split(":")
    assert callable(getattr(importlib.import_module(module), func))


def test_the_data_location_can_be_moved(monkeypatch, tmp_path):
    """`pip install .` puts the package in site-packages, where data must not be
    written and .github/ does not exist. An installed copy has to be able to
    point at the real checkout."""
    import importlib

    monkeypatch.setenv("POSTCADENCE_HOME", str(tmp_path))
    from agent import paths
    importlib.reload(paths)
    try:
        assert paths.ROOT == tmp_path
        assert paths.CONFIG_FILE == tmp_path / "data" / "config.json"
    finally:
        monkeypatch.delenv("POSTCADENCE_HOME")
        importlib.reload(paths)


def test_a_fork_has_a_clean_starting_point():
    """The data files are committed because Actions reads them - which means a
    fork inherits somebody else's schedule and topics unless it is told."""
    repo = Path(__file__).resolve().parent.parent      # the real one, not the sandbox

    for name in ("config", "topics", "state"):
        example = repo / "data" / f"{name}.example.json"
        assert example.exists(), f"data/{name}.example.json is missing"
        assert json.loads(example.read_text(encoding="utf-8"))

    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert "config.example.json" in readme
