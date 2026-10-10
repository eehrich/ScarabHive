#!/usr/bin/env python3
"""Total LLM cost of one agent run — by SESSION ID — including the full
sub-agent (and sub-sub-agent, ...) tree.

Self-contained base-system tool. It reads only:
  - the session↔request mapping from the logs (``logs/cli.log`` + ``logs/api.log``,
    incl. rotations) — the line ``Request <rid> acquired lock for session <sid>``
    emitted by the core ``session_tracking`` component,
  - the core message_debugger DB (``data/message_debugger/debugger.db``) for usage,
  - the central pricing table (``config/llm_pricing.yaml``).
No dependency on the writer plugin.

Why a session id needs the log: the message_debugger stores usage keyed by
``request_id``. Its ``session_id`` column (``turns`` and ``llm_requests``) holds
the request's session (for ``llm_requests`` only when the agent's session
tracker knows the request), is empty in older rows, and a sub-agent's rows
carry the sub-agent's own session id (``sub_<name>_<n>``), not the
coordinator's — so filtering on it would miss the sub-agent tree. But every agent-cli / server run logs which
request_id(s) served which session, so the session id you get from
``Session saved: <sid>`` resolves to its request-id root(s), whose request
trees are then walked. A session that ran several times (resumes / pipeline
phases) maps to several roots — all are summed.

Sub-agents attach via the request tree: a child agent's request_id is prefixed
with the coordinator's root (root ``pps9mf3s1o`` → sub-agent
``pps9mf3s1o_063_sub_cont_...`` → sub-sub-agent ``..._071_sub_cont_...``), so one
prefix walk covers the whole tree however deep it nests.

Cost model (mirrors config/llm_pricing.yaml):
  - if the stored usage carries a ``cost`` field it is used verbatim, 0
    included (OpenRouter's billed amount incl. caching; Ollama's 0);
  - otherwise: uncached_input·input + cached_input·cached_rate +
    output·output, per 1M tokens, ×batch_discount for batch providers.

Usage:
    python scripts/session_costs.py uiej9qvu2t          # cost of a session
    python scripts/session_costs.py uiej9qvu2t --json    # machine-readable
    python scripts/session_costs.py --list-recent        # recent session ids
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import re
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "message_debugger" / "debugger.db"
DEFAULT_PRICING = REPO_ROOT / "config" / "llm_pricing.yaml"
LOG_DIR = REPO_ROOT / "logs"

# Standalone script: make the package importable so the pricing formula can be
# shared instead of copied (the copy here had already drifted from the core).
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
from agent_system.llm.pricing import estimate_cost  # noqa: E402
from agent_system.llm.pricing import load_pricing as _load_pricing  # noqa: E402

# session_tracking logs: "Request <request_id> acquired lock for session <session_id>"
_LOCK_RE = re.compile(r"Request (\S+) acquired lock for session (\S+)")


# ---------------------------------------------------------------------------
# Session <-> request-id mapping, recovered from the logs
# ---------------------------------------------------------------------------

def _log_files(explicit: str | None) -> list[Path]:
    """Log files to scan: an explicit one, or cli.log + api.log incl. rotations."""
    if explicit:
        return [Path(explicit)]
    files: list[Path] = []
    for base in ("cli.log", "api.log"):
        files += [Path(p) for p in sorted(glob.glob(str(LOG_DIR / f"{base}*")))]
    return files


def _open_log(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore")
    return open(path, encoding="utf-8", errors="ignore")


def _root_of(request_id: str) -> str:
    """Root id = everything before the first ``_`` (a sub-agent id folds back
    to its top root, so the whole run is measured)."""
    return request_id.split("_", 1)[0] if request_id else request_id


def scan_session_map(log_files: list[Path]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Parse the logs once. Returns:
      - session_to_roots: session_id -> [request-id roots], in first-seen order,
      - root_to_session:  request-id root -> session_id (for --list-recent)."""
    session_to_roots: dict[str, list[str]] = {}
    root_to_session: dict[str, str] = {}
    for path in log_files:
        if not path.exists():
            continue
        try:
            with _open_log(path) as fh:
                for line in fh:
                    if "acquired lock for session" not in line:
                        continue
                    m = _LOCK_RE.search(line)
                    if not m:
                        continue
                    full_rid, sid = m.group(1), m.group(2)
                    root = _root_of(full_rid)
                    roots = session_to_roots.setdefault(sid, [])
                    if root not in roots:
                        roots.append(root)
                    # Only the coordinator's OWN lock (full id == root) names the
                    # run's TOP session; sub-agent locks fold to the same root but
                    # belong to sub-sessions — don't let them overwrite it.
                    if full_rid == root:
                        root_to_session[root] = sid
        except OSError as e:
            print(f"WARNING: could not read log {path}: {e}", file=sys.stderr)
    return session_to_roots, root_to_session


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def load_pricing(path: Path) -> dict[str, dict[str, float]]:
    """Per-model rates, via the shared loader (agent_system.llm.pricing)."""
    table = _load_pricing(path)
    if not table:
        print(f"WARNING: no pricing loaded from {path} — costs will be $0 "
              f"unless the usage carries an OpenRouter cost field.", file=sys.stderr)
    return table


def compute_cost(
    pricing: dict[str, dict[str, float]],
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    is_batch: bool,
    path: Path | None = None,
    cache_write_tokens: int = 0,
) -> float:
    """Estimated cost of calls that carry no billed cost; calc_tree_costs takes
    a billed sum as it is.

    The formula itself lives in agent_system.llm.pricing.estimate_cost -- this
    used to be a third private copy of it, and the copies drifted.
    """
    cost = estimate_cost(model, prompt_tokens, completion_tokens, cached_tokens,
                         cache_write_tokens=cache_write_tokens, is_batch=is_batch, path=path)
    # unknown model → uncounted (surfaced as a warning by the caller)
    return cost if cost is not None else 0.0


# ---------------------------------------------------------------------------
# Request-id tree walk
# ---------------------------------------------------------------------------

def _root_exists(conn: sqlite3.Connection, root: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM llm_requests "
        "WHERE request_id = ? OR request_id LIKE ? || '!_%' ESCAPE '!' LIMIT 1",
        (root, root),
    ).fetchone()
    return row is not None


def _tree_where(roots: list[str]) -> tuple[str, list[str]]:
    conds, params = [], []
    for r in roots:
        conds.append("(request_id = ? OR request_id LIKE ? || '!_%' ESCAPE '!')")
        params += [r, r]
    return " OR ".join(conds), params


def tree_stats(conn: sqlite3.Connection, roots: list[str]) -> dict:
    """Shape metrics for the resolved tree (for the human header)."""
    where, params = _tree_where(roots)
    tree_calls = conn.execute(
        f"SELECT COUNT(*) FROM llm_requests WHERE ({where}) AND direction='response'",
        params,
    ).fetchone()[0]
    root_ph = ",".join("?" * len(roots))
    root_calls = conn.execute(
        f"SELECT COUNT(*) FROM llm_requests "
        f"WHERE request_id IN ({root_ph}) AND direction='response'",
        roots,
    ).fetchone()[0]
    depth = 0
    for (rid,) in conn.execute(
        f"SELECT DISTINCT request_id FROM llm_requests "
        f"WHERE ({where}) AND direction='response'",
        params,
    ):
        depth = max(depth, (rid or "").count("_sub"))
    return {
        "root_calls": root_calls,
        "tree_calls": tree_calls,
        "sub_calls": max(0, tree_calls - root_calls),
        "max_depth": depth,
    }


def calc_tree_costs(
    conn: sqlite3.Connection,
    roots: list[str],
    pricing: dict[str, dict[str, float]],
) -> tuple[list[dict], list[str]]:
    """Aggregate per (model, agent, batch) rows and price each.

    Returns (rows, warnings). ``warnings`` names models that had calls but no
    price and no billed cost — i.e. counted as $0 (a hint to add a rate).

    Calls that carry a cost and calls that carry none are summed apart: a
    billed cost -- 0 included (Ollama) -- is the price of its calls only, and
    the others are estimated."""
    where, params = _tree_where(roots)
    cur = conn.execute(
        f"""
        SELECT model, agent_name,
               CASE WHEN provider LIKE 'batch!_%' ESCAPE '!' THEN 1 ELSE 0 END AS is_batch,
               json_extract(usage_json, '$.cost') IS NOT NULL AS billed,
               COUNT(*) AS calls,
               COALESCE(SUM(json_extract(usage_json, '$.prompt_tokens')), 0) AS pt,
               COALESCE(SUM(json_extract(usage_json, '$.completion_tokens')), 0) AS ct,
               COALESCE(SUM(json_extract(usage_json, '$.prompt_tokens_details.cached_tokens')), 0) AS cached,
               COALESCE(SUM(COALESCE(json_extract(usage_json, '$.prompt_tokens_details.cache_write_tokens'),
                                     json_extract(usage_json, '$.prompt_tokens_details.cache_creation_tokens'),
                                     0)), 0) AS writes,
               COALESCE(SUM(json_extract(usage_json, '$.cost')), 0) AS or_cost
        FROM llm_requests
        WHERE ({where}) AND direction = 'response'
        GROUP BY model, agent_name, is_batch, billed
        """,
        params,
    )
    merged: dict[tuple, dict] = {}
    unpriced: set[str] = set()
    for model, agent, is_batch, billed, calls, pt, ct, cached, writes, or_cost in cur.fetchall():
        batch = bool(is_batch)
        if billed:
            cost = float(or_cost)
        else:
            cost = compute_cost(pricing, model, pt, ct, cached, batch,
                                cache_write_tokens=writes)
            if cost == 0 and (pt or ct) and model not in pricing:  # errors carry no tokens
                unpriced.add(model or "(empty)")
        row = merged.setdefault((model, agent, batch), {
            "model": model, "agent": agent, "is_batch": batch, "calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "cost": 0.0,
        })
        row["calls"] += calls
        row["prompt_tokens"] += pt
        row["completion_tokens"] += ct
        row["cached_tokens"] += cached
        row["cost"] += cost
    rows = sorted(merged.values(), key=lambda r: r["prompt_tokens"], reverse=True)
    return rows, sorted(unpriced)


def _totals(rows: list[dict]) -> dict:
    return {
        "cost": sum(r["cost"] for r in rows),
        "calls": sum(r["calls"] for r in rows),
        "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
        "completion_tokens": sum(r["completion_tokens"] for r in rows),
        "cached_tokens": sum(r["cached_tokens"] for r in rows),
    }


# ---------------------------------------------------------------------------
# Discovery: recent sessions (so you can pick the session id)
# ---------------------------------------------------------------------------

def list_recent(conn: sqlite3.Connection, root_to_session: dict[str, str],
                limit: int) -> None:
    """List recent runs by SESSION id, with time, calls and coordinator agent."""
    rows = conn.execute(
        """
        SELECT CASE WHEN INSTR(request_id, '_') > 0
                    THEN SUBSTR(request_id, 1, INSTR(request_id, '_') - 1)
                    ELSE request_id END AS root,
               MAX(created_at) AS last_ts,
               COUNT(*) AS calls,
               MAX(CASE WHEN request_id NOT LIKE '%!_%' ESCAPE '!'
                        THEN agent_name END) AS coordinator
        FROM llm_requests
        WHERE direction = 'response'
        GROUP BY root
        ORDER BY last_ts DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    print(f"\n{'Session id':<16} | {'Last call':<19} | {'Calls':>5} | "
          f"{'Coordinator':<24} | Run root")
    print("-" * 92)
    for root, last_ts, calls, coordinator in rows:
        sid = root_to_session.get(root, "(not in logs)")
        print(f"{sid:<16} | {(last_ts or '')[:19]:<19} | {calls:>5} | "
              f"{(coordinator or ''):<24} | {root}")
    print("\nThen:  python scripts/session_costs.py <session id>")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _by(rows: list[dict], key: str) -> dict[str, float]:
    agg: dict[str, float] = {}
    for r in rows:
        name = r[key]
        if key == "model" and r["is_batch"]:
            name = f"{name} (batch)"
        agg[name] = agg.get(name, 0.0) + r["cost"]
    return dict(sorted(agg.items(), key=lambda kv: kv[1], reverse=True))


def print_human(session_id: str, roots: list[str], stats: dict,
                rows: list[dict], warnings: list[str]) -> None:
    tot = _totals(rows)
    print(f"\nSession    : {session_id}")
    print(f"Run root(s): {', '.join(roots)}")
    print(
        f"Tree       : {stats['tree_calls']} calls "
        f"= {stats['root_calls']} coordinator + {stats['sub_calls']} sub-agent"
        f"  |  max sub-agent depth: {stats['max_depth']}"
    )
    print("=" * 136)
    print(f"  {'Model':<28.28} | {'Agent':<32.32} | {'Calls':>5} | "
          f"{'Input':>11} | {'Cached':>11} | {'Cache%':>6} | {'Output':>11} | {'Cost $':>9}")
    print("  " + "-" * 134)
    for r in rows:
        model = f"{r['model']} (batch)" if r["is_batch"] else r["model"]
        row_cache_pct = (r["cached_tokens"] / r["prompt_tokens"] * 100) if r["prompt_tokens"] else 0
        print(f"  {model:<28.28} | {r['agent']:<32.32} | {r['calls']:>5} | "
              f"{r['prompt_tokens']:>11,} | {r['cached_tokens']:>11,} | {row_cache_pct:>5.1f}% | "
              f"{r['completion_tokens']:>11,} | ${r['cost']:>8.4f}")
    print("  " + "-" * 134)
    cache_pct = (tot["cached_tokens"] / tot["prompt_tokens"] * 100
                 if tot["prompt_tokens"] else 0)
    print(f"  {'TOTAL':<28} | {'':<32} | {tot['calls']:>5} | "
          f"{tot['prompt_tokens']:>11,} | {tot['cached_tokens']:>11,} | {cache_pct:>5.1f}% | "
          f"{tot['completion_tokens']:>11,} | ${tot['cost']:>8.4f}")
    print(f"  Cache hit rate: {cache_pct:.1f}%")

    print("\n  Cost by model:")
    for name, c in _by(rows, "model").items():
        pct = c / tot["cost"] * 100 if tot["cost"] else 0
        print(f"    {name:<34} ${c:>8.4f}  ({pct:>5.1f}%)")
    print("\n  Cost by agent:")
    for name, c in _by(rows, "agent").items():
        pct = c / tot["cost"] * 100 if tot["cost"] else 0
        print(f"    {name:<34} ${c:>8.4f}  ({pct:>5.1f}%)")

    if warnings:
        print(f"\n  ! no price + no billed cost (counted as $0): {', '.join(warnings)}")
        print("    -> add these models to config/llm_pricing.yaml for accurate totals.")
    print(f"\n  >>> GRAND TOTAL (incl. all sub-agents): ${tot['cost']:.4f}")


def build_json(session_id: str, roots: list[str], stats: dict,
               rows: list[dict], warnings: list[str]) -> dict:
    tot = _totals(rows)
    per_agent: dict[str, dict] = {}
    for r in rows:
        a = per_agent.setdefault(r["agent"], {
            "cost_usd": 0.0, "calls": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "cached_tokens": 0})
        a["cost_usd"] += r["cost"]
        a["calls"] += r["calls"]
        a["prompt_tokens"] += r["prompt_tokens"]
        a["completion_tokens"] += r["completion_tokens"]
        a["cached_tokens"] += r["cached_tokens"]
    for a in per_agent.values():
        a["cost_usd"] = round(a["cost_usd"], 6)
    return {
        "session_id": session_id,
        "root_request_ids": roots,
        "total_cost_usd": round(tot["cost"], 6),
        "total_calls": tot["calls"],
        "prompt_tokens": tot["prompt_tokens"],
        "completion_tokens": tot["completion_tokens"],
        "cached_tokens": tot["cached_tokens"],
        "cache_hit_rate_pct": round(
            tot["cached_tokens"] / tot["prompt_tokens"] * 100, 1)
        if tot["prompt_tokens"] else 0.0,
        "coordinator_calls": stats["root_calls"],
        "sub_agent_calls": stats["sub_calls"],
        "max_sub_agent_depth": stats["max_depth"],
        "distinct_agents": len(per_agent),
        "cost_by_agent": dict(sorted(
            per_agent.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)),
        "cost_by_model": {k: round(v, 6) for k, v in _by(rows, "model").items()},
        "unpriced_models": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Total LLM cost of an agent run by SESSION ID, incl. all "
                    "sub-agents (reads logs + message_debugger DB + pricing yaml).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The session id is what agent-cli prints as 'Session saved: <id>'.\n"
            "It is resolved to its request-id root(s) via logs/cli.log + api.log.\n\n"
            "Examples:\n"
            "  python scripts/session_costs.py uiej9qvu2t\n"
            "  python scripts/session_costs.py uiej9qvu2t --json\n"
            "  python scripts/session_costs.py --list-recent\n"
        ),
    )
    parser.add_argument("session_id", nargs="?",
                        help="session id (from 'Session saved: <id>')")
    parser.add_argument("--list-recent", "-l", type=int, nargs="?", const=15,
                        metavar="N", help="list recent session ids (default 15)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--db", help=f"debugger DB path (default: {DEFAULT_DB})")
    parser.add_argument("--pricing", help=f"pricing YAML (default: {DEFAULT_PRICING})")
    parser.add_argument("--log", help="explicit log file (default: logs/cli.log+api.log)")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DEFAULT_DB
    if not db_path.exists():
        print(f"ERROR: debugger DB not found: {db_path}", file=sys.stderr)
        return 2

    session_to_roots, root_to_session = scan_session_map(_log_files(args.log))

    conn = sqlite3.connect(str(db_path))
    try:
        if args.list_recent is not None:
            list_recent(conn, root_to_session, args.list_recent)
            return 0
        if not args.session_id:
            parser.print_help()
            return 1

        sid = args.session_id
        # Primary: resolve the session id to its request-id root(s) via the logs.
        roots = [r for r in session_to_roots.get(sid, []) if _root_exists(conn, r)]
        # Power-user fallback: the argument is itself a request-id root.
        if not roots and _root_exists(conn, _root_of(sid)):
            roots = [_root_of(sid)]
            print(f"(note: '{sid}' matched a request-id root directly, not a "
                  f"session id)", file=sys.stderr)

        if not roots:
            hint = ("session not found in logs (cli.log/api.log may have rotated)"
                    if sid not in session_to_roots else
                    "session found in logs but its request rows are not in the "
                    "debugger (pruned by retention?)")
            print(f"ERROR: no cost data for session '{sid}': {hint}.\n"
                  f"       Try --list-recent to see resolvable sessions.",
                  file=sys.stderr)
            return 2

        pricing = load_pricing(Path(args.pricing) if args.pricing else DEFAULT_PRICING)
        stats = tree_stats(conn, roots)
        rows, warnings = calc_tree_costs(conn, roots, pricing)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(build_json(sid, roots, stats, rows, warnings),
                         ensure_ascii=False, indent=2))
    else:
        print_human(sid, roots, stats, rows, warnings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
