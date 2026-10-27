"""Material writers consume explicitly reviewed rows with trusted source evidence."""
from abc import ABC, abstractmethod
from pathlib import Path


class ApprovedFormWriter(ABC):
    def preview(self, result: dict, review_state: dict) -> dict:
        """Validate matching state and prepare editable rows; override per material."""
        raise NotImplementedError

    @abstractmethod
    def write(self, approved_records: list[dict], template: Path | None, output: Path) -> Path:
        """Write to a new file; implementation validates required approval/evidence."""
