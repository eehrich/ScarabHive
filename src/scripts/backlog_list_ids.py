#!/usr/bin/env python3
import os
import re
from collections import defaultdict
from pathlib import Path


def list_ids(path: Path):
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()

    occ = defaultdict(list)
    for i, line in enumerate(lines, start=1):
        for m in re.finditer(r"\b(?:Epic|Task)\s+(\d{4})\b", line):
            occ[m.group(1)].append(i)
    return occ


def main():
    path = Path(os.environ.get('BACKLOG_MD', 'backlog.md'))
    occ = list_ids(path)
    dups = {k: v for k, v in occ.items() if len(v) > 1}
    print('DUPLICATE IDS:')
    for k, v in sorted(dups.items()):
        print(k, '->', v)

    print('\nALL IDS AND LOCATIONS:')
    for k, v in sorted(occ.items()):
        print(k, '->', v)


if __name__ == '__main__':
    main()
