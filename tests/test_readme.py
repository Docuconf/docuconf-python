"""The README's snippets, run.

Python blocks whose first line is a file name (``# orders/settings.py``)
become a small project in a temporary directory. The tests then run that
project as the README does: its tests, its entry point (checked against the
README's output), its export commands and the local development recipe. Every
other block is at least compiled or compared with the code it shows.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import docuconf

HERE = Path(__file__).parent
ROOT = HERE.parent
README = (ROOT / "README.md").read_text()
BLOCK = re.compile(r"^```(\w*)\n(.*?)^```", re.S | re.M)
HEADER = re.compile(r"^# ([\w/]+\.py)\n")


def blocks(lang: str) -> list[str]:
    return [body for kind, body in BLOCK.findall(README) if kind == lang]


def files() -> dict[str, str]:
    """The Python blocks headed with a file name, by that name."""
    out: dict[str, str] = {}
    for body in blocks("python"):
        m = HEADER.match(body)
        if m:
            out[m.group(1)] = body
    return out


def block_after(marker: str, lang: str) -> str:
    """The first ``lang`` block after the text ``marker``."""
    start = README.index(marker)
    m = BLOCK.search(README, start)
    while m is not None and m.group(1) != lang:
        m = BLOCK.search(README, m.end())
    assert m is not None, f"no {lang} block after {marker!r}"
    return m.group(2)


def env_for(project: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("ORDERS_", "DOCUCONF_", "DJANGO_", "CATALOG_"))}
    base["PYTHONPATH"] = os.pathsep.join([str(project), str(ROOT / "src")])
    base["DOCUCONF_TERMINATION_LOG"] = str(project / "termination-log")
    return {**base, **(extra or {})}


def run_shell_line(
    line: str, project: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a README command line: leading NAME=value words are environment, ``python``/``docuconf`` this Python."""
    words = shlex.split(line)
    env: dict[str, str] = {}
    while words and re.match(r"^[A-Z_][A-Z0-9_]*=", words[0]):
        k, v = words.pop(0).split("=", 1)
        env[k] = v
    if words[0] == "python":
        words[0] = sys.executable
    elif words[0] == "docuconf":
        words[:1] = [sys.executable, "-m", "docuconf"]
    return subprocess.run(
        words, cwd=project, env=env_for(project, {**env, **(extra_env or {})}), capture_output=True, text=True
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    for name, body in files().items():
        if name.startswith(("examples/", "mysite/")):
            continue  # compared with the repository's files instead
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return tmp_path


def test_every_python_block_compiles() -> None:
    all_blocks = blocks("python")
    assert len(all_blocks) >= 9
    for body in all_blocks:
        compile(body, "README.md", "exec")


def test_install_names_this_distribution_and_its_extras() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    name = re.search(r'^name = "(.+)"', pyproject, re.M)
    assert name is not None
    extras = set(
        re.findall(r"^(\w+) = \[", pyproject.split("[project.optional-dependencies]")[1].split("\n[")[0], re.M)
    )
    for line in blocks("sh")[0].splitlines():
        m = re.match(
            r'pip install "([\w-]+)(?:\[([\w,]+)\])? @ git\+https://github.com/docuconf/docuconf-python@main"$', line
        )
        assert m, line
        assert m.group(1) == name.group(1)
        assert set((m.group(2) or "").split(",")) - {""} <= extras


def test_examples_app_settings_is_the_readme_block() -> None:
    body = files()["examples/app/settings.py"]
    assert body.split("\n", 1)[1] == (ROOT / "examples/app/settings.py").read_text()


def test_quickstart_tests_pass(project: Path) -> None:
    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("httpx") is None:
        pytest.skip("fastapi and httpx are not installed")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=project,
        env=env_for(project),
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "3 passed" in r.stdout


def test_run_and_see_an_error(project: Path) -> None:
    run = block_after("## 3. Run", "sh").strip()
    r = run_shell_line(run, project)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "orders listening on :8080, timeout 0:00:30\n"

    bad = block_after("## 4. See an error", "sh").strip()
    r = run_shell_line(bad, project)
    assert r.returncode == 1
    assert r.stderr == block_after("## 4. See an error", "text")
    assert "Traceback" not in r.stderr

    # The typo warning quoted under it.
    r = run_shell_line(run.replace("ORDERS_DATABASE_URL", "ORDERS_LOG_LEVL=debug ORDERS_DATABASE_URL"), project)
    quoted = re.search(r"`(docuconf: ORDERS_LOG_LEVL [^`]+)`", README)
    assert quoted is not None and quoted.group(1) in r.stderr


def test_export_commands(project: Path) -> None:
    for line in block_after("## 6. Export the contract", "sh").splitlines():
        r = run_shell_line(line, project, {"GIT_SHA": "abc123"})
        assert r.returncode == 0, f"{line}: {r.stderr}"
    assert (project / "contract.cue").read_text().startswith("// Code generated by docuconf")


def test_local_development_recipe(tmp_path: Path) -> None:
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    for line in block_after("## Local development", "sh").splitlines():
        r = subprocess.run(line, shell=True, cwd=tmp_path, capture_output=True, text=True)
        assert r.returncode == 0, f"{line}: {r.stderr}"
    from examples.app.settings import Settings

    env = {"ORDERS_DATABASE_URL": "postgres://db/orders", "ORDERS_API_TOKEN": "t" * 20}
    with pytest.raises(docuconf.ConfigValidationError) as info:
        Settings.load(env=env)
    shown = block_after("## Local development", "text").strip()
    assert shown in str(info.value)
    settings = Settings.load(env=env, file_root=str(tmp_path / "dev"))
    assert settings.rates.per_minute == 60
    assert settings.tls.certificate.subject.rfc4514_string() == "CN=orders.internal"


def test_overlay_example(project: Path) -> None:
    sys.path.insert(0, str(project))
    try:
        from catalog.settings import Settings  # type: ignore[import-not-found]

        assert docuconf.load(Settings, env={"CATALOG_PAGE_SIZE": "7"}).page_size == 7
        assert "overlays" in docuconf.contract_data(Settings)
    finally:
        sys.path.remove(str(project))
        for mod in [m for m in sys.modules if m == "catalog" or m.startswith("catalog.")]:
            del sys.modules[mod]


def test_contract_first_example(project: Path) -> None:
    contract = {
        "apiVersion": "docuconf.dev/v1alpha1",
        "kind": "ConfigContract",
        "metadata": {"name": "svc"},
        "vars": {"PORT": {"type": "int", "description": "HTTP listen port"}},
    }
    (project / "contract.json").write_text(json.dumps(contract))
    r = subprocess.run(
        [sys.executable, "contract_first.py"], cwd=project, env=env_for(project), capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == "8080\n"


def test_unresolved_reference_output() -> None:
    from pydantic import Field, SecretStr

    class S(docuconf.DocuconfSettings):
        database_url: SecretStr = Field(description="Database connection string")

    with pytest.raises(docuconf.ConfigValidationError) as info:
        S.load(env={"DATABASE_URL": "vault:secret/data/db#url"})
    assert block_after("## Injected secrets", "text").strip() == str(info.value.violations[0]).join(["- ", ""])
