# Pine Script Validator

[![CI](https://github.com/Poryaei/pine-script-validator/actions/workflows/ci.yml/badge.svg)](https://github.com/Poryaei/pine-script-validator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)

Static validator, audit CLI, and agent-debugging toolkit for TradingView Pine Script.

`Pine Script Validator` helps humans and coding agents inspect `.pine` files locally, catch syntax and semantic issues early, and turn raw diagnostics into actionable debugging output. It is designed for real-world local workflows where an agent or developer needs to validate, interpret, and iterate on Pine code outside TradingView.

Current metadata coverage and built-in validation are focused on Pine Script v6.

## Highlights

- Pure Python lexer and parser for Pine Script
- Validation of built-in functions and namespaces using bundled Pine Script v6 metadata
- Human-readable diagnostics with line and column locations
- `--json` output for tooling and automation
- `--agent-json` output with excerpts, pointers, suggestions, and next steps
- Severity ordering and filtering controls
- Batch validation for files, directories, and glob patterns
- `--sarif` output for CI and code scanning workflows
- Audit mode for large Pine corpora

## Why This Project Exists

Pine Script tooling outside TradingView is limited, especially for automated debugging workflows. Agents usually need:

- a local parser they can run repeatedly
- machine-readable diagnostics
- enough source context to propose a fix
- stable output they can feed into a retry loop

This project focuses on exactly that.

## Pine Version

This project currently targets Pine Script v6 for built-in metadata coverage, namespace validation, and most real-world compatibility expectations.

If a script relies on older undocumented behavior or version-specific quirks outside the current validator rules, additional parser or semantic support may still be needed.

## Installation

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

## Quick Start

Validate a file in text mode:

```powershell
pine-validator path\to\script.pine
```

Emit plain JSON diagnostics:

```powershell
pine-validator path\to\script.pine --json
```

Emit agent-oriented debugging output:

```powershell
pine-validator path\to\script.pine --agent-json
```

Validate an entire directory:

```powershell
pine-validator path\to\pine-scripts --agent-json
```

Emit SARIF for CI or review tooling:

```powershell
pine-validator path\to\pine-scripts --sarif
```

Validate from stdin:

```powershell
Get-Content path\to\script.pine | python -m pinescript_validator.cli - --agent-json
```

## Agent Workflow

Recommended loop for an agent:

1. Run `pine-validator script.pine --agent-json --no-hints --no-information`
2. Fix hard failures first
3. Re-run until `summary.error` becomes `0`
4. Re-run with warnings and hints enabled for cleanup

For larger agent workflows, point the CLI at a directory and process the batch report file by file.

## Severity Controls

Diagnostics are ordered by severity first, then by source location. By default the CLI prints:

1. errors
2. warnings
3. information
4. hints

Each level can be toggled independently:

```powershell
pine-validator script.pine --no-hints --no-information
pine-validator script.pine --no-errors --no-warnings --hints
pine-validator scripts\*.pine --agent-json --warnings --hints --no-information
```

Supported toggles:

- `--errors` / `--no-errors`
- `--warnings` / `--no-warnings`
- `--information` / `--no-information`
- `--hints` / `--no-hints`

## Example Agent Output

```json
{
  "tool": "pine-script-validator",
  "mode": "agent",
  "path": "example.pine",
  "ok": false,
  "summary": {
    "total": 2,
    "error": 1,
    "warning": 0,
    "information": 0,
    "hint": 1
  },
  "diagnostics": [
    {
      "line": 3,
      "column": 13,
      "severity": "error",
      "message": "Invalid parameter 'invalid_param' for 'plot'",
      "excerpt": "plot(close, invalid_param=true)",
      "pointer": "            ^",
      "suggestion": "Check the target function signature and remove, rename, or relocate unsupported named arguments."
    }
  ],
  "next_steps": [
    "Fix all error diagnostics first, then re-run validation to expose any follow-up semantic issues."
  ]
}
```

## CLI Modes

- default text output for humans
- `--json` for plain machine-readable diagnostics
- `--agent-json` for richer debugging output intended for agents and automation
- directory and multi-file batch validation
- `--sarif` for CI, code scanning, and external review integrations

Exit codes:

- `0` when no error-level diagnostics are present
- `1` when at least one error-level diagnostic is reported

## Audit Mode

The audit command scans one or more script roots, compares observed Pine usage against bundled metadata and local documentation, and writes report files for broader maintenance work.

```powershell
python -m pinescript_validator.audit
python -m pinescript_validator.audit --scripts-root ..\ExternalScripts --docs-root ..\PineTool\pinescript_docs
```

Generated outputs:

- `smart_audit_report.json`
- `smart_audit_report.md`

## Development

Run the tests:

```powershell
python -m pytest -q
```

Project layout:

```text
src/pinescript_validator/   Parser, validator, CLI, audit, agent-report, and SARIF modules
tests/                      Regression and feature tests
pyproject.toml              Packaging and pytest configuration
```

Contribution guide: [CONTRIBUTING.md](CONTRIBUTING.md)

## Feedback And Issues

If you spot a bug, false positive, missing Pine Script v6 behavior, or a confusing diagnostic, please open an issue in the [GitHub Issues](https://github.com/Poryaei/pine-script-validator/issues) section.

Helpful issue reports usually include:

- the Pine snippet or file shape that triggered the problem
- the exact command you ran
- the validator output you received
- the behavior you expected instead

## Scope And Limitations

- This is a validator and static analysis tool, not a Pine runtime
- The current compatibility target is Pine Script v6
- It does not simulate TradingView execution behavior
- Some rare or undocumented Pine patterns may still need parser support
- `--agent-json` suggestions are heuristic guidance, not guaranteed autofixes
- Validation quality depends on the bundled metadata and implemented semantic rules

## Roadmap

- Improve structured diagnostics for more Pine-specific failure modes
- Expand semantic coverage for advanced script patterns
- Add more regression cases from real-world Pine scripts
- Keep improving agent-oriented debugging ergonomics

## License

MIT. See [LICENSE](LICENSE).
