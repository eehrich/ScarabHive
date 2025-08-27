#!/usr/bin/env python3
"""Wrapper to run the backlog updater (keeps legacy path)."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).parent / 'backlog_update.py'), run_name='__main__')
