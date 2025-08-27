#!/usr/bin/env python3
"""Simple markdown lint helper (moved from .prompts/scripts)."""
import sys
from pathlib import Path


def main(path):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    issues = []
    lines = text.splitlines()
    seen_headers = set()
    for i, line in enumerate(lines, start=1):
        if '\t' in line:
            issues.append(f"Tab character at line {i}")
        if line.rstrip() != line:
            issues.append(f"Trailing whitespace at line {i}")
        if line.startswith('#'):
            h = line.strip()
            if h in seen_headers:
                issues.append(f"Duplicate header '{h}' at line {i}")
            seen_headers.add(h)
        # check indent width (only list-indent style)
        if line.startswith('  '):
            # ensure indents multiple of 2
            leading = len(line) - len(line.lstrip(' '))
            if leading % 2 != 0:
                issues.append(f"Odd indent width ({leading}) at line {i}")

    if issues:
        print("LINT REPORT")
        for it in issues:
            print(it)
        return 1
    print("LINT REPORT\nNo issues found")
    return 0

# Note: this linter accepts 'reverted' (↩) as a valid status label in backlog entries.


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("usage: md_lint.py <file>")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
