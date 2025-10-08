"""SSH connection pool management."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import asyncssh

from .auth import SSHAuthenticator
from .models import MachineConfig, CommandResult, FileTransferResult

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ConnectionPool:
    """Connection pool for a single SSH machine."""
    
    def __init__(self, machine_config: MachineConfig, known_hosts_file: str | None, strict_host_key_checking: bool):
        """Initialize connection pool.
        
        Args:
            machine_config: Machine configuration
            known_hosts_file: Path to known_hosts file
            strict_host_key_checking: Whether to verify host keys
        """
        self.config = machine_config
        self.known_hosts_file = known_hosts_file
        self.strict_host_key_checking = strict_host_key_checking
        self.max_connections = machine_config.max_connections
        
        # Connection pool
        self.available: asyncio.Queue[asyncssh.SSHClientConnection] = asyncio.Queue()
        self.in_use: set[asyncssh.SSHClientConnection] = set()
        self.total_created = 0
        
        # Statistics
        self.last_used = time.time()
        self.total_commands = 0
        
        self._lock = asyncio.Lock()
    
    async def acquire(self) -> asyncssh.SSHClientConnection:
        """Acquire connection from pool.
        
        Returns:
            SSH connection
            
        Raises:
            asyncssh.Error: On connection failure
        """
        self.last_used = time.time()
        
        # Try to get existing connection
        try:
            conn = self.available.get_nowait()
            
            # Verify connection is still alive
            try:
                # Simple keepalive check
                result = await asyncio.wait_for(
                    conn.run('echo 1', check=False),
                    timeout=5.0
                )
                if result.exit_status == 0:
                    async with self._lock:
                        self.in_use.add(conn)
                    logger.debug(f"Reusing connection to {self.config.name}")
                    return conn
                else:
                    # Connection broken, close it
                    conn.close()
            except (asyncio.TimeoutError, asyncssh.Error):
                # Connection broken, close it
                conn.close()
        except asyncio.QueueEmpty:
            pass
        
        # Create new connection if under limit
        async with self._lock:
            if self.total_created < self.max_connections:
                logger.info(f"Creating new connection to {self.config.name} ({self.total_created + 1}/{self.max_connections})")
                conn = await SSHAuthenticator.create_connection(
                    self.config,
                    self.known_hosts_file,
                    self.strict_host_key_checking
                )
                self.total_created += 1
                self.in_use.add(conn)
                return conn
        
        # Wait for available connection
        logger.debug(f"Waiting for available connection to {self.config.name}")
        conn = await self.available.get()
        
        # Verify connection is still alive
        try:
            result = await asyncio.wait_for(
                conn.run('echo 1', check=False),
                timeout=5.0
            )
            if result.exit_status == 0:
                async with self._lock:
                    self.in_use.add(conn)
                return conn
            else:
                # Connection broken, recreate
                conn.close()
                async with self._lock:
                    self.total_created -= 1
                return await self.acquire()  # Recursive retry
        except (asyncio.TimeoutError, asyncssh.Error):
            # Connection broken, recreate
            conn.close()
            async with self._lock:
                self.total_created -= 1
            return await self.acquire()  # Recursive retry
    
    async def release(self, conn: asyncssh.SSHClientConnection) -> None:
        """Release connection back to pool.
        
        Args:
            conn: SSH connection to release
        """
        async with self._lock:
            if conn in self.in_use:
                self.in_use.remove(conn)
        
        try:
            # Verify connection is still alive
            result = await asyncio.wait_for(
                conn.run('echo 1', check=False),
                timeout=5.0
            )
            if result.exit_status == 0:
                await self.available.put(conn)
                logger.debug(f"Released connection to {self.config.name}")
            else:
                conn.close()
                async with self._lock:
                    self.total_created -= 1
                logger.warning(f"Connection to {self.config.name} is dead, closed it")
        except (asyncio.TimeoutError, asyncssh.Error):
            conn.close()
            async with self._lock:
                self.total_created -= 1
            logger.warning(f"Connection to {self.config.name} is dead, closed it", exc_info=True)
    
    async def close_all(self) -> None:
        """Close all connections in pool."""
        logger.info(f"Closing all connections to {self.config.name}")
        
        # Close in-use connections
        async with self._lock:
            for conn in list(self.in_use):
                conn.close()
            self.in_use.clear()
        
        # Close available connections
        while not self.available.empty():
            try:
                conn = self.available.get_nowait()
                conn.close()
            except asyncio.QueueEmpty:
                break
        
        async with self._lock:
            self.total_created = 0


class SSHConnectionManager:
    """Manages SSH connection pools for multiple machines."""
    
    def __init__(self, config: dict, command_history=None):
        """Initialize connection manager.
        
        Args:
            config: Plugin configuration containing machines list
            command_history: Optional shared deque for command history
        """
        self.machines: dict[str, MachineConfig] = {}
        self.pools: dict[str, ConnectionPool] = {}
        
        # Command history for web UI (shared with web_endpoints)
        self.command_history = command_history
        
        # Security settings
        security_config = config.get('security', {})
        self.strict_host_key_checking = security_config.get('strict_host_key_checking', True)
        self.known_hosts_file = security_config.get('known_hosts_file', '~/.ssh/known_hosts')
        
        # Audit logging
        self.audit_log_enabled = security_config.get('audit_log', True)
        self.audit_log_file = security_config.get('audit_log_file', 'logs/ssh_control_audit.log')
        
        # Load machine configurations
        self._load_config(config)
        
        logger.info(f"Initialized SSH connection manager with {len(self.machines)} machines")
    
    def _load_config(self, config: dict) -> None:
        """Load machine configurations from config.
        
        Args:
            config: Plugin configuration
        """
        machines_config = config.get('machines', [])
        defaults = config.get('defaults', {})
        
        for machine_data in machines_config:
            # Apply defaults
            machine_dict = {**defaults, **machine_data}
            
            # Create machine config
            try:
                machine_config = MachineConfig(**machine_dict)
                self.machines[machine_config.name] = machine_config
                logger.debug(f"Loaded config for machine: {machine_config.name}")
            except Exception as e:
                logger.error(f"Failed to load machine config: {machine_data.get('name', 'unknown')}: {e}", exc_info=True)
    
    async def _get_pool(self, machine_name: str) -> ConnectionPool:
        """Get or create connection pool for machine.
        
        Args:
            machine_name: Name of the machine
            
        Returns:
            Connection pool for the machine
            
        Raises:
            ValueError: If machine not configured
        """
        if machine_name not in self.machines:
            raise ValueError(f"Unknown machine: {machine_name}. Available: {list(self.machines.keys())}")
        
        if machine_name not in self.pools:
            machine_config = self.machines[machine_name]
            self.pools[machine_name] = ConnectionPool(
                machine_config,
                self.known_hosts_file,
                self.strict_host_key_checking
            )
        
        return self.pools[machine_name]
    
    async def execute_command(
        self,
        machine_name: str,
        command: str,
        timeout: int | None = None
    ) -> CommandResult:
        """Execute command on remote machine.
        
        Args:
            machine_name: Name of the machine
            command: Command to execute
            timeout: Command timeout in seconds (uses machine default if None)
            
        Returns:
            Command execution result
            
        Raises:
            ValueError: If machine not configured
            asyncssh.Error: On connection or execution failure
        """
        pool = await self._get_pool(machine_name)
        conn = await pool.acquire()
        
        start_time = time.time()
        try:
            # Use machine's default timeout if not specified
            if timeout is None:
                timeout = pool.config.command_timeout
            
            logger.debug(f"Executing command on {machine_name}: {command}")
            
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=timeout
            )
            
            duration = time.time() - start_time
            
            pool.total_commands += 1
            
            command_result = CommandResult(
                machine=machine_name,
                command=command,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_status or 0,
                duration=duration
            )
            
            # Add to command history if available (include stdout/stderr)
            if self.command_history is not None:
                # Store full output (up to 10KB to avoid memory issues)
                stdout_full = result.stdout[:10000] if result.stdout else ''
                stderr_full = result.stderr[:10000] if result.stderr else ''
                self.command_history.append({
                    'machine': machine_name,
                    'command': command,
                    'stdout_preview': stdout_full,
                    'stderr_preview': stderr_full,
                    'exit_code': command_result.exit_code,
                    'duration': duration,
                    'timestamp': start_time,
                    'success': command_result.exit_code == 0
                })
            
            # Audit log
            if self.audit_log_enabled:
                self._audit_log('execute_command', machine_name, {
                    'command': command,
                    'exit_code': command_result.exit_code,
                    'duration': duration
                })
            
            logger.debug(f"Command completed on {machine_name}: exit_code={command_result.exit_code}, duration={duration:.2f}s")
            
            return command_result
            
        except asyncio.TimeoutError:
            duration = time.time() - start_time
            logger.error(f"Command timeout on {machine_name} after {duration:.2f}s: {command}")
            raise
        finally:
            await pool.release(conn)
    
    async def execute_command_stream(
        self,
        machine_name: str,
        command: str,
        timeout: int | None = None
    ):
        """Execute command with streaming output (async generator for SSE).
        
        Args:
            machine_name: Name of the machine
            command: Command to execute
            timeout: Command timeout in seconds (uses machine default if None)
            
        Yields:
            Dict with 'type' (stdout/stderr/exit/error) and 'data' (output line or exit code)
            
        Raises:
            ValueError: If machine not configured
            asyncssh.Error: On connection or execution failure
        """
        pool = await self._get_pool(machine_name)
        conn = await pool.acquire()
        
        start_time = time.time()
        try:
            # Use machine's default timeout if not specified
            if timeout is None:
                timeout = pool.config.command_timeout
            
            logger.debug(f"Executing streaming command on {machine_name}: {command}")
            
            # Yield start event
            yield {
                'type': 'start',
                'data': {
                    'machine': machine_name,
                    'command': command,
                    'timestamp': start_time
                }
            }
            
            # Create SSH process for real-time output
            stdout_lines = []
            stderr_lines = []
            
            async with conn.create_process(command) as process:
                # Read stdout and stderr line by line
                stdout_done = False
                stderr_done = False
                
                while not (stdout_done and stderr_done):
                    # Try reading from both streams with timeout
                    try:
                        # Read stdout
                        if not stdout_done:
                            try:
                                line = await asyncio.wait_for(process.stdout.readline(), timeout=0.1)
                                if line:
                                    line_stripped = line.rstrip('\n')
                                    stdout_lines.append(line_stripped)
                                    yield {
                                        'type': 'stdout',
                                        'data': line_stripped
                                    }
                                else:
                                    stdout_done = True
                            except asyncio.TimeoutError:
                                pass
                        
                        # Read stderr
                        if not stderr_done:
                            try:
                                line = await asyncio.wait_for(process.stderr.readline(), timeout=0.1)
                                if line:
                                    line_stripped = line.rstrip('\n')
                                    stderr_lines.append(line_stripped)
                                    yield {
                                        'type': 'stderr',
                                        'data': line_stripped
                                    }
                                else:
                                    stderr_done = True
                            except asyncio.TimeoutError:
                                pass
                        
                        # Check if process finished
                        if process.returncode is not None:
                            break
                            
                    except Exception as e:
                        logger.error(f"Error reading stream: {e}")
                        break
                
                # Wait for process to complete
                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    process.kill()
                    duration = time.time() - start_time
                    yield {
                        'type': 'error',
                        'data': f'Command timeout after {duration:.2f}s'
                    }
                    raise
                
                # Read any remaining output
                remaining_stdout = await process.stdout.read()
                if remaining_stdout:
                    for line in remaining_stdout.splitlines():
                        stdout_lines.append(line)
                        yield {
                            'type': 'stdout',
                            'data': line
                        }
                
                remaining_stderr = await process.stderr.read()
                if remaining_stderr:
                    for line in remaining_stderr.splitlines():
                        stderr_lines.append(line)
                        yield {
                            'type': 'stderr',
                            'data': line
                        }
                
                # Yield exit code
                exit_code = process.returncode or 0
                duration = time.time() - start_time
                
                pool.total_commands += 1
                
                # Add to command history if available (with collected output)
                if self.command_history is not None:
                    stdout_full = '\n'.join(stdout_lines)
                    stderr_full = '\n'.join(stderr_lines)
                    # Store full output (up to 10KB to avoid memory issues)
                    self.command_history.append({
                        'machine': machine_name,
                        'command': command,
                        'stdout_preview': stdout_full[:10000] if stdout_full else '',
                        'stderr_preview': stderr_full[:10000] if stderr_full else '',
                        'exit_code': exit_code,
                        'duration': duration,
                        'timestamp': start_time,
                        'success': exit_code == 0
                    })
                
                yield {
                    'type': 'exit',
                    'data': {
                        'exit_code': exit_code,
                        'duration': duration,
                        'success': exit_code == 0
                    }
                }
                
                # Audit log
                if self.audit_log_enabled:
                    self._audit_log('execute_command_stream', machine_name, {
                        'command': command,
                        'exit_code': exit_code,
                        'duration': duration
                    })
                
                logger.debug(f"Streaming command completed on {machine_name}: exit_code={exit_code}, duration={duration:.2f}s")
        
        except asyncio.TimeoutError:
            raise
        except Exception as e:
            logger.error(f"Streaming command failed on {machine_name}: {e}", exc_info=True)
            yield {
                'type': 'error',
                'data': str(e)
            }
        finally:
            await pool.release(conn)
    
    async def upload_file(
        self,
        machine_name: str,
        local_path: str,
        remote_path: str,
        mode: str | None = None
    ) -> FileTransferResult:
        """Upload file to remote machine via SFTP.
        
        Args:
            machine_name: Name of the machine
            local_path: Local file path to upload
            remote_path: Remote destination path
            mode: File permissions in octal format (e.g., '0644')
            
        Returns:
            File transfer result
            
        Raises:
            ValueError: If machine not configured
            FileNotFoundError: If local file doesn't exist
            asyncssh.Error: On connection or transfer failure
        """
        from pathlib import Path
        
        local_file = Path(local_path)
        if not local_file.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")
        
        pool = await self._get_pool(machine_name)
        conn = await pool.acquire()
        
        start_time = time.time()
        try:
            logger.debug(f"Uploading file to {machine_name}: {local_path} -> {remote_path}")
            
            # Start SFTP session
            async with conn.start_sftp_client() as sftp:
                await sftp.put(local_path, remote_path)
                
                # Set permissions if specified
                if mode:
                    octal_mode = int(mode, 8) if isinstance(mode, str) else mode
                    await sftp.chmod(remote_path, octal_mode)
            
            duration = time.time() - start_time
            bytes_transferred = local_file.stat().st_size
            
            result = FileTransferResult(
                machine=machine_name,
                local_path=local_path,
                remote_path=remote_path,
                bytes_transferred=bytes_transferred,
                duration=duration,
                success=True
            )
            
            # Audit log
            if self.audit_log_enabled:
                self._audit_log('upload_file', machine_name, {
                    'local_path': local_path,
                    'remote_path': remote_path,
                    'bytes': bytes_transferred,
                    'duration': duration
                })
            
            logger.info(f"File uploaded to {machine_name}: {bytes_transferred} bytes in {duration:.2f}s")
            
            return result
            
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"File upload failed on {machine_name}: {e}", exc_info=True)
            return FileTransferResult(
                machine=machine_name,
                local_path=local_path,
                remote_path=remote_path,
                bytes_transferred=0,
                duration=duration,
                success=False,
                error=str(e)
            )
        finally:
            await pool.release(conn)
    
    async def download_file(
        self,
        machine_name: str,
        remote_path: str,
        local_path: str
    ) -> FileTransferResult:
        """Download file from remote machine via SFTP.
        
        Args:
            machine_name: Name of the machine
            remote_path: Remote file path to download
            local_path: Local destination path
            
        Returns:
            File transfer result
            
        Raises:
            ValueError: If machine not configured
            asyncssh.Error: On connection or transfer failure
        """
        from pathlib import Path
        
        pool = await self._get_pool(machine_name)
        conn = await pool.acquire()
        
        start_time = time.time()
        try:
            logger.debug(f"Downloading file from {machine_name}: {remote_path} -> {local_path}")
            
            # Start SFTP session
            async with conn.start_sftp_client() as sftp:
                await sftp.get(remote_path, local_path)
            
            duration = time.time() - start_time
            bytes_transferred = Path(local_path).stat().st_size
            
            result = FileTransferResult(
                machine=machine_name,
                local_path=local_path,
                remote_path=remote_path,
                bytes_transferred=bytes_transferred,
                duration=duration,
                success=True
            )
            
            # Audit log
            if self.audit_log_enabled:
                self._audit_log('download_file', machine_name, {
                    'remote_path': remote_path,
                    'local_path': local_path,
                    'bytes': bytes_transferred,
                    'duration': duration
                })
            
            logger.info(f"File downloaded from {machine_name}: {bytes_transferred} bytes in {duration:.2f}s")
            
            return result
            
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"File download failed on {machine_name}: {e}", exc_info=True)
            return FileTransferResult(
                machine=machine_name,
                local_path=local_path,
                remote_path=remote_path,
                bytes_transferred=0,
                duration=duration,
                success=False,
                error=str(e)
            )
        finally:
            await pool.release(conn)
    
    async def check_connection(self, machine_name: str) -> dict[str, any]:
        """Check SSH connection health for machine.
        
        Args:
            machine_name: Name of the machine
            
        Returns:
            Connection status information
        """
        try:
            pool = await self._get_pool(machine_name)
            conn = await pool.acquire()
            
            start_time = time.time()
            try:
                result = await asyncio.wait_for(
                    conn.run('echo 1', check=False),
                    timeout=5.0
                )
                latency = (time.time() - start_time) * 1000  # Convert to ms
                
                return {
                    'machine': machine_name,
                    'connected': result.exit_status == 0,
                    'latency_ms': latency,
                    'error': None,
                    'last_used': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(pool.last_used))
                }
            finally:
                await pool.release(conn)
        except Exception as e:
            logger.error(f"Connection check failed for {machine_name}: {e}", exc_info=True)
            return {
                'machine': machine_name,
                'connected': False,
                'latency_ms': None,
                'error': str(e),
                'last_used': None
            }
    
    def list_machines(self, tags: list[str] | None = None) -> list[dict[str, any]]:
        """List all configured machines.
        
        Args:
            tags: Filter by tags (optional)
            
        Returns:
            List of machine information
        """
        machines = []
        for name, config in self.machines.items():
            # Filter by tags if specified
            if tags and not any(tag in config.tags for tag in tags):
                continue
            
            machines.append({
                'name': name,
                'host': config.host,
                'port': config.port,
                'username': config.username,
                'auth_method': config.auth_method,
                'tags': config.tags
            })
        
        return machines
    
    async def close_all(self) -> None:
        """Close all connections."""
        logger.info("Closing all SSH connections")
        for pool in self.pools.values():
            await pool.close_all()
        self.pools.clear()
    
    def _audit_log(self, operation: str, machine: str, details: dict) -> None:
        """Write audit log entry.
        
        Args:
            operation: Operation type
            machine: Machine name
            details: Additional details
        """
        # Simple audit logging (could be enhanced with structured logging)
        import json
        log_entry = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'operation': operation,
            'machine': machine,
            **details
        }
        logger.info(f"AUDIT: {json.dumps(log_entry)}")
