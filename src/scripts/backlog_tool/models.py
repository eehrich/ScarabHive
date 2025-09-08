"""Data models for backlog parsing and manipulation.

This module contains the core data structures used throughout the backlog system.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Task:
    id: str
    title: str
    status: str
    # multiline description (optional)
    description: List[str] = field(default_factory=list)
    # timestamps
    added: Optional[str] = None
    closed: Optional[str] = None
    # list of note lines
    notes: List[str] = field(default_factory=list)
    # preserve any unknown lines so we can round-trip
    raw_lines: List[str] = field(default_factory=list)


@dataclass
class Epic(Task):
    # the tasks contained by this epic
    tasks: List[Task] = field(default_factory=list)


@dataclass
class Backlog:
    header: List[str]
    epics_open: List[Epic]
    epics_finished: List[Epic]
    footer: List[str]
