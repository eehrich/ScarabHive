"""The table `plugins list` and `mcp list` print.

Both draw it with tabulate's github format and carried the same plain fallback
for an installation without tabulate, each its own copy. One copy, here: the
two listings differ only in whether the last column is padded.
"""
from __future__ import annotations

from typing import Sequence

try:
    from tabulate import tabulate  # optional dependency for pretty tables
except Exception:
    tabulate = None


def print_table(rows: Sequence[Sequence[str]], headers: Sequence[str], pad_last: bool = True) -> None:
    """Print *rows* under *headers*: tabulate's github table, else plain columns.

    The fallback pads every column to its widest cell (10 when there are no
    rows), the last one only with *pad_last*, and rules the header off.
    """
    if tabulate:
        print(tabulate(rows, headers=headers, tablefmt="github"))
        return
    # simple fallback
    if rows:
        widths = [max(len(str(r[i])) for r in rows) for i in range(len(headers))]
    else:
        widths = [10] * len(headers)
    last = len(headers) - 1

    def line(cells: Sequence[str]) -> str:
        return "  ".join(str(cell).ljust(width) if pad_last or i < last else str(cell)
                         for i, (cell, width) in enumerate(zip(cells, widths)))

    hdr = line(headers)
    print(hdr)
    print("-" * len(hdr))
    for row in rows:
        print(line(row))
