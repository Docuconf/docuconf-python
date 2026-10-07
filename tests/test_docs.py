"""description and details from their native doc locations (SPEC §4.2, §14.7)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field
from pydantic_settings import SettingsConfigDict

import docuconf
from docuconf import DeclarationError, DocuconfSettings, TextFile
from docuconf.docs import split_doc, to_markdown
from tests.conftest import find_cue

SPEC_CUE = Path(os.environ.get("DOCUCONF_SPEC_CUE", Path(__file__).parent.parent.parent / "docuconf-go/spec/cue"))
CUE = find_cue()


class Pool(BaseModel):
    size: int = Field(10, description="Connections in the pool")
    """Each worker holds one connection while it runs a query."""


class DocSettings(DocuconfSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__")

    port: int = Field(8080, ge=1, le=65535, description="HTTP listen port")
    """Behind the mesh, keep the default.

    The sidecar forwards :class:`Ingress` traffic here; see ``PORT`` in the chart."""

    region: str = "eu-west-1"
    """Cloud region for object storage.

    Change it with the bucket:

    - ``eu-west-1`` for Europe
    - ``us-east-1`` for the US

    Example::

        REGION=us-east-1

    .. code-block:: yaml

        region: us-east-1

    :param region: dropped, as API docs are not configuration docs.
    """

    plain: str = Field("x", description="No details here")

    extra: str = Field("x", description="Details from extra", json_schema_extra={"details": "Given *here*."})
    """Ignored: the explicit details win."""

    pool: Pool = Pool()

    licence: Annotated[str, TextFile(path="/etc/app/licence.txt")] = Field("", description="Licence key file")
    """Issued per customer.

    Rotate it yearly."""


class AttributeDocstrings(DocuconfSettings):
    model_config = SettingsConfigDict(use_attribute_docstrings=True)

    timeout: int = 30
    """Seconds to wait for the upstream.

    Raise it for batch clients."""


class NoDescription(DocuconfSettings):
    nothing: int = 3


class BlankDetails(DocuconfSettings):
    blank: str = Field("x", description="Has blank details", json_schema_extra={"details": "  \n "})


class LongDetails(DocuconfSettings):
    long: str = Field("x", description="Has long details", json_schema_extra={"details": "日本" * 2000 + "!"})


class MaxDetails(DocuconfSettings):
    most: str = Field("x", description="Has 4000 characters of details", json_schema_extra={"details": "日本" * 2000})


def _vars(cls: type[DocuconfSettings]) -> dict[str, Any]:
    return docuconf.contract_data(cls, name="svc")["vars"]  # type: ignore[no-any-return]


def test_description_from_field_and_details_from_the_docstring() -> None:
    port = _vars(DocSettings)["PORT"]
    assert port["description"] == "HTTP listen port"
    assert port["details"] == (
        "Behind the mesh, keep the default.\n\nThe sidecar forwards `Ingress` traffic here; see `PORT` in the chart."
    )
    assert list(port)[:3] == ["type", "description", "details"]
    assert "details" not in _vars(DocSettings)["PLAIN"]


def test_description_from_the_docstring_first_paragraph() -> None:
    region = _vars(DocSettings)["REGION"]
    assert region["description"] == "Cloud region for object storage"
    assert region["details"] == (
        "Change it with the bucket:\n\n"
        "- `eu-west-1` for Europe\n"
        "- `us-east-1` for the US\n\n"
        "Example:\n\n"
        "```\nREGION=us-east-1\n```\n\n"
        "```yaml\nregion: us-east-1\n```"
    )


def test_details_from_json_schema_extra_win() -> None:
    assert _vars(DocSettings)["EXTRA"]["details"] == "Given *here*."


def test_nested_model_and_file_input() -> None:
    assert _vars(DocSettings)["POOL__SIZE"]["details"] == "Each worker holds one connection while it runs a query."
    files = docuconf.contract_data(DocSettings, name="svc")["files"]
    assert files["licence"]["details"] == "Issued per customer.\n\nRotate it yearly."
    assert list(files["licence"])[:3] == ["type", "description", "details"]


def test_use_attribute_docstrings() -> None:
    timeout = _vars(AttributeDocstrings)["TIMEOUT"]
    assert timeout["description"] == "Seconds to wait for the upstream"
    assert timeout["details"] == "Raise it for batch clients."


def test_split_doc_and_markdown() -> None:
    assert split_doc("One line.") == ("One line", "")
    assert split_doc("Wrapped\n   over lines.\n\nRest.\n\nMore.") == ("Wrapped over lines", "Rest.\n\nMore.")
    assert split_doc("   ") == ("", "")
    assert to_markdown("Use :py:meth:`~app.Client.close` or :ref:`the guide <closing>`.") == (
        "Use `app.Client.close` or `the guide`."
    )
    assert to_markdown("```python\n:class:`kept`\n```") == "```python\n:class:`kept`\n```"


def test_missing_description_fails() -> None:
    with pytest.raises(DeclarationError) as info:
        docuconf.to_contract(NoDescription, name="svc")
    assert "NOTHING (nothing): needs a description of at least 5 characters" in str(info.value)


def test_blank_details_fail() -> None:
    with pytest.raises(DeclarationError) as info:
        docuconf.to_contract(BlankDetails, name="svc")
    assert "BLANK (blank): details must not be blank" in str(info.value)


def test_details_over_4000_code_points_fail() -> None:
    with pytest.raises(DeclarationError) as info:
        docuconf.to_contract(LongDetails, name="svc")
    assert "details are 4001 characters; at most 4000 are allowed" in str(info.value)
    # 4000 code points (12000 bytes of UTF-8) is fine.
    assert len(_vars(MaxDetails)["MOST"]["details"]) == 4000


def test_details_are_not_runtime_config() -> None:
    settings = docuconf.load(AttributeDocstrings, env={"TIMEOUT": "5"}, termination_log=False)
    assert settings.timeout == 5
    assert settings.model_dump() == {"timeout": 5}


def test_contract_first_loads_with_details() -> None:
    contract = {
        "apiVersion": "docuconf.dev/v1alpha1",
        "kind": "ConfigContract",
        "metadata": {"name": "svc", "generator": {"language": "go", "sdk": "docuconf-go", "version": "0.1.0"}},
        "vars": {
            "PORT": {
                "type": "int",
                "description": "HTTP listen port",
                "details": "Behind the mesh, keep the default.\n\n- one\n- two",
                "default": 8080,
            },
        },
    }
    values = docuconf.load_contract(contract, {"PORT": "9090"}, termination_log=False)
    assert values.PORT == 9090  # type: ignore[attr-defined]
    bad = {**contract, "vars": {"PORT": {**contract["vars"]["PORT"], "details": " "}}}  # type: ignore[dict-item]
    with pytest.raises(DeclarationError, match="details must not be blank"):
        docuconf.load_contract(bad, {}, termination_log=False)


@pytest.mark.skipif(
    CUE is None or not (SPEC_CUE / "contract").is_dir(), reason="cue or the docuconf-go meta-schema is not available"
)
def test_details_pass_cue_vet(tmp_path: Path) -> None:
    out = docuconf.to_contract(DocSettings, name="svc")
    assert "details:" in out
    shutil.copytree(SPEC_CUE / "cue.mod", tmp_path / "cue.mod")
    shutil.copytree(SPEC_CUE / "contract", tmp_path / "contract")
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc/contract.cue").write_text(out)
    assert CUE is not None
    r = subprocess.run([CUE, "vet", "-c", "./svc"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
