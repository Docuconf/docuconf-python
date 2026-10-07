"""The README's Django recipe (tests/django_project), run as Django runs it: through manage.py."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("django")

HERE = Path(__file__).parent
PROJECT = HERE / "django_project"
README = (HERE.parent / "README.md").read_text()
GOOD = {"DJANGO_SECRET_KEY": "a-secret-key-of-twenty-chars", "DJANGO_DATABASE_URL": "sqlite:///db.sqlite3"}


def run(project: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_")}
    base["PYTHONPATH"] = os.pathsep.join([str(project), str(HERE.parent / "src")])
    return subprocess.run(
        [sys.executable, "manage.py", *args],
        cwd=project,
        env={**base, **env, "DOCUCONF_TERMINATION_LOG": str(project / "termination-log")},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "django_project"
    shutil.copytree(PROJECT, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def readme_block(header: str) -> str:
    m = re.search(rf"```python\n{re.escape(header)}\n(.*?)```", README, re.S)
    assert m, f"README has no python block headed {header!r}"
    return m.group(1)


def test_project_matches_the_readme() -> None:
    config = (PROJECT / "mysite/config.py").read_text()
    assert config.split('"""\n\n', 1)[1] == readme_block("# mysite/config.py")
    assert readme_block("# mysite/settings.py (excerpt)") in (PROJECT / "mysite/settings.py").read_text()


def test_manage_py_check(project: Path) -> None:
    r = run(project, GOOD, "check")
    assert r.returncode == 0, r.stdout + r.stderr


def test_bad_environment_exits_1_without_a_traceback(project: Path) -> None:
    r = run(project, {"DJANGO_SECRET_KEY": "short", "DJANGO_DEBUG": "maybe"}, "check")
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert r.stderr.startswith("docuconf: 3 configuration problems:\n"), r.stderr
    assert "DJANGO_DATABASE_URL [missing_required]" in r.stderr
    assert "DJANGO_DEBUG [invalid_type]" in r.stderr
    assert "DJANGO_SECRET_KEY [out_of_range]" in r.stderr
    assert "short" not in r.stderr  # a secret
    assert (project / "termination-log").read_text() == r.stderr.rstrip("\n")


def test_collectstatic_with_the_readme_placeholders(project: Path) -> None:
    line = next(ln for ln in README.splitlines() if "manage.py collectstatic" in ln)
    words = line.split()
    i = words.index("python")
    env = dict(a.split("=", 1) for a in words[:i])
    args = words[i + 2 :]  # after python manage.py
    r = run(project, env, *args)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "static files copied" in r.stdout


def test_export(project: Path) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "docuconf", "export", "mysite.config:Env"],
        cwd=project,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(HERE.parent / "src")},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "DJANGO_SECRET_KEY" in r.stdout and "package mysite" in r.stdout
