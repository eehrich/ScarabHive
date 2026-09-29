from __future__ import annotations

import re
from pathlib import Path

from .diagnostics import Diagnostic, Severity


_SUGGESTION_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"Invalid parameter '([^']+)'"),
        "Check the target function signature and remove, rename, or relocate unsupported named arguments.",
    ),
    (
        re.compile(r"Undefined variable '([^']+)'"),
        "Declare the identifier before use, fix the spelling, or replace it with the intended Pine namespace/member.",
    ),
    (
        re.compile(r"Undefined function '([^']+)'"),
        "Check the function name, required imports or aliases, and whether the call exists in the targeted Pine version.",
    ),
    (
        re.compile(r"already defined"),
        "Rename one of the conflicting declarations or reuse the existing symbol instead of redeclaring it.",
    ),
    (
        re.compile(r"never used"),
        "Remove the unused declaration or wire it into the logic if it is meant to participate in the script.",
    ),
    (
        re.compile(r"Unknown property '([^']+)' on namespace '([^']+)'"),
        "Verify the namespace member name and confirm that it is available in Pine v6 metadata.",
    ),
    (
        re.compile(r"should be called on each calculation for consistency"),
        "Hoist the stateful call out of the conditional or ternary branch so it executes on every bar.",
    ),
    (
        re.compile(r"Unexpected token"),
        "Inspect the surrounding Pine syntax near this location; multiline expressions and indentation are common causes.",
    ),
]


def _severity_counts(diagnostics: list[Diagnostic]) -> dict[str, int]:
    return {
        "error": sum(1 for item in diagnostics if item.severity == Severity.ERROR),
        "warning": sum(1 for item in diagnostics if item.severity == Severity.WARNING),
        "information": sum(1 for item in diagnostics if item.severity == Severity.INFORMATION),
        "hint": sum(1 for item in diagnostics if item.severity == Severity.HINT),
    }


def _line_excerpt(lines: list[str], line_number: int) -> str:
    if line_number < 1 or line_number > len(lines):
        return ""
    return lines[line_number - 1]


def _pointer(column: int, length: int) -> str:
    safe_column = max(column, 1)
    safe_length = max(length, 1)
    return (" " * (safe_column - 1)) + ("^" * safe_length)


def _suggestion(message: str) -> str:
    for pattern, suggestion in _SUGGESTION_RULES:
        if pattern.search(message):
            return suggestion
    return "Inspect the diagnostic location and surrounding Pine syntax, then adjust the symbol usage or statement structure accordingly."


def build_agent_report(
    diagnostics: list[Diagnostic],
    text: str,
    *,
    file_path: str | Path | None = None,
) -> dict[str, object]:
    lines = text.splitlines()
    path_value = str(file_path) if file_path is not None else "<stdin>"
    counts = _severity_counts(diagnostics)

    items: list[dict[str, object]] = []
    for diagnostic in diagnostics:
        excerpt = _line_excerpt(lines, diagnostic.line)
        items.append(
            {
                "path": path_value,
                "line": diagnostic.line,
                "column": diagnostic.column,
                "length": diagnostic.length,
                "end_column": diagnostic.column + max(diagnostic.length, 1) - 1,
                "severity": diagnostic.severity.name.lower(),
                "source": diagnostic.source,
                "message": diagnostic.message,
                "excerpt": excerpt,
                "pointer": _pointer(diagnostic.column, diagnostic.length),
                "suggestion": _suggestion(diagnostic.message),
            }
        )

    next_steps: list[str] = []
    if counts["error"]:
        next_steps.append("Fix all error diagnostics first, then re-run validation to expose any follow-up semantic issues.")
    if counts["warning"]:
        next_steps.append("Review warnings after errors; they often point to risky but parseable Pine patterns.")
    if counts["hint"]:
        next_steps.append("Use remaining hints as cleanup work once the script is error-free.")
    if not next_steps:
        next_steps.append("No diagnostics were reported. The script is a good candidate for the next agent step or deeper semantic checks.")

    return {
        "tool": "pine-script-validator",
        "mode": "agent",
        "path": path_value,
        "ok": counts["error"] == 0,
        "summary": {
            "total": len(diagnostics),
            **counts,
        },
        "diagnostics": items,
        "next_steps": next_steps,
    }


def clone_agent_report_with_diagnostics(
    report: dict[str, object],
    diagnostics: list[Diagnostic],
    text: str,
    *,
    file_path: str | Path | None = None,
) -> dict[str, object]:
    return build_agent_report(diagnostics, text, file_path=file_path)
