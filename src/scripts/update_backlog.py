#!/usr/bin/env python3
"""Compatibility shim: run the canonical `scripts.backlog update` subcommand
while keeping the legacy script path available for tests and users.

This shim sets sys.argv so the invoked module sees the `update` subcommand.
"""
import runpy
import sys

# Ensure the invoked module sees the 'update' subcommand by default.
# Preserve any extra args passed through.
argv = sys.argv[1:]
sys.argv = [sys.argv[0], 'update'] + argv
runpy.run_module('scripts.backlog', run_name='__main__')
