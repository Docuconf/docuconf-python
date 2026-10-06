"""Config-file overlays (SPEC §4.7, §11.2 item 9)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, ClassVar

import pytest
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

import docuconf
from docuconf import ConfigFile, ConfigValidationError, DeclarationError, DocuconfSettings, Meta, Overlay
from docuconf.export import package_name
from tests.fixtures.overlay_settings import BASE_FILE, CatalogSettings, TomlCatalogSettings, YamlCatalogSettings
from tests.test_export import CUE, SPEC_CUE, needs_cue, vet

OVERLAY = "app/config/catalog.json"


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory with the baked-in file, a DOCUCONF_FILE_ROOT for the overlay, and the secret."""
    for key in list(os.environ):
        if key.upper().startswith("CATALOG_"):
            monkeypatch.delenv(key)
    workdir = tmp_path / "app"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    root = tmp_path / "root"
    (root / "app/config").mkdir(parents=True)
    monkeypatch.setenv("DOCUCONF_FILE_ROOT", str(root))
    monkeypatch.setenv("CATALOG_DB_PASSWORD", "s3cr3t")
    return root


def write_overlay(root: Path, data: dict[str, Any], name: str = OVERLAY) -> Path:
    p = root / name
    p.write_text(json.dumps(data))
    return p


def load_error(cls: type[BaseSettings] = CatalogSettings) -> ConfigValidationError:
    with pytest.raises(ConfigValidationError) as info:
        docuconf.load(cls, watch=False)
    return info.value


# -- export -------------------------------------------------------------------


def test_export_declares_overlay_and_config_keys() -> None:
    data = docuconf.contract_data(CatalogSettings)
    assert data["overlays"] == {
        "platform": {
            "description": "Platform overrides, layered over the baked-in settings",
            "format": "json",
            "path": "/app/config/catalog.json",
            "keySeparator": ".",
            "reload": "watch",
        }
    }
    keys = {n: v.get("configKey") for n, v in data["vars"].items()}
    assert keys == {
        "CATALOG_CACHE_TTL": "cache_ttl",
        "CATALOG_DB_PASSWORD": None,  # secrets never come from an overlay
        "CATALOG_FEATURED_CATEGORIES": "featured_categories",
        "CATALOG_LOG_LEVEL": "log_level",
        "CATALOG_PAGE_SIZE": "page_size",
        "CATALOG_SEARCH__TIMEOUT": "search.timeout",
        "CATALOG_SEARCH__URL": "search.url",
    }
    assert docuconf.contract_data(YamlCatalogSettings)["overlays"]["platform"] == {
        "format": "yaml",
        "path": "/app/config/catalog.yaml",
        "keySeparator": ".",
    }


@needs_cue
@pytest.mark.parametrize("cls", [CatalogSettings, YamlCatalogSettings, TomlCatalogSettings])
def test_export_with_overlay_passes_cue_vet(cls: type[BaseSettings], tmp_path: Path) -> None:
    r = vet(docuconf.to_contract(cls), tmp_path)
    assert r.returncode == 0, r.stderr


def test_config_key_follows_aliases() -> None:
    class Db(BaseModel):
        host: str = Field("db", description="Database host", alias="Host")

    class S(BaseSettings):
        docuconf_overlays: ClassVar[Sequence[Overlay]] = (Overlay("platform", "/etc/svc/config/overlay.toml"),)
        model_config = SettingsConfigDict(env_prefix="SVC_", env_nested_delimiter="__")
        port: int = Field(8080, description="HTTP port", alias="SVC_HTTP_PORT")
        db: Db = Field(Db(), description="Database")

    keys = {n: v["configKey"] for n, v in docuconf.contract_data(S, name="svc")["vars"].items()}
    assert keys == {"SVC_DB__HOST": "db.Host", "SVC_HTTP_PORT": "SVC_HTTP_PORT"}


# -- loading and precedence ---------------------------------------------------


def test_precedence_env_over_overlay_over_baked_in_file_over_defaults(
    app: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path(BASE_FILE).write_text(
        "page_size: 10\ncache_ttl: PT30S\nlog_level: debug\nsearch:\n  url: https://base.internal\n"
    )
    write_overlay(app, {"page_size": 50, "log_level": "warn", "search": {"url": "https://overlay.internal"}})
    monkeypatch.setenv("CATALOG_LOG_LEVEL", "error")
    monkeypatch.setenv("CATALOG_SEARCH__TIMEOUT", "PT2S")

    s = docuconf.load(CatalogSettings, watch=False)

    assert s.log_level == "error"  # environment beats the overlay
    assert s.page_size == 50  # overlay beats the baked-in file
    assert s.search.url == "https://overlay.internal"
    assert s.search.timeout == timedelta(seconds=2)  # nested values merge across sources
    assert s.cache_ttl == timedelta(seconds=30)  # baked-in file beats the default
    assert s.featured_categories == []  # default


def test_missing_overlay_is_fine(app: Path) -> None:
    Path(BASE_FILE).write_text("search:\n  url: https://base.internal\n")
    s = docuconf.load(CatalogSettings, watch=False)
    assert s.page_size == 20 and s.search.url == "https://base.internal"


def test_overlay_satisfies_a_required_variable(app: Path) -> None:
    write_overlay(app, {"search": {"url": "https://search.internal"}, "featured_categories": ["books"]})
    s = docuconf.load(CatalogSettings, watch=False)
    assert s.search.url == "https://search.internal"
    assert s.featured_categories == ["books"]


def test_empty_env_value_falls_back_to_the_overlay(app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_overlay(app, {"page_size": 50, "search": {"url": "https://search.internal"}})
    monkeypatch.setenv("CATALOG_PAGE_SIZE", "")
    assert docuconf.load(CatalogSettings, watch=False).page_size == 50


def test_overlay_values_are_validated_like_env_values(app: Path) -> None:
    write_overlay(
        app,
        {"page_size": 0, "log_level": "loud", "cache_ttl": "soon", "search": {"url": "http://search.internal"}},
    )
    err = load_error()
    by_name = {v.input: v for v in err.violations}
    assert {n: v.code for n, v in by_name.items()} == {
        "CATALOG_CACHE_TTL": "invalid_type",
        "CATALOG_LOG_LEVEL": "not_in_enum",
        "CATALOG_PAGE_SIZE": "out_of_range",
        "CATALOG_SEARCH__URL": "invalid_scheme",
    }
    assert by_name["CATALOG_PAGE_SIZE"].message.endswith("(from overlay platform)")


def test_overlay_int_must_fit_64_bits(app: Path) -> None:
    write_overlay(app, {"page_size": 2**70, "search": {"url": "https://search.internal"}})
    err = load_error()
    assert [(v.input, v.code) for v in err.violations] == [("CATALOG_PAGE_SIZE", "out_of_range")]


@pytest.mark.parametrize(
    ("content", "detail"),
    [("{not json", "is not valid json"), ("[1, 2]", "must hold a mapping")],
)
def test_malformed_overlay_is_reported_with_everything_else(
    app: Path, monkeypatch: pytest.MonkeyPatch, content: str, detail: str
) -> None:
    (app / OVERLAY).write_text(content)
    monkeypatch.setenv("CATALOG_PAGE_SIZE", "many")
    err = load_error()
    assert [(v.input, v.code) for v in err.violations] == [
        ("CATALOG_PAGE_SIZE", "invalid_type"),
        ("CATALOG_SEARCH__URL", "missing_required"),
        ("platform", "file_malformed"),
    ]
    assert detail in err.violations[-1].message


def test_env_and_overlay_both_set_logs_a_warning(
    app: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    write_overlay(app, {"page_size": 50, "search": {"url": "https://search.internal"}})
    monkeypatch.setenv("CATALOG_PAGE_SIZE", "70")
    assert docuconf.load(CatalogSettings, watch=False).page_size == 70
    assert "CATALOG_PAGE_SIZE is set in the environment and in overlay platform" in caplog.text


@pytest.mark.parametrize(
    ("cls", "content"),
    [
        (YamlCatalogSettings, "page_size: 42\nsearch:\n  url: https://search.internal\n"),
        (TomlCatalogSettings, 'page_size = 42\n\n[search]\nurl = "https://search.internal"\n'),
    ],
)
def test_yaml_and_toml_overlays(app: Path, cls: type[CatalogSettings], content: str) -> None:
    fmt = docuconf.declaration(cls).overlays[0].format
    (app / f"app/config/catalog.{fmt}").write_text(content)
    s = docuconf.load(cls, watch=False)
    assert s.page_size == 42 and s.search.url == "https://search.internal"


def test_mixin_loads_overlays_with_default_sources(app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Svc(DocuconfSettings, BaseSettings):
        docuconf_overlays: ClassVar[Sequence[Overlay]] = (Overlay("platform", "/app/config/catalog.json"),)
        model_config = SettingsConfigDict(env_prefix="CATALOG_")
        page_size: int = Field(20, description="Items per page")
        log_level: str = Field("info", description="Minimum log level")

    write_overlay(app, {"page_size": 50, "log_level": "warn"})
    monkeypatch.setenv("CATALOG_LOG_LEVEL", "error")
    s = Svc.load(watch=False)
    assert (s.page_size, s.log_level) == (50, "error")


# -- declaration checks -------------------------------------------------------


def test_overlays_must_be_wired_into_the_sources(app: Path) -> None:
    class Svc(BaseSettings):
        docuconf_overlays: ClassVar[Sequence[Overlay]] = (Overlay("platform", "/app/config/catalog.json"),)
        model_config = SettingsConfigDict(env_prefix="CATALOG_")
        page_size: int = Field(20, description="Items per page")

    with pytest.raises(DeclarationError, match="settings_customise_sources does not load them"):
        docuconf.load(Svc, watch=False)


def test_overlay_must_not_hide_shipped_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_settings import JsonConfigSettingsSource

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DOCUCONF_FILE_ROOT", raising=False)
    (tmp_path / "config").mkdir()

    class InWorkdir(DocuconfSettings, BaseSettings):
        docuconf_overlays: ClassVar[Sequence[Overlay]] = (Overlay("platform", f"{tmp_path}/overlay.json"),)
        page_size: int = Field(20, description="Items per page")

    with pytest.raises(DeclarationError, match="working directory"):
        docuconf.load(InWorkdir, watch=False)

    class OverBakedIn(BaseSettings):
        docuconf_overlays: ClassVar[Sequence[Overlay]] = (Overlay("platform", f"{tmp_path}/config/overlay.json"),)
        page_size: int = Field(20, description="Items per page")

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return docuconf.with_overlays(
                settings_cls, init_settings, env_settings, JsonConfigSettingsSource(settings_cls, "config/base.json")
            )

    with pytest.raises(DeclarationError, match=r"would hide .*config/base\.json"):
        docuconf.load(OverBakedIn, watch=False)


def test_environment_must_come_before_baked_in_files(app: Path) -> None:
    from pydantic_settings import JsonConfigSettingsSource

    class Svc(BaseSettings):
        docuconf_overlays: ClassVar[Sequence[Overlay]] = (Overlay("platform", "/app/config/catalog.json"),)
        page_size: int = Field(20, description="Items per page")

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return docuconf.with_overlays(
                settings_cls, init_settings, JsonConfigSettingsSource(settings_cls, "base.json"), env_settings
            )

    with pytest.raises(DeclarationError, match="environment sources must come before config file sources"):
        docuconf.load(Svc, watch=False)


class _Routes(BaseModel):
    routes: list[str] = Field(default_factory=list)


@pytest.mark.parametrize(
    ("overlays", "fields", "message"),
    [
        ((Overlay("platform", "/app/config/settings.ini"),), {}, "cannot infer the format"),
        ((Overlay("Platform", "/app/config/a.json"),), {}, "must be a DNS label"),
        ((Overlay("platform", "app/config/a.json"),), {}, "must be absolute and normalised"),
        ((Overlay("platform", "/app/config/a.json", description="x"),), {}, "at least 5 characters"),
        (
            (Overlay("platform", "/app/config/a.json"), Overlay("platform", "/app/other/b.json")),
            {},
            "declared twice",
        ),
        (
            (Overlay("platform", "/etc/svc/routes/overlay.json"),),
            {
                "routes": (
                    Annotated[_Routes, ConfigFile(path="/etc/svc/routes/routes.json")],
                    Field(_Routes(), description="Routing table"),
                )
            },
            "also mounted for file routes",
        ),
        (
            (Overlay("platform", "/app/config/a.json"),),
            {"port": (Annotated[int, Meta(config_key="http.port")], Field(8080, description="HTTP port"))},
            "config_key 'http.port' must be 'port'",
        ),
        (
            (Overlay("platform", "/app/config/a.json"),),
            {"port": (int, Field(8080, description="HTTP port", alias="http.port"))},
            "a key part contains '.'",
        ),
    ],
)
def test_overlay_declaration_errors(overlays: tuple[Overlay, ...], fields: dict[str, Any], message: str) -> None:
    from pydantic import create_model

    cls = create_model(  # type: ignore[call-overload]
        "Svc",
        __base__=BaseSettings,
        page_size=(int, Field(20, description="Items per page")),
        **fields,
    )
    cls.docuconf_overlays = overlays
    with pytest.raises(DeclarationError) as info:
        docuconf.declaration(cls)
    assert message in str(info.value)


# -- watch --------------------------------------------------------------------


def test_watch_reloads_the_overlay(app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = write_overlay(app, {"page_size": 50, "log_level": "warn", "search": {"url": "https://a.internal"}})
    monkeypatch.setenv("CATALOG_LOG_LEVEL", "error")
    s = docuconf.load(CatalogSettings, watch_interval=3600)
    w = docuconf.get_watcher(s)
    assert w is not None and w.overlays == ["platform"] and w.inputs == []
    seen: list[tuple[str, Any]] = []
    w.on_reload(lambda name, value: seen.append((name, value)))
    try:
        assert w.check_now() == []
        # Kubernetes-style update: write a new file and rename it into place.
        staging = overlay.with_name("staging.json")
        staging.write_text(json.dumps({"page_size": 75, "log_level": "debug", "search": {"url": "https://b.internal"}}))
        staging.replace(overlay)
        assert w.check_now() == ["platform"]
        assert s.page_size == 75
        assert s.search.url == "https://b.internal"
        assert s.log_level == "error"  # the environment still wins
        assert seen == [("platform", {"page_size": 75, "search": s.search})]

        # A bad update is rejected and the previous values kept.
        overlay.write_text(json.dumps({"page_size": 0}))
        assert w.check_now() == []
        assert s.page_size == 75

        # A removed overlay falls back to the lower layers.
        overlay.unlink()
        monkeypatch.setenv("CATALOG_SEARCH__URL", "https://env.internal")
        assert w.check_now() == ["platform"]
        assert s.page_size == 20 and s.search.url == "https://env.internal"
    finally:
        w.stop()


def test_restart_overlay_is_not_watched(app: Path) -> None:
    (app / "app/config/catalog.yaml").write_text("search:\n  url: https://search.internal\n")
    s = docuconf.load(YamlCatalogSettings)
    assert docuconf.get_watcher(s) is None


# -- end to end ---------------------------------------------------------------

RENDER = """\
package platform

import (
\t"encoding/json"
\t"docuconf.dev/contract"
\tapp "docuconf.dev/svc:{pkg}"
)

rendered: contract.#Render & {{
\tcontract: app
\tvalues: CATALOG_DB_PASSWORD: injected: {{provider: "bank-vaults", ref: "vault:secret/data/catalog/db#password"}}
\toverlays: platform: {{
\t\tCATALOG_PAGE_SIZE:   50
\t\tCATALOG_CACHE_TTL:   "1m30s"
\t\tCATALOG_LOG_LEVEL:   "warn"
\t\tCATALOG_SEARCH__URL: "https://search.internal"
\t\tCATALOG_SEARCH__TIMEOUT: "2s"
\t\tCATALOG_FEATURED_CATEGORIES: ["books", "games"]
\t}}
}}
file: rendered.configMaps[0].data["{file}"]
env: json.Marshal(rendered.env)
"""


def cue_module(cls: type[BaseSettings], tmp_path: Path) -> Path:
    module = tmp_path / "module"
    shutil.copytree(SPEC_CUE / "cue.mod", module / "cue.mod")
    shutil.copytree(SPEC_CUE / "contract", module / "contract")
    (module / "svc").mkdir()
    (module / "svc/contract.cue").write_text(docuconf.to_contract(cls))
    return module


def cue_export(module: Path, expr: str) -> str:
    assert CUE is not None
    r = subprocess.run(
        [CUE, "export", "./platform", "-e", expr, "--out", "text"], cwd=module, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


@needs_cue
@pytest.mark.parametrize("cls", [CatalogSettings, YamlCatalogSettings, TomlCatalogSettings])
def test_overlay_rendered_by_the_platform_binds_in_the_app(
    cls: type[CatalogSettings], app: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Export the contract, render the overlay with the meta-schema's #Render, load the rendered file."""
    o = docuconf.declaration(cls).overlays[0]
    module = cue_module(cls, tmp_path)
    file_name = o.path.rsplit("/", 1)[1]
    (module / "platform").mkdir()
    (module / "platform/render.cue").write_text(
        RENDER.format(pkg=package_name(docuconf.declaration(cls).service or ""), file=file_name)
    )
    rendered = cue_export(module, "file")
    (app / o.path.lstrip("/")).write_text(rendered)

    s = docuconf.load(cls, watch=False)
    assert s.page_size == 50
    assert s.cache_ttl == timedelta(seconds=90)
    assert s.log_level == "warn"
    assert s.search.url == "https://search.internal"
    assert s.search.timeout == timedelta(seconds=2)
    assert s.featured_categories == ["books", "games"]

    # When the injector did not run, the app sees the reference #Render wrote, and says so.
    env = json.loads(cue_export(module, "env"))
    assert env == [{"name": "CATALOG_DB_PASSWORD", "value": "vault:secret/data/catalog/db#password"}]
    monkeypatch.setenv(env[0]["name"], env[0]["value"])
    err = load_error(cls)
    assert [(v.input, v.code) for v in err.violations] == [("CATALOG_DB_PASSWORD", "invalid_type")]
    assert "secret/data" not in str(err)
