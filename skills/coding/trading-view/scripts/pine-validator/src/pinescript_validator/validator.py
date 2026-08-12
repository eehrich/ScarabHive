from __future__ import annotations

from pathlib import Path

from .agent_report import build_agent_report
from .ast_validator import AstValidator
from .diagnostics import Diagnostic, Severity
from .parser import Parser
from .pattern_validator import PatternValidator


class PineScriptValidator:
    def __init__(self) -> None:
        self.pattern_validator = PatternValidator()
        self.ast_validator = AstValidator()

    def validate_text(self, text: str) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []

        parser = Parser(text)
        program = parser.parse()

        for error in parser.lexer_errors:
            diagnostics.append(
                Diagnostic(
                    line=error.line,
                    column=error.column,
                    length=error.length,
                    message=error.message,
                    severity=Severity.ERROR,
                    source="lexer",
                )
            )

        for error in parser.get_errors():
            diagnostics.append(
                Diagnostic(
                    line=error.line,
                    column=error.column,
                    length=1,
                    message=error.message,
                    severity=Severity.ERROR,
                    source="parser",
                )
            )

        diagnostics.extend(self.pattern_validator.validate(text))
        diagnostics.extend(self.ast_validator.validate(program))
        return self._dedupe_and_sort(diagnostics)

    def validate_file(self, path: str | Path) -> list[Diagnostic]:
        file_path = Path(path)
        return self.validate_text(file_path.read_text(encoding="utf-8-sig"))

    def build_agent_report_for_text(self, text: str, *, file_path: str | Path | None = None) -> dict[str, object]:
        diagnostics = self.validate_text(text)
        return build_agent_report(diagnostics, text, file_path=file_path)

    def build_agent_report_for_file(self, path: str | Path) -> dict[str, object]:
        file_path = Path(path)
        text = file_path.read_text(encoding="utf-8-sig")
        diagnostics = self.validate_text(text)
        return build_agent_report(diagnostics, text, file_path=file_path)

    @staticmethod
    def _dedupe_and_sort(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
        seen: set[tuple[int, int, int, str, Severity]] = set()
        output: list[Diagnostic] = []
        for diagnostic in diagnostics:
            key = (
                diagnostic.line,
                diagnostic.column,
                diagnostic.length,
                diagnostic.message,
                diagnostic.severity,
            )
            if key in seen:
                continue
            seen.add(key)
            output.append(diagnostic)
        output.sort(key=lambda item: (item.severity, item.line, item.column, item.message))
        return output
