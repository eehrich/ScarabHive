from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path


class Severity(IntEnum):
    ERROR = 0
    WARNING = 1
    INFORMATION = 2
    HINT = 3


@dataclass(slots=True, frozen=True)
class Diagnostic:
    line: int
    column: int
    length: int
    message: str
    severity: Severity
    source: str = "validator"

    def to_dict(self) -> dict[str, object]:
        return {
            "line": self.line,
            "column": self.column,
            "length": self.length,
            "message": self.message,
            "severity": self.severity.name.lower(),
            "source": self.source,
        }

    def format(self, file_path: Path | None = None) -> str:
        location = f"{self.line}:{self.column}"
        if file_path is not None:
            location = f"{file_path}:{location}"
        return f"{location} [{self.severity.name.lower()}] {self.message}"
