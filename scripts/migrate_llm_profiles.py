"""Migration: llm_profile [normal, advanced] + llm_profile_fallbacks → chain semantics.

OLD (positional semantics):
    llm_profile: [deepseek-chat, or-gpt-full-unlimited]      # [0]=standard, [-1]=advanced
    llm_profile_fallbacks: [deepseek-chat, or-gemini-pro]    # [0]=std-fb, [1]=adv-fb

NEW (chain semantics):
    llm_profile: [deepseek-chat]                             # [primary, fallback1, ...]
    llm_profile_advanced: [or-gpt-full-unlimited, or-gemini-pro]

Rules:
- llm_profile with 1 entry (or a string): stays; llm_profile_advanced is
  ALWAYS added explicitly ([] if no advanced existed) — prevents
  unintentionally inheriting an advanced chain from default_config.
- llm_profile with 2 entries [n, a]: llm_profile=[n], advanced=[a].
- >2 entries (choice lists like basic_agent): llm_profile=all but the last,
  advanced=[last] — the file is flagged for REVIEW (check the choice semantics).
- fallbacks [f0, f1, extra...]: f0 + extra → normal chain, f1 → advanced chain.
  (At runtime the normal chain serves as the safety net of the
  advanced chain anyway — duplicate entries are deduplicated.)
- ONLY lines inside `agent_config:` blocks are transformed
  (schema.yaml config keys named llm_profile stay untouched).
- Only single-line flow lists/scalars; block lists are flagged.

Usage:
    python scripts/migrate_llm_profiles.py            # dry run (shows diffs)
    python scripts/migrate_llm_profiles.py --apply    # writes
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOTS = ["src", "config", "tests"]
EXCLUDE_PARTS = {"node_modules", ".venv", "batch_logs", "__pycache__"}

LP_RE = re.compile(r"^(\s*)llm_profile:\s*(.*?)\s*(#.*)?$")
FB_RE = re.compile(r"^(\s*)llm_profile_fallbacks:\s*(.*?)\s*(#.*)?$")
ADV_RE = re.compile(r"^(\s*)llm_profile_advanced:")
AC_RE = re.compile(r"^(\s*)agent_config:\s*(#.*)?$")


def parse_value(raw: str) -> list[str] | str | None:
    """Flow list '[a, b]' → list; scalar → string; empty/block style → None."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("["):
        if not raw.endswith("]"):
            return None  # multi-line flow list — flag it
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
    return raw.strip("'\"")


def fmt_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


def dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for i in items:
        if i and i not in seen:
            seen.append(i)
    return seen


def migrate_file(path: Path) -> tuple[list[str] | None, list[str]]:
    """Returns (new lines or None if unchanged, warnings)."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=False)
    warnings: list[str] = []
    changed = False

    # Locate agent_config blocks: (start_line, block_indent)

    # First collect all relevant line indices, grouped per block
    blocks: list[dict] = []
    current: dict | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if AC_RE.match(line):
            current = {"ac_indent": indent, "child_indent": None,
                       "lp": None, "fb": None, "adv": False}
            blocks.append(current)
            continue
        if current is not None and stripped and not line.lstrip().startswith("#"):
            if indent <= current["ac_indent"]:
                current = None  # block ends (dedent)
                continue
            if current["child_indent"] is None:
                current["child_indent"] = indent  # level of the direct children
        if current is None:
            continue
        # Match only DIRECT children of agent_config — a deeply nested key
        # of the same name (e.g. under template_vars) must not be migrated.
        if current["child_indent"] is not None and indent != current["child_indent"]:
            continue
        m = LP_RE.match(line)
        if m and "llm_profile_" not in line.split(":")[0]:
            current["lp"] = i
        m = FB_RE.match(line)
        if m:
            current["fb"] = i
        if ADV_RE.match(line):
            current["adv"] = True  # block already migrated — idempotence guard

    out = list(lines)
    # Process backwards so that insert indices stay stable
    for blk in reversed(blocks):
        lp_i = blk["lp"]
        fb_i = blk["fb"]
        if blk["adv"]:
            # Idempotence: llm_profile_advanced already exists → the block is
            # already chain semantics. Only an orphaned fallbacks line
            # would be an error (half-migrated) — flag it instead of guessing.
            if fb_i is not None:
                warnings.append(
                    f"{path}: llm_profile_advanced AND llm_profile_fallbacks "
                    f"in the same block (line {fb_i+1}) — clean up MANUALLY"
                )
            continue
        if lp_i is None and fb_i is None:
            continue
        if lp_i is None and fb_i is not None:
            warnings.append(f"{path}: llm_profile_fallbacks without llm_profile (line {fb_i+1}) — MANUAL")
            continue

        lp_m = LP_RE.match(lines[lp_i])
        lp_indent, lp_raw, lp_comment = lp_m.group(1), lp_m.group(2), lp_m.group(3) or ""
        lp_val = parse_value(lp_raw)
        if lp_val is None:
            warnings.append(f"{path}: llm_profile block style/empty (line {lp_i+1}) — MANUAL")
            continue

        fb_val: list[str] | str | None = None
        if fb_i is not None:
            fb_m = FB_RE.match(lines[fb_i])
            fb_val = parse_value(fb_m.group(2))
            if fb_val is None:
                warnings.append(f"{path}: llm_profile_fallbacks block style (line {fb_i+1}) — MANUAL")
                continue
            if isinstance(fb_val, str):
                fb_val = [fb_val]

        lp_list = [lp_val] if isinstance(lp_val, str) else list(lp_val)
        fb_list = list(fb_val) if fb_val else []

        if len(lp_list) > 2:
            warnings.append(
                f"{path}: llm_profile has {len(lp_list)} entries (choice list?) "
                f"(line {lp_i+1}) — migrated as a chain, please REVIEW"
            )

        # Mapping old → new semantics.
        # IMPORTANT: The old runtime loop used the COMPLETE fallbacks list in
        # NORMAL mode (the [f1,f0] swap applied only to advanced). Therefore:
        # - if the agent had NO advanced (len<=1): ALL fallbacks → normal
        #   chain, advanced stays [] (no "invented" advanced model —
        #   f1 was an availability fallback, not a quality upgrade).
        # - if it had an advanced: f1 (the old advanced fallback) → advanced
        #   chain, f0 + rest → normal chain. Both modes still reach ALL entries
        #   at runtime via the symmetric safety net
        #   (AgentConfig.fallback_chain: own chain first, then the other).
        if len(lp_list) <= 1:
            normal = (lp_list or ["normal"]) + fb_list
            advanced: list[str] = []
        else:
            normal = lp_list[:-1]
            advanced = [lp_list[-1]]
            if fb_list:
                normal += fb_list[:1] + fb_list[2:]
                if len(fb_list) > 1:
                    advanced += [fb_list[1]]
        normal = dedupe(normal)
        advanced = dedupe([a for a in advanced if a not in normal[:1]])

        comment = f"  {lp_comment}" if lp_comment else ""
        new_lp_line = f"{lp_indent}llm_profile: {fmt_list(normal)}{comment}"
        new_adv_line = f"{lp_indent}llm_profile_advanced: {fmt_list(advanced)}"

        if fb_i is not None:
            fb_comment = (FB_RE.match(lines[fb_i]).group(3) or "")
            if fb_comment:
                new_adv_line += f"  {fb_comment}"
            # the fallbacks line becomes the advanced line
            if fb_i > lp_i:
                out[lp_i] = new_lp_line
                out[fb_i] = new_adv_line
            else:
                out[fb_i] = new_adv_line
                out[lp_i] = new_lp_line
        else:
            out[lp_i] = new_lp_line
            out.insert(lp_i + 1, new_adv_line)
        changed = True

    if not changed:
        return None, warnings
    # Preserve the line-ending style
    trailing_nl = "\n" if text.endswith("\n") else ""
    return [ln + "\n" for ln in out[:-1]] + [out[-1] + trailing_nl], warnings


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    apply = "--apply" in sys.argv
    repo = Path(__file__).resolve().parent.parent
    all_warnings: list[str] = []
    changed_files: list[Path] = []

    for root in ROOTS:
        for path in sorted((repo / root).rglob("*.yaml")) + sorted((repo / root).rglob("*.yml")):
            if any(part in EXCLUDE_PARTS for part in path.parts):
                continue
            try:
                new_lines, warnings = migrate_file(path)
            except Exception as e:
                all_warnings.append(f"{path}: ERROR {e}")
                continue
            all_warnings.extend(warnings)
            if new_lines is None:
                continue
            changed_files.append(path)
            if apply:
                path.write_text("".join(new_lines), encoding="utf-8", newline="\n")

    print(f"{'CHANGED' if apply else 'WOULD CHANGE'}: {len(changed_files)} files")
    for p in changed_files:
        print(f"  {p.relative_to(repo)}")
    if all_warnings:
        print(f"\nWARNINGS ({len(all_warnings)}):")
        for w in all_warnings:
            print(f"  ⚠ {w}")
    if not apply:
        print("\nDry run — write with --apply.")


if __name__ == "__main__":
    main()
