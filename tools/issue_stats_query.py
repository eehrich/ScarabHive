#!/usr/bin/env python3
"""Issue-Stats CLI — query the ``issue_events`` table for pipeline insights.

Replaces the JSON-grovel scripts that used to walk
``chapters.metadata.scoring_history`` to compute persisted-rate per code,
routing distribution, etc. Now every stat is a one-line SQL.

Usage (from project root)::

    python tools/issue_stats_query.py --book-id 167
    python tools/issue_stats_query.py --book-id 167 --code NH18
    python tools/issue_stats_query.py --book-id all --top-codes
    python tools/issue_stats_query.py --story-id 58            # v5b stats

Output is human-readable text. Use ``--json`` for machine-readable output.

Backward compat: works on DBs that don't yet have the ``issue_events``
table (returns ``(no data — table missing or empty)``). On DBs without the
v5b ``story_id`` column (pre-v5b schema), ``--story-id`` returns empty
stats with a one-line note.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_DB = "data/writer/books.db"


def _connect(db_path: str) -> sqlite3.Connection | None:
    if not Path(db_path).exists():
        print(f"ERROR: database not found at {db_path}", file=sys.stderr)
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='issue_events'"
    )
    return cur.fetchone() is not None


def _has_story_id_column(conn: sqlite3.Connection) -> bool:
    """Pre-v5b DBs lack ``story_id`` — return False to short-circuit queries."""
    cur = conn.execute("PRAGMA table_info(issue_events)")
    return any(row[1] == "story_id" for row in cur.fetchall())


def _scope_where(
    book_id: int | None, story_id: int | None,
) -> tuple[str, list[Any]]:
    """Build a ``WHERE ... AND`` fragment + params for the requested scope."""
    where = "WHERE 1=1"
    params: list[Any] = []
    if book_id is not None:
        where += " AND book_id = ?"
        params.append(book_id)
    if story_id is not None:
        where += " AND story_id = ?"
        params.append(story_id)
    return where, params


def persisted_rate_per_code(
    conn: sqlite3.Connection, book_id: int | None,
    story_id: int | None = None,
) -> list[dict[str, Any]]:
    """For each code: how often it was detected, how often it persisted into
    the next round vs. got fixed. Persisted-rate = persisted / (persisted+fixed)."""
    where, params = _scope_where(book_id, story_id)
    rows = conn.execute(
        f"""SELECT code,
                   COUNT(*) AS total,
                   SUM(CASE WHEN resolution = 'fixed' THEN 1 ELSE 0 END) AS fixed,
                   SUM(CASE WHEN resolution = 'persisted' THEN 1 ELSE 0 END) AS persisted,
                   SUM(CASE WHEN resolution = 'escalated' THEN 1 ELSE 0 END) AS escalated,
                   SUM(CASE WHEN resolution = 'unresolved_at_end' THEN 1 ELSE 0 END) AS unresolved,
                   SUM(CASE WHEN resolution = 'rolled_back' THEN 1 ELSE 0 END) AS rolled_back,
                   SUM(CASE WHEN resolution IS NULL THEN 1 ELSE 0 END) AS open
            FROM issue_events
            {where}
            AND code IS NOT NULL
            GROUP BY code
            ORDER BY total DESC""",
        tuple(params),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        fixed_or_persisted = (d["fixed"] or 0) + (d["persisted"] or 0)
        d["persisted_rate"] = (
            round((d["persisted"] or 0) / fixed_or_persisted, 3)
            if fixed_or_persisted else None
        )
        out.append(d)
    return out


def routing_distribution(
    conn: sqlite3.Connection, book_id: int | None,
    story_id: int | None = None,
) -> list[dict[str, Any]]:
    """How are issues routed? Counts per routed_to bucket."""
    where, params = _scope_where(book_id, story_id)
    rows = conn.execute(
        f"""SELECT
                COALESCE(routed_to, '(unrouted)') AS routed_to,
                routing_reason,
                COUNT(*) AS count
            FROM issue_events
            {where}
            GROUP BY routed_to, routing_reason
            ORDER BY count DESC""",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def phase_distribution(
    conn: sqlite3.Connection, book_id: int | None,
    story_id: int | None = None,
) -> list[dict[str, Any]]:
    where, params = _scope_where(book_id, story_id)
    rows = conn.execute(
        f"""SELECT phase, COUNT(*) AS count,
                   SUM(CASE WHEN resolution = 'fixed' THEN 1 ELSE 0 END) AS fixed,
                   SUM(CASE WHEN resolution IN ('unresolved_at_end', 'persisted')
                       THEN 1 ELSE 0 END) AS unresolved
            FROM issue_events
            {where}
            GROUP BY phase
            ORDER BY phase""",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def top_codes_per_book(
    conn: sqlite3.Connection, limit: int = 5,
) -> list[dict[str, Any]]:
    """Per book: top N issue codes by detection count."""
    rows = conn.execute(
        """SELECT book_id, code, COUNT(*) AS count
           FROM issue_events
           WHERE code IS NOT NULL
           GROUP BY book_id, code
           ORDER BY book_id, count DESC""",
    ).fetchall()
    per_book: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        per_book.setdefault(r["book_id"], []).append(dict(r))
    out: list[dict[str, Any]] = []
    for bid, codes in per_book.items():
        out.append({"book_id": bid, "top_codes": codes[:limit]})
    return out


def code_trace(
    conn: sqlite3.Connection, book_id: int | None, code: str,
    story_id: int | None = None,
) -> list[dict[str, Any]]:
    """Full event trace for one (book_or_story, code) — useful debugging."""
    where, params = _scope_where(book_id, story_id)
    where += " AND code = ?"
    params.append(code)
    rows = conn.execute(
        f"""SELECT id, phase, round_idx, chapter_id, scene_id, beat_id,
                  severity, impact_level, routed_to, resolution,
                  resolver_agent, resolved_in_round_idx,
                  detected_at, resolved_at
           FROM issue_events
           {where}
           ORDER BY id ASC""",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


# v5b: split phase_distribution output into v4 and v5b buckets for the
# human-readable section.
_V5B_PHASE_PREFIX = "5b_"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--db", default=DEFAULT_DB,
        help=f"Path to books.db (default: {DEFAULT_DB})",
    )
    p.add_argument(
        "--book-id", default=None,
        help="Book id to filter (omit or use 'all' for global stats)",
    )
    p.add_argument(
        "--story-id", default=None,
        help="Story id to filter (v5b events). Mutually exclusive with "
             "--book-id. Use this for v5b synopsis-audit / debate-critic "
             "stats.",
    )
    p.add_argument(
        "--code", default=None,
        help="Filter to a single issue code (e.g. NH18). With --book-id "
             "or --story-id, prints full event trace.",
    )
    p.add_argument(
        "--top-codes", action="store_true",
        help="Show top issue codes per book (across all books).",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Output as JSON instead of human-readable text.",
    )
    args = p.parse_args()

    book_id: int | None
    if args.book_id is None or args.book_id == "all":
        book_id = None
    else:
        try:
            book_id = int(args.book_id)
        except ValueError:
            print(f"ERROR: invalid --book-id {args.book_id!r}", file=sys.stderr)
            return 2

    story_id: int | None
    if args.story_id is None or args.story_id == "all":
        story_id = None
    else:
        try:
            story_id = int(args.story_id)
        except ValueError:
            print(f"ERROR: invalid --story-id {args.story_id!r}", file=sys.stderr)
            return 2

    if book_id is not None and story_id is not None:
        print("ERROR: --book-id and --story-id are mutually exclusive",
              file=sys.stderr)
        return 2

    conn = _connect(args.db)
    if conn is None:
        return 2
    try:
        if not _table_exists(conn):
            print(
                "(no data — issue_events table not present. Run a pipeline "
                "pass with the latest schema first.)",
            )
            return 0

        if story_id is not None and not _has_story_id_column(conn):
            print(
                "(pre-v5b schema: issue_events has no story_id column. "
                "Run a v5b pipeline pass to upgrade the DB.)",
            )
            return 0

        # Code-trace mode
        if args.code and (book_id is not None or story_id is not None):
            trace = code_trace(conn, book_id, args.code, story_id=story_id)
            if args.json:
                print(json.dumps(trace, indent=2, default=str))
            else:
                if not trace:
                    scope_s = (f"story {story_id}" if story_id is not None
                               else f"book {book_id}")
                    print(f"(no events for {scope_s} code {args.code})")
                else:
                    scope_s = (f"story {story_id}" if story_id is not None
                               else f"book {book_id}")
                    print(f"\n# Trace: {scope_s} code {args.code} "
                          f"({len(trace)} events)\n")
                    for ev in trace:
                        print(
                            f"  [{ev['id']:>5}] phase={ev['phase']:<18} "
                            f"r{ev['round_idx']:<2} ch={ev['chapter_id']} "
                            f"sc={ev['scene_id']} beat={ev['beat_id'] or '-':<6} "
                            f"sev={ev['severity'] or '-':<8} "
                            f"impact={ev['impact_level'] or '-':<7} "
                            f"→ {ev['routed_to'] or '(unrouted)':<24} "
                            f"= {ev['resolution'] or '(open)'}"
                        )
            return 0

        result: dict[str, Any] = {
            "book_id": book_id,
            "story_id": story_id,
            "persisted_rate_per_code": persisted_rate_per_code(
                conn, book_id, story_id=story_id,
            ),
            "routing_distribution": routing_distribution(
                conn, book_id, story_id=story_id,
            ),
            "phase_distribution": phase_distribution(
                conn, book_id, story_id=story_id,
            ),
        }
        if args.top_codes:
            result["top_codes_per_book"] = top_codes_per_book(conn)

        if args.json:
            print(json.dumps(result, indent=2, default=str))
            return 0

        # Human-readable
        if story_id is not None:
            scope = f"story {story_id}"
        elif book_id is not None:
            scope = f"book {book_id}"
        else:
            scope = "all books + stories"
        print(f"\n# Issue-Stats ({scope})\n")

        # Split phase distribution into v4 vs v5b buckets.
        v4_phases = [
            r for r in result["phase_distribution"]
            if not (r.get("phase") or "").startswith(_V5B_PHASE_PREFIX)
        ]
        v5b_phases = [
            r for r in result["phase_distribution"]
            if (r.get("phase") or "").startswith(_V5B_PHASE_PREFIX)
        ]

        print("## Phase distribution (v4 book-pipeline)")
        if not v4_phases:
            print("  (no data)")
        for row in v4_phases:
            print(
                f"  {row['phase']:<18} total={row['count']:<4} "
                f"fixed={row['fixed']:<4} unresolved={row['unresolved']}"
            )

        print("\n## v5b Phasen (story-designer)")
        if not v5b_phases:
            print("  (no data)")
        for row in v5b_phases:
            print(
                f"  {row['phase']:<22} total={row['count']:<4} "
                f"fixed={row['fixed']:<4} unresolved={row['unresolved']}"
            )

        print("\n## Persisted-rate per code "
              "(persisted / (persisted+fixed); higher = harder to fix)")
        if not result["persisted_rate_per_code"]:
            print("  (no data)")
        for row in result["persisted_rate_per_code"]:
            rate = row["persisted_rate"]
            rate_s = "—" if rate is None else f"{rate*100:>5.1f}%"
            print(
                f"  {row['code']:<8} total={row['total']:<4} "
                f"fixed={row['fixed']:<4} persisted={row['persisted']:<4} "
                f"escalated={row['escalated']:<4} unresolved={row['unresolved']:<4} "
                f"open={row['open']:<4} rate={rate_s}"
            )

        print("\n## Routing distribution")
        if not result["routing_distribution"]:
            print("  (no data)")
        for row in result["routing_distribution"]:
            print(
                f"  {row['routed_to']:<24} reason={row['routing_reason'] or '-':<32} "
                f"count={row['count']}"
            )

        if args.top_codes:
            print("\n## Top codes per book")
            if not result.get("top_codes_per_book"):
                print("  (no data)")
            for entry in result.get("top_codes_per_book", []):
                codes = ", ".join(
                    f"{c['code']}×{c['count']}"
                    for c in entry["top_codes"]
                )
                print(f"  Book {entry['book_id']:>4}: {codes}")

        print()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
