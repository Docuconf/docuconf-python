"""docuconf.fastapi: the settings dependency and its lifespan."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import Field

from docuconf import ConfigValidationError, DocuconfSettings


def test_fastapi_dependency(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import asyncio

    from docuconf import fastapi as dfa

    class S(DocuconfSettings):
        port: int = Field(8080, ge=1, description="HTTP listen port")

    class App:
        dependency_overrides: ClassVar[dict[Any, Any]] = {}

    dep = dfa.settings_dependency(S, env={"PORT": "7"})
    assert dep() is dep() and dep().port == 7

    async def start(app: Any) -> None:
        async with dep.lifespan(app):
            pass

    exits: list[int] = []
    monkeypatch.setattr(dfa, "_exit", exits.append)
    App.dependency_overrides[dep] = lambda: S.load(env={"PORT": "0"})
    with pytest.raises(SystemExit):
        asyncio.run(start(App()))
    assert exits == [1]
    assert capsys.readouterr().err.startswith("docuconf: 1 configuration problem:\n  - PORT [out_of_range]")

    App.dependency_overrides[dep] = lambda: S.load(env={"PORT": "9"})
    assert dep.check(App()).port == 9
    strict = dfa.settings_dependency(S, exit_on_error=False, env={"PORT": "0"})
    with pytest.raises(ConfigValidationError):
        strict.check()
