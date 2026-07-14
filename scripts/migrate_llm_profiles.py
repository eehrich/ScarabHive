"""Migration: llm_profile [normal, advanced] + llm_profile_fallbacks → Ketten-Semantik.

ALT (Positions-Semantik):
    llm_profile: [deepseek-chat, or-gpt-full-unlimited]      # [0]=standard, [-1]=advanced
    llm_profile_fallbacks: [deepseek-chat, or-gemini-pro]    # [0]=std-fb, [1]=adv-fb

NEU (Ketten-Semantik):
    llm_profile: [deepseek-chat]                             # [primär, fallback1, ...]
    llm_profile_advanced: [or-gpt-full-unlimited, or-gemini-pro]

Regeln:
- llm_profile mit 1 Eintrag (oder String): bleibt; llm_profile_advanced wird
  IMMER explizit ergänzt ([] wenn kein Advanced existierte) — verhindert
  ungewolltes Erben einer Advanced-Kette aus default_config.
- llm_profile mit 2 Einträgen [n, a]: llm_profile=[n], advanced=[a].
- >2 Einträge (Choice-Listen wie basic_agent): llm_profile=alle außer letztem,
  advanced=[letzter] — Datei wird als REVIEW geflaggt (Choice-Semantik prüfen).
- fallbacks [f0, f1, extra...]: f0 + extra → normale Kette, f1 → Advanced-Kette.
  (Zur Laufzeit dient die normale Kette ohnehin als Sicherheitsnetz der
  Advanced-Kette — Doppel-Einträge werden dedupliziert.)
- Transformiert werden NUR Zeilen innerhalb von `agent_config:`-Blöcken
  (schema.yaml-Config-Keys namens llm_profile bleiben unberührt).
- Nur einzeilige Flow-Listen/Skalare; Block-Listen werden geflaggt.

Aufruf:
    python scripts/migrate_llm_profiles.py            # Dry-Run (zeigt Diffs)
    python scripts/migrate_llm_profiles.py --apply    # schreibt
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
    """Flow-Liste '[a, b]' → Liste; Skalar → String; leer/Block-Stil → None."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("["):
        if not raw.endswith("]"):
            return None  # mehrzeilige Flow-Liste — flaggen
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
    """Returns (neue Zeilen oder None wenn unverändert, Warnungen)."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=False)
    warnings: list[str] = []
    changed = False

    # agent_config-Blöcke lokalisieren: (start_line, block_indent)
    in_block_indent: int | None = None
    ac_indent: int | None = None

    # Erst alle relevanten Zeilen-Indizes einsammeln, gruppiert pro Block
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
                current = None  # Block zu Ende (Dedent)
                continue
            if current["child_indent"] is None:
                current["child_indent"] = indent  # Ebene der direkten Kinder
        if current is None:
            continue
        # Nur DIREKTE Kinder von agent_config matchen — ein tiefer verschachtelter
        # gleichnamiger Key (z.B. unter template_vars) darf nicht migriert werden.
        if current["child_indent"] is not None and indent != current["child_indent"]:
            continue
        m = LP_RE.match(line)
        if m and "llm_profile_" not in line.split(":")[0]:
            current["lp"] = i
        m = FB_RE.match(line)
        if m:
            current["fb"] = i
        if ADV_RE.match(line):
            current["adv"] = True  # Block bereits migriert — Idempotenz-Guard

    out = list(lines)
    # Rückwärts bearbeiten, damit Insert-Indizes stabil bleiben
    for blk in reversed(blocks):
        lp_i = blk["lp"]
        fb_i = blk["fb"]
        if blk["adv"]:
            # Idempotenz: llm_profile_advanced existiert bereits → Block ist
            # schon Ketten-Semantik. Nur noch eine verwaiste fallbacks-Zeile
            # wäre ein Fehler (halb-migriert) — flaggen statt raten.
            if fb_i is not None:
                warnings.append(
                    f"{path}: llm_profile_advanced UND llm_profile_fallbacks "
                    f"im selben Block (Zeile {fb_i+1}) — MANUELL bereinigen"
                )
            continue
        if lp_i is None and fb_i is None:
            continue
        if lp_i is None and fb_i is not None:
            warnings.append(f"{path}: llm_profile_fallbacks ohne llm_profile (Zeile {fb_i+1}) — MANUELL")
            continue

        lp_m = LP_RE.match(lines[lp_i])
        lp_indent, lp_raw, lp_comment = lp_m.group(1), lp_m.group(2), lp_m.group(3) or ""
        lp_val = parse_value(lp_raw)
        if lp_val is None:
            warnings.append(f"{path}: llm_profile Block-Stil/leer (Zeile {lp_i+1}) — MANUELL")
            continue

        fb_val: list[str] | str | None = None
        if fb_i is not None:
            fb_m = FB_RE.match(lines[fb_i])
            fb_val = parse_value(fb_m.group(2))
            if fb_val is None:
                warnings.append(f"{path}: llm_profile_fallbacks Block-Stil (Zeile {fb_i+1}) — MANUELL")
                continue
            if isinstance(fb_val, str):
                fb_val = [fb_val]

        lp_list = [lp_val] if isinstance(lp_val, str) else list(lp_val)
        fb_list = list(fb_val) if fb_val else []

        if len(lp_list) > 2:
            warnings.append(
                f"{path}: llm_profile hat {len(lp_list)} Einträge (Choice-Liste?) "
                f"(Zeile {lp_i+1}) — migriert als Kette, bitte REVIEWEN"
            )

        # Mapping alte → neue Semantik.
        # WICHTIG: Der alte Runtime-Loop nutzte im NORMAL-Modus die KOMPLETTE
        # fallbacks-Liste (der [f1,f0]-Swap galt nur für advanced). Deshalb:
        # - hatte der Agent KEIN Advanced (len<=1): ALLE fallbacks → normale
        #   Kette, advanced bleibt [] (kein "erfundenes" Advanced-Modell —
        #   f1 war ein Verfügbarkeits-Fallback, kein Qualitäts-Upgrade).
        # - hatte er ein Advanced: f1 (der alte Advanced-Fallback) → Advanced-
        #   Kette, f0 + Rest → normale Kette. Beide Modi erreichen zur Laufzeit
        #   weiterhin ALLE Einträge über das symmetrische Sicherheitsnetz
        #   (AgentConfig.fallback_chain: eigene Kette zuerst, dann die andere).
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
            # fallbacks-Zeile wird zur advanced-Zeile
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
    # Zeilenende-Stil erhalten
    trailing_nl = "\n" if text.endswith("\n") else ""
    return [l + "\n" for l in out[:-1]] + [out[-1] + trailing_nl], warnings


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
                all_warnings.append(f"{path}: FEHLER {e}")
                continue
            all_warnings.extend(warnings)
            if new_lines is None:
                continue
            changed_files.append(path)
            if apply:
                path.write_text("".join(new_lines), encoding="utf-8", newline="\n")

    print(f"{'GEÄNDERT' if apply else 'WÜRDE ÄNDERN'}: {len(changed_files)} Dateien")
    for p in changed_files:
        print(f"  {p.relative_to(repo)}")
    if all_warnings:
        print(f"\nWARNUNGEN ({len(all_warnings)}):")
        for w in all_warnings:
            print(f"  ⚠ {w}")
    if not apply:
        print("\nDry-Run — mit --apply schreiben.")


if __name__ == "__main__":
    main()
