"""Data models for SSH control operations."""

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Literal


class MachineConfig(BaseModel):
    """Configuration for a single SSH machine."""
    
    name: str
    host: str
    port: int = 22
    username: str
    auth_method: Literal['key', 'password', 'agent'] = 'key'
    key_path: str | None = None
    key_passphrase: str | None = None
    password: str | None = None
    connection_timeout: int = 10
    command_timeout: int = 300
    max_connections: int = Field(3, ge=1)  # 0 would stall every command, <0 raises
    tags: list[str] = Field(default_factory=list)


class CommandResult(BaseModel):
    """Result of command execution on remote machine."""
    
    machine: str
    command: str
    stdout: str
    stderr: str
    exit_code: int
    duration: float


class FileTransferResult(BaseModel):
    """Result of file transfer operation."""
    
    machine: str
    local_path: str
    remote_path: str
    bytes_transferred: int
    duration: float
    success: bool
    error: str | None = None


class SystemInfo(BaseModel):
    """System information from remote machine."""
    
    machine: str
    os_name: str
    os_version: str
    kernel: str
    uptime_seconds: float
    load_average: list[float]
    memory_total: int
    memory_available: int
    disk_usage: dict[str, dict[str, int]]


class ConnectionStatus(BaseModel):
    """SSH connection health status."""
    
    machine: str
    connected: bool
    latency_ms: float | None = None
    error: str | None = None
    last_used: str | None = None
