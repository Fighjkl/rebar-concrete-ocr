"""Small material contract: extraction, matching/review, statistics, future filling."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Progress = Callable[[str, int, int, str], None]


@dataclass(frozen=True)
class RuntimeOptions:
    device: str = 'gpu:0'
    ocr_profile: str = 'batched_gpu'
    dpi: int = 180
    inspection_layout: str = 'auto'
    artifact_policy: str = 'compact'


@dataclass(frozen=True)
class ExtractedDocuments:
    records: Path
    attachment_index: Path
    page_index: Path | None = None


@dataclass(frozen=True)
class MaterialInfo:
    id: str
    name: str
    enabled: bool
    description: str
    layouts: tuple[str, ...] = ('auto',)


class MaterialAdapter(ABC):
    """A new material can replace all business logic without editing the web UI."""
    info: MaterialInfo

    @abstractmethod
    def extract(self, pdf: Path, output: Path, options: RuntimeOptions, progress: Progress) -> ExtractedDocuments:
        """Write original records, OCR evidence and coordinates to a new directory."""

    @abstractmethod
    def match(self, pdf: Path, documents: ExtractedDocuments, output: Path) -> dict:
        """Create review/index.html and machine-readable result/metrics artifacts."""

    @abstractmethod
    def statistics(self, result: dict) -> dict:
        """Material-specific statistics; duplicates and units remain explicit."""

    def fingerprint(self) -> str:
        """Override when configuration is read from files or another source."""
        return self.info.id + ':v1'

    def form_writer(self):
        """Return an ApprovedFormWriter once formal filling is implemented."""
        return None
