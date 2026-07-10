"""Import framework: parser interface and categories."""

from abc import ABC, abstractmethod
from enum import Enum

from sqlmodel import Session

from dtos.imports import ImportConfirmRequest, ImportConfirmResponse, ImportPreviewResponse

# Anti-DoS caps applied to every uploaded CSV
MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CSV_ROWS = 20_000


class ImportCategory(str, Enum):
    CRYPTO = "crypto"
    STOCK = "stock"
    BANK = "bank"


class ImportParser(ABC):
    """One import source (Binance, Kraken, generic CSV…).

    Implementations are stateless singletons registered in
    :mod:`services.imports.registry`.
    """

    source_id: str
    label: str
    category: ImportCategory
    file_hint: str
    supports_mapping: bool = False

    @abstractmethod
    def detect(self, csv_content: str) -> float:
        """Header-based confidence score in [0, 1] that this parser matches."""

    @abstractmethod
    def preview(
        self,
        session: Session,
        csv_content: str,
        options: dict,
        *,
        account_id: str | None = None,
        master_key: str | None = None,
    ) -> ImportPreviewResponse:
        """Parse the CSV and return a user-reviewable preview.

        When ``account_id`` and ``master_key`` are provided, rows already
        present on the target account are flagged ``is_duplicate``.
        """

    @abstractmethod
    def execute(
        self,
        session: Session,
        account_id: str,
        payload: ImportConfirmRequest,
        master_key: str,
    ) -> ImportConfirmResponse:
        """Create the transactions/points from a confirmed preview."""


def csv_header_line(csv_content: str) -> str:
    """First non-empty line of the CSV, BOM stripped (for cheap detection)."""
    if csv_content.startswith("\ufeff"):
        csv_content = csv_content[1:]
    for line in csv_content.splitlines():
        if line.strip():
            return line.strip()
    return ""
