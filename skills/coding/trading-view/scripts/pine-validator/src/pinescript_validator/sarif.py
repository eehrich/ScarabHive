from __future__ import annotations

from pathlib import Path

from .diagnostics import Diagnostic, Severity


_LEVELS = {
    Severity.ERROR: "error",
    Severity.WARNING: "warning",
    Severity.INFORMATION: "note",
    Severity.HINT: "note",
}


def _rule_id(diagnostic: Diagnostic) -> str:
    slug = diagnostic.message.lower()
    for old, new in (("'", ""), ('"', ""), (" ", "-"), (".", ""), (",", ""), (":", "")):
        slug = slug.replace(old, new)
    return f"{diagnostic.source}/{slug[:80]}"


def _artifact_uri(path: str | Path | None) -> str:
    if path is None:
        return "<stdin>"
    return Path(path).as_posix()


def build_sarif_run(results: list[dict[str, object]]) -> dict[str, object]:
    rule_index: dict[str, dict[str, str]] = {}
    sarif_results: list[dict[str, object]] = []

    for item in results:
        path = item["path"]
        diagnostics: list[Diagnostic] = item["diagnostics"]
        for diagnostic in diagnostics:
            rule_id = _rule_id(diagnostic)
            rule_index.setdefault(
                rule_id,
                {
                    "id": rule_id,
                    "name": diagnostic.source,
                    "shortDescription": {"text": diagnostic.message},
                },
            )
            sarif_results.append(
                {
                    "ruleId": rule_id,
                    "level": _LEVELS[diagnostic.severity],
                    "message": {"text": diagnostic.message},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": _artifact_uri(path)},
                                "region": {
                                    "startLine": diagnostic.line,
                                    "startColumn": diagnostic.column,
                                    "endColumn": diagnostic.column + max(diagnostic.length, 1) - 1,
                                },
                            }
                        }
                    ],
                }
            )

    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "pine-script-validator",
                        "informationUri": "https://github.com/Poryaei/pine-script-validator",
                        "rules": list(rule_index.values()),
                    }
                },
                "results": sarif_results,
            }
        ],
    }
