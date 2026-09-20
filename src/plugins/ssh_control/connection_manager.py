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
        
        # Semaphore limits concurrent connections - guarantees FIFO fairness
        self._semaphore = asyncio.Semaphore(self.max_connections)
        
        # Connection pool (reusable connections)
        self.available: asyncio.Queue[asyncssh.SSHClientConnection] = asyncio.Queue()
        self.in_use: set[asyncssh.SSHClientConnection] = set()
        self.total_created = 0
        
        # Statistics
        self.last_used = time.time()
        self.total_commands = 0
        self.last_latency_ms: float | None = None  # Cache last measured latency
        
        self._lock = asyncio.Lock()
    
    def has_free_slot(self) -> bool:
        """Whether acquire() would get a slot without waiting for another command to finish."""
        return not self._semaphore.locked()

    async def acquire(self, timeout: float = 30.0) -> asyncssh.SSHClientConnection:
        """Acquire connection from pool using semaphore for fairness.
        
        Uses a semaphore to guarantee FIFO ordering - requests are served
        in the order they arrive, preventing starvation.
        
        Args:
            timeout: Maximum time to wait for connection slot (default: 30s)
        
        Returns:
            SSH connection
            
        Raises:
            asyncssh.Error: On connection failure
            asyncio.TimeoutError: If no connection slot available within timeout
        """
        self.last_used = time.time()
        
        # Wait for semaphore slot (FIFO queue) with timeout
        try:
            acquired = await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=timeout
            )
            if not acquired:
                raise asyncio.TimeoutError("Failed to acquire semaphore")
        except asyncio.TimeoutError:
            logger.error(
                f"Timeout waiting for connection slot to {self.config.name} after {timeout}s "
                f"(max_connections={self.max_connections}, {len(self.in_use)} in use)"
            )
            raise asyncio.TimeoutError(
                f"No connection slot available to {self.config.name} within {timeout}s. "
                f"All {self.max_connections} slots are busy. "
                f"Consider increasing max_connections or reducing parallel commands."
            )
        
        # We have a slot - now get or create a connection
        try:
            conn = await self._get_or_create_connection()
            async with self._lock:
                self.in_use.add(conn)
            return conn
        except BaseException:
            # Release semaphore slot on failure, cancellation included
            self._semaphore.release()
            raise
    
    async def _get_or_create_connection(self) -> asyncssh.SSHClientConnection:
        """Get existing connection from pool or create new one.
        
        Called after semaphore is acquired, so we're guaranteed a slot.
        """
        # Try to get existing connection from pool
        while True:
            try:
                conn = self.available.get_nowait()
                
                # Verify connection is still alive
                try:
                    result = await asyncio.wait_for(
                        conn.run('echo 1', check=False),
                        timeout=5.0
                    )
                    if result.exit_status == 0:
                        logger.debug(f"Reusing connection to {self.config.name}")
                        return conn
                    else:
                        # Connection broken, close and try next
                        conn.close()
                        async with self._lock:
                            self.total_created -= 1
                except (asyncio.TimeoutError, asyncssh.Error):
                    # Connection broken, close and try next
                    conn.close()
                    async with self._lock:
                        self.total_created -= 1
            except asyncio.QueueEmpty:
                break
        
        # No reusable connection - create new one
        logger.info(f"Creating new connection to {self.config.name}")
        conn = await SSHAuthenticator.create_connection(
            self.config,
            self.known_hosts_file,
            self.strict_host_key_checking
        )
        async with self._lock:
            self.total_created += 1
        return conn
    
    async def release(self, conn: asyncssh.SSHClientConnection) -> None:
        """Release connection back to pool and free semaphore slot.
        
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
        finally:
            # Always release semaphore slot
            self._semaphore.release()
    
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
        
        # Reset semaphore to initial state
        self._semaphore = asyncio.Semaphore(self.max_connections)


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
    
    async def pool_for(self, machine_name: str) -> ConnectionPool:
        """The machine's connection pool, for callers that hold a connection
        themselves -- a background command holds one for its whole life."""
        return await self._get_pool(machine_name)

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
        start_time = time.time()
        try:
            conn = await pool.acquire()
        except Exception as e:
            # connect_timeout raises a bare TimeoutError
            error = str(e) or ("Connection timed out" if isinstance(e, asyncio.TimeoutError) else type(e).__name__)
            self._remember(machine_name, command, start_time, error=error)
            if isinstance(e, asyncio.TimeoutError):
                raise asyncio.TimeoutError(error) from e  # the agent's answer carries the reason the history does
            raise

        try:  # everything after the acquire, so that a cancelled call releases the connection too
            # Measure and cache latency on first use of this pool
            if pool.last_latency_ms is None:
                try:
                    ping_start = time.time()
                    await asyncio.wait_for(
                        conn.run('echo 1', check=False),
                        timeout=2.0
                    )
                    pool.last_latency_ms = (time.time() - ping_start) * 1000
                    logger.debug(f"Measured initial latency for {machine_name}: {pool.last_latency_ms:.1f}ms")
                except Exception as e:
                    logger.debug(f"Failed to measure latency for {machine_name}: {e}")
                    pool.last_latency_ms = 0.0  # Set to 0 to avoid retrying

            start_time = time.time()
            # Use machine's default timeout if not specified
            if timeout is None:
                timeout = pool.config.command_timeout
            
            logger.debug(f"Executing command on {machine_name}: {command}")
            
            try:
                result = await asyncio.wait_for(
                    conn.run(command, check=False),
                    timeout=timeout
                )
            except Exception as e:
                error = f"Timed out after {timeout}s" if isinstance(e, asyncio.TimeoutError) else str(e)
                logger.error(f"Command failed on {machine_name} after {time.time() - start_time:.2f}s: {command}: {error}")
                self._remember(machine_name, command, start_time, error=error)
                if isinstance(e, asyncio.TimeoutError):
                    raise asyncio.TimeoutError(error) from e
                raise
            
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
            
            self._remember(machine_name, command, start_time, stdout=result.stdout, stderr=result.stderr,
                           exit_code=command_result.exit_code)

            # Audit log
            if self.audit_log_enabled:
                self._audit_log('execute_command', machine_name, {
                    'command': command,
                    'exit_code': command_result.exit_code,
                    'duration': duration
                })
            
            logger.debug(f"Command completed on {machine_name}: exit_code={command_result.exit_code}, duration={duration:.2f}s")
            
            return command_result
            
        finally:
            await pool.release(conn)

    def _remember(self, machine_name: str, command: str, start_time: float, *, stdout: str | None = '',
                  stderr: str | None = '', exit_code: int | None = None, error: str | None = None) -> None:
        """The history the panel shows: every run, also one that failed, with up to 10 KB of each output."""
        if self.command_history is None:
            return
        self.command_history.append({
            'machine': machine_name,
            'command': command,
            'stdout_preview': (stdout or '')[:10000],
            'stderr_preview': (stderr or '')[:10000],
            'exit_code': exit_code,
            'error': error,
            'duration': time.time() - start_time,
            'timestamp': start_time,
        })

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
            logger.info(f"File upload failed on {machine_name}: {e}")
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
            logger.info(f"File download failed on {machine_name}: {e}")
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
    
    async def check_connection(self, machine_name: str, lazy: bool = True) -> dict[str, any]:
        """Check SSH connection health for machine.
        
        Args:
            machine_name: Name of the machine
            lazy: If True (default), only check existing connections without creating new ones.
                  If False, actively test connection (will create connection if needed).
            
        Returns:
            Connection status information with 'not_yet_connected' flag for lazy checks
        """
        # Check if machine exists
        if machine_name not in self.machines:
            return {
                'machine': machine_name,
                'connected': False,
                'latency_ms': None,
                'error': 'Machine not configured',
                'last_used': None,
                'not_yet_connected': False
            }
        
        # Lazy check: only check if pool has been created and has active connections
        if lazy:
            if machine_name not in self.pools:
                # Pool never created = never used
                return {
                    'machine': machine_name,
                    'connected': False,
                    'latency_ms': None,
                    'error': None,
                    'last_used': None,
                    'not_yet_connected': True
                }
            
            pool = self.pools[machine_name]
            
            # Check if pool has any connections (in use or available)
            if pool.total_created == 0:
                # Pool exists but no connections ever created
                return {
                    'machine': machine_name,
                    'connected': False,
                    'latency_ms': None,
                    'error': None,
                    'last_used': None,
                    'not_yet_connected': True
                }
            
            # Pool has connections - return status based on last_used time
            # Consider connection potentially active if used within last 5 minutes
            age_seconds = time.time() - pool.last_used
            is_recent = age_seconds < 300  # 5 minutes
            
            return {
                'machine': machine_name,
                'connected': is_recent,
                'latency_ms': pool.last_latency_ms,  # Return cached latency from last test
                'error': None,
                'last_used': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(pool.last_used)),
                'not_yet_connected': False,
                'age_seconds': age_seconds
            }
        
        # Active check: actually test the connection (creates connection if needed)
        # BUT: Don't create new connections for machines that were never used
        try:
            # If pool doesn't exist or has no connections, skip active check
            if machine_name not in self.pools or self.pools[machine_name].total_created == 0:
                # Machine never used - return not_yet_connected without creating connection
                return {
                    'machine': machine_name,
                    'connected': False,
                    'latency_ms': None,
                    'error': None,
                    'last_used': None,
                    'not_yet_connected': True
                }
            
            pool = await self._get_pool(machine_name)
            conn = await pool.acquire()
            
            start_time = time.time()
            try:
                result = await asyncio.wait_for(
                    conn.run('echo 1', check=False),
                    timeout=5.0
                )
                latency = (time.time() - start_time) * 1000  # Convert to ms
                
                # Cache the latency for lazy checks
                pool.last_latency_ms = latency
                
                return {
                    'machine': machine_name,
                    'connected': result.exit_status == 0,
                    'latency_ms': latency,
                    'error': None,
                    'last_used': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(pool.last_used)),
                    'not_yet_connected': False
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
                'last_used': None,
                'not_yet_connected': False
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
