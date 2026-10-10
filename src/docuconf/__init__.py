"""docuconf for pydantic-settings.

Typed configuration contracts between an application and the Kubernetes
platform that runs it: keep your ``BaseSettings`` class, export it as a CUE
contract, and validate the real environment, file inputs and config-file
overlays at boot.
"""

from ._version import __version__
from .contract import ContractSettings, contract_settings, load_contract
from .declaration import Declaration, declaration
from .errors import ERROR_CODES, ConfigValidationError, DeclarationError, DocuconfError, DocuconfWarning, Violation
from .export import contract_data, to_contract
from .keyset import Keys, KeySet
from .loader import DocuconfSettings, load, load_or_exit
from .markers import (
    BinaryFile,
    CaBundleFile,
    ConfigFile,
    Csv,
    CsvList,
    Duration,
    Exclude,
    IndexedList,
    JsonMaxLength,
    KeystoreFile,
    Meta,
    Overlay,
    Secret,
    TextFile,
    TlsFile,
    Url,
)
from .overlays import with_overlays
from .values import CaBundle, Keystore, TlsKeyPair
from .watch import RejectedChange, ReloadStatus, Watcher, get_watcher

__all__ = [
    "ERROR_CODES",
    "BinaryFile",
    "CaBundle",
    "CaBundleFile",
    "ConfigFile",
    "ConfigValidationError",
    "ContractSettings",
    "Csv",
    "CsvList",
    "Declaration",
    "DeclarationError",
    "DocuconfError",
    "DocuconfSettings",
    "DocuconfWarning",
    "Duration",
    "Exclude",
    "IndexedList",
    "JsonMaxLength",
    "KeySet",
    "Keys",
    "Keystore",
    "KeystoreFile",
    "Meta",
    "Overlay",
    "Secret",
    "TextFile",
    "TlsFile",
    "TlsKeyPair",
    "Url",
    "Violation",
    "RejectedChange",
    "ReloadStatus",
    "Watcher",
    "__version__",
    "contract_data",
    "contract_settings",
    "declaration",
    "get_watcher",
    "load",
    "load_or_exit",
    "load_contract",
    "to_contract",
    "with_overlays",
]
