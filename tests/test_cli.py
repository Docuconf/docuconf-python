"""The docuconf command line: exporting without an environment, one-line errors, --check."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import docuconf
from docuconf import ConfigValidationError, cli


@pytest.fixture
def module_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    (tmp_path / "modlevel.py").write_text(
        "from typing import ClassVar\n"
        "from pydantic import Field\n"
        "import docuconf\n"
        "class Settings(docuconf.DocuconfSettings):\n"
        "    docuconf_service: ClassVar[str] = 'svc'\n"
        "    database_url: str = Field(description='Postgres connection string')\n"
        "settings = docuconf.load(Settings)\n"
        "also = Settings()\n"
        "class Config(docuconf.DocuconfSettings):\n"
        "    port: int = Field(8080, description='HTTP listen port')\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    yield tmp_path
    sys.modules.pop("modlevel", None)


def test_export_survives_a_module_level_load(module_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["export", "modlevel:Settings"]) == 0
    assert "DATABASE_URL" in capsys.readouterr().out
    with pytest.raises(ConfigValidationError):
        docuconf.load(sys.modules["modlevel"].Settings)  # outside the export, load validates again


def test_cli_import_errors_are_one_line(module_dir: Path) -> None:
    with pytest.raises(SystemExit, match="no module named 'nope'"):
        cli.main(["export", "nope:Settings"])
    with pytest.raises(SystemExit, match="has no attribute 'Setings' \\(did you mean 'Settings'\\?\\)"):
        cli.main(["export", "modlevel:Setings"])


def test_check_needs_out(module_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["export", "modlevel:Settings", "--check"])
    assert info.value.code == 2
    assert "--check needs --out FILE" in capsys.readouterr().err


def test_check_ignores_the_generator_version(module_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = module_dir / "contract.cue"
    assert cli.main(["export", "modlevel:Settings", "-o", str(out)]) == 0
    out.write_text(out.read_text().replace(f'"{docuconf.__version__}"', '"0.0.1"'))
    assert cli.main(["export", "modlevel:Settings", "-o", str(out), "--check"]) == 0
    assert "names another docuconf version" in capsys.readouterr().err
    out.write_text(out.read_text().replace("DATABASE_URL", "DB_URL"))
    assert cli.main(["export", "modlevel:Settings", "-o", str(out), "--check"]) == 1


def test_missing_service_name_names_the_flag(module_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["export", "modlevel:Config"]) == 1
    err = capsys.readouterr().err
    assert "Config has no service name" in err and "pass --name or set docuconf_service" in err
