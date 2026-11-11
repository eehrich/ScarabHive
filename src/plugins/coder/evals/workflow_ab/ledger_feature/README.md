# ledger

A small household ledger. One entry per line:

    YYYY-MM-DD; text; amount; category

- Blank lines and comment lines (starting with `#`, also indented) are skipped.
- An amount is a finite decimal number (`-3.50`, `2000`).
- Categories are case-insensitive and kept lower-case.

`ledger.parse` reads lines, `ledger.report` sums them per category and per month.
Run the tests with `python -m pytest`.
