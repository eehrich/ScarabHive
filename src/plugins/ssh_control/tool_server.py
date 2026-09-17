"""SSH Control Tool Server Component

Provides tools for SSH-based remote machine control.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, TYPE_CHECKING

from agent_system.tools.schema_based import SchemaBasedToolServer

from . import machine_store
from .connection_manager import SSHConnectionManager

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)


class SSHControlToolServer(SchemaBasedToolServer):
    """tool server component for SSH control plugin."""

    def __init__(
        self,
        name: str,
        system_config: AgentSystemConfig,
        server_config: ToolServerConfig,
        command_history: deque | None = None
    ):
        """Initialize SSH control tool server.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration
            command_history: Shared command history deque (for web UI)
        """
        super().__init__(name, system_config, server_config)

        # Shared command history for web UI
        self.command_history = command_history if command_history is not None else deque(maxlen=1000)

        # Initialize connection manager with shared command history
        if isinstance(server_config, dict):
            config_dict = server_config
        elif hasattr(server_config, 'model_dump'):
            # Pydantic v2
            config_dict = server_config.model_dump()
        else:
            # Fallback
            config_dict = dict(server_config)

        # Machines added at runtime are read back HERE, which is the whole
        # point: before this, `persistent: true` wrote a file that no loader
        # included, so every "saved" machine was gone at the next start.
        #
        # A NEW dict, not an assignment into config_dict: in the dict branch
        # above that object is the caller's, and writing the merged list back
        # into it would make the stored machines look configured to the next
        # instance built from the same config -- at which point they could no
        # longer be removed from the store.
        config_dict = {**config_dict,
                       'machines': machine_store.merge_into(
                           name, config_dict.get('machines') or [])}

        self.connection_manager = SSHConnectionManager(config_dict, command_history=self.command_history)

        stranded = machine_store.legacy_machine_count()
        if stranded:
            logger.warning(
                "%s still holds %d machine(s) that nothing reads -- ssh_control "
                "wrote them there before the store moved to %s. Re-add them or "
                "move them by hand, then delete the file.",
                machine_store.LEGACY_PATH, stranded, machine_store.store_path(name),
            )

        logger.info(
            f"SSH Control Tool Server '{name}' initialized with "
            f"{len(self.connection_manager.machines)} machines"
        )

    # MCP Tool Handlers - auto-dispatched by SchemaBasedToolServer

    async def list_machines(self, params: dict[str, Any]) -> dict[str, Any]:
        """List all configured SSH machines.

        Args:
            params: Parameters containing optional tags filter

        Returns:
            Dict with list of machines
        """
        tags = params.get('tags')
        status = params.get('_status')

        # Send status update
        if status:
            await status.progress(
                "Listing: machines" + (f" with tags {tags}" if tags else ""),
                meta={'tags': tags}
            )

        machines = self.connection_manager.list_machines(tags=tags)

        logger.debug(f"Listed {len(machines)} machines" + (f" with tags {tags}" if tags else ""))

        # Send completion status
        if status:
            if len(machines) == 1:
                await status.end(
                    "Listed: 1 machine" + (f" with tags {tags}" if tags else ""),
                    meta={'count': len(machines), 'tags': tags}
                )
            else:
                await status.end(
                    f"Listed: {len(machines)} machines" + (f" with tags {tags}" if tags else ""),
                    meta={'count': len(machines), 'tags': tags}
                )

        return {
            'machines': machines,
            'count': len(machines)
        }

    async def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """Execute command on one or more remote machines.

        Args:
            params: Parameters containing machine, command, timeout, check_exit_code

        Returns:
            Dict with command results for each machine
        """
        machine = params.get('machine')
        command = params.get('command')
        timeout = params.get('timeout')
        check_exit_code = params.get('check_exit_code', True)
        status = params.get('_status')  # Injected by call_with_status

        if not machine:
            raise ValueError("Missing required parameter: machine")
        if not command:
            raise ValueError("Missing required parameter: command")

        # Handle single machine or list of machines
        machines = [machine] if isinstance(machine, str) else machine

        logger.info(f"Executing command on {len(machines)} machine(s): {command}")

        # Send status update with command details
        if status:
            machine_str = machines[0] if len(machines) == 1 else f"{len(machines)} machines"
            # Truncate command if too long for status display
            cmd_display = command if len(command) <= 60 else command[:57] + "..."
            await status.progress(
                f"Executing: {machine_str}: {cmd_display}",
                meta={
                    'machines': machines,
                    'command': command,
                    'timeout': timeout
                }
            )

        # Execute commands in parallel
        tasks = []
        for machine_name in machines:
            task = self.connection_manager.execute_command(
                machine_name,
                command,
                timeout=timeout
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        responses = []
        for machine_name, result in zip(machines, results):
            if isinstance(result, Exception):
                logger.info(f"Command failed on {machine_name}: {result}")
                response = {
                    'machine': machine_name,
                    'command': command,
                    'error': str(result),
                    'success': False
                }
            else:
                # History is logged by connection_manager.execute_command()
                response = {
                    'machine': result.machine,
                    'command': result.command,
                    'stdout': result.stdout,
                    'stderr': result.stderr,
                    'exit_code': result.exit_code,
                    'duration': result.duration,
                    # Contract change, deliberate: a caller who passes
                    # check_exit_code=false has declared a non-zero exit
                    # acceptable. Without this the counters below and the
                    # status branch called a tolerated exit a failure --
                    # while the response carried no `error` key, because the
                    # branch below only sets one when check_exit_code is on.
                    # Consumers enumerated 2026-09-02: no CODE reads this
                    # field outside this method (no endpoint, template or
                    # test); it is read by the calling AGENT, in the returned
                    # `results` list and the `successful`/`failed` counts --
                    # which is exactly where the two answers used to disagree.
                    'success': result.exit_code == 0 or not check_exit_code
                }

                # Check exit code if required
                if check_exit_code and result.exit_code != 0:
                    response['error'] = f"Command failed with exit code {result.exit_code}"

            responses.append(response)

        # Send completion status with summary
        if status:
            successful = sum(1 for r in responses if r.get('success', False))
            failed = sum(1 for r in responses if not r.get('success', False))

            # Same budget as the progress line at :134 -- the END replaces it
            # in the WebUI, so capping it harder loses information twice.
            cmd_display = command if len(command) <= 60 else command[:57] + "..."
            machine_str = machines[0] if len(machines) == 1 else f"{len(machines)} machines"
            # The outcome sits right here and used to be dropped -- one
            # short summary, not a per-machine list: the line has to fit.
            if len(machines) == 1:
                r = responses[0]
                detail = f"exit {r.get('exit_code')} in {r.get('duration', 0):.1f}s"
            else:
                detail = f"{successful} ok"

            # Outcome first: the WebUI cuts the line at the right edge, so a
            # long command in front would take the result with it.
            if failed == 0:
                await status.end(
                    f"{machine_str}: {detail} -- {cmd_display}",
                    meta={
                        'successful': successful,
                        'failed': failed,
                        'command': command
                    }
                )
            else:
                first_error = next(
                    (str(r.get('error') or r.get('stderr', ''))[:60]
                     for r in responses if not r.get('success', False)), '')
                await status.error(
                    f"{machine_str}: {successful} ok, {failed} failed"
                    + (f" ({first_error})" if first_error else "")
                    + f" -- {cmd_display}",
                    meta={
                        'successful': successful,
                        'failed': failed,
                        'command': command
                    }
                )

        return {
            'results': responses,
            'total_machines': len(machines),
            'successful': sum(1 for r in responses if r.get('success', False)),
            'failed': sum(1 for r in responses if not r.get('success', False))
        }

    async def upload_file(self, params: dict[str, Any]) -> dict[str, Any]:
        """Upload file to remote machine(s).

        Args:
            params: Parameters containing machine, local_path, remote_path, mode

        Returns:
            Dict with upload results
        """
        machine = params.get('machine')
        local_path = params.get('local_path')
        remote_path = params.get('remote_path')
        mode = params.get('mode')
        status = params.get('_status')

        if not machine:
            raise ValueError("Missing required parameter: machine")
        if not local_path:
            raise ValueError("Missing required parameter: local_path")
        if not remote_path:
            raise ValueError("Missing required parameter: remote_path")

        # Handle single machine or list of machines
        machines = [machine] if isinstance(machine, str) else machine

        logger.info(f"Uploading file to {len(machines)} machine(s): {local_path} -> {remote_path}")

        # Send status update
        if status:
            machine_str = machines[0] if len(machines) == 1 else f"{len(machines)} machines"
            import os
            filename = os.path.basename(local_path)
            await status.progress(
                f"Uploading: {machine_str}: {filename} → {remote_path}",
                meta={
                    'machines': machines,
                    'local_path': local_path,
                    'remote_path': remote_path,
                    'mode': mode
                }
            )

        # Upload to all machines in parallel
        tasks = []
        for machine_name in machines:
            task = self.connection_manager.upload_file(
                machine_name,
                local_path,
                remote_path,
                mode=mode
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        responses = []
        for machine_name, result in zip(machines, results):
            if isinstance(result, Exception):
                logger.info(f"Upload failed on {machine_name}: {result}")
                responses.append({
                    'machine': machine_name,
                    'error': str(result),
                    'success': False
                })
            else:
                responses.append({
                    'machine': result.machine,
                    'local_path': result.local_path,
                    'remote_path': result.remote_path,
                    'bytes_transferred': result.bytes_transferred,
                    'duration': result.duration,
                    'success': result.success
                })

        # Send completion status
        if status:
            successful = sum(1 for r in responses if r.get('success', False))
            failed = sum(1 for r in responses if not r.get('success', False))
            total_bytes = sum(r.get('bytes_transferred', 0) for r in responses if r.get('success'))

            # Format bytes nicely
            if total_bytes < 1024:
                size_str = f"{total_bytes} B"
            elif total_bytes < 1024 * 1024:
                size_str = f"{total_bytes / 1024:.1f} KB"
            else:
                size_str = f"{total_bytes / (1024 * 1024):.1f} MB"

            machine_str = machines[0] if len(machines) == 1 else f"{len(machines)} machines"
            # Get filename from local_path
            import os
            filename = os.path.basename(local_path)

            if failed == 0:
                await status.end(
                    f"Uploaded: {machine_str}: {filename} → {remote_path} ({size_str})",
                    meta={'successful': successful, 'bytes': total_bytes}
                )
            else:
                await status.error(
                    f"Uploaded: {machine_str}: {filename} ({successful} ok, {failed} failed)",
                    meta={'successful': successful, 'failed': failed}
                )

        return {
            'results': responses,
            'total_machines': len(machines),
            'successful': sum(1 for r in responses if r.get('success', False)),
            'failed': sum(1 for r in responses if not r.get('success', False))
        }

    async def download_file(self, params: dict[str, Any]) -> dict[str, Any]:
        """Download file from remote machine.

        Args:
            params: Parameters containing machine, remote_path, local_path

        Returns:
            Dict with download result
        """
        machine = params.get('machine')
        remote_path = params.get('remote_path')
        local_path = params.get('local_path')
        status = params.get('_status')

        if not machine:
            raise ValueError("Missing required parameter: machine")
        if not remote_path:
            raise ValueError("Missing required parameter: remote_path")
        if not local_path:
            raise ValueError("Missing required parameter: local_path")

        logger.info(f"Downloading file from {machine}: {remote_path} -> {local_path}")

        # Send status update
        if status:
            import os
            filename = os.path.basename(remote_path)
            await status.progress(
                f"Downloading: {machine}: {filename}",
                meta={
                    'machine': machine,
                    'remote_path': remote_path,
                    'local_path': local_path
                }
            )

        try:
            result = await self.connection_manager.download_file(
                machine,
                remote_path,
                local_path
            )

            # Send completion status
            if status:
                if result.bytes_transferred < 1024:
                    size_str = f"{result.bytes_transferred} B"
                elif result.bytes_transferred < 1024 * 1024:
                    size_str = f"{result.bytes_transferred / 1024:.1f} KB"
                else:
                    size_str = f"{result.bytes_transferred / (1024 * 1024):.1f} MB"

                # Get filename from remote_path
                import os
                filename = os.path.basename(remote_path)

                await status.end(
                    f"Downloaded: {machine}: {filename} ({size_str})",
                    meta={'bytes': result.bytes_transferred, 'duration': result.duration}
                )

            return {
                'machine': result.machine,
                'remote_path': result.remote_path,
                'local_path': result.local_path,
                'bytes_transferred': result.bytes_transferred,
                'duration': result.duration,
                'success': result.success
            }
        except Exception as e:
            logger.info(f"Download failed: {e}")

            # Send error status
            if status:
                import os
                filename = os.path.basename(remote_path)
                await status.error(
                    f"Downloaded: {machine}: {filename} - {str(e)}",
                    meta={'error': str(e)}
                )

            return {
                'machine': machine,
                'error': str(e),
                'success': False
            }

    async def check_connection(self, params: dict[str, Any]) -> dict[str, Any]:
        """Check SSH connection health for machine(s).

        Args:
            params: Parameters containing optional machine filter

        Returns:
            Dict with connection status for each machine
        """
        machine = params.get('machine')
        status = params.get('_status')

        # Determine which machines to check
        if machine is None:
            # Check all machines
            machines = list(self.connection_manager.machines.keys())
        elif isinstance(machine, str):
            machines = [machine]
        else:
            machines = machine

        logger.info(f"Checking connection health for {len(machines)} machine(s)")

        # Send status update
        if status:
            machine_str = machines[0] if len(machines) == 1 else f"{len(machines)} machines"
            await status.progress(
                f"Checking: {machine_str}",
                meta={'machines': machines}
            )

        # Check connections in parallel
        tasks = []
        for machine_name in machines:
            # Use lazy=False for explicit connection checks via tool
            # (LLM explicitly requested status check, so actually test connection)
            task = self.connection_manager.check_connection(machine_name, lazy=False)
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        statuses = []
        for machine_name, result in zip(machines, results):
            if isinstance(result, Exception):
                statuses.append({
                    'machine': machine_name,
                    'connected': False,
                    'error': str(result)
                })
            else:
                statuses.append(result)

        # Send completion status
        if status:
            connected = sum(1 for s in statuses if s.get('connected', False))
            disconnected = sum(1 for s in statuses if not s.get('connected', False))

            machine_str = machines[0] if len(machines) == 1 else f"{len(machines)} machines"

            if disconnected == 0:
                # Calculate average latency
                avg_latency = sum(s.get('latency_ms', 0) for s in statuses if s.get('connected')) / max(connected, 1)
                await status.end(
                    f"Connected: {machine_str} ({avg_latency:.0f}ms)",
                    meta={'connected': connected, 'avg_latency_ms': avg_latency}
                )
            else:
                # Some machines are down - this is informational, not an error
                await status.end(
                    f"Status: {machine_str}: {connected} up, {disconnected} down",
                    meta={'connected': connected, 'disconnected': disconnected}
                )

        return {
            'statuses': statuses,
            'total_machines': len(machines),
            'connected': sum(1 for s in statuses if s.get('connected', False)),
            'disconnected': sum(1 for s in statuses if not s.get('connected', False))
        }

    async def add_machine(self, params: dict[str, Any]) -> dict[str, Any]:
        """Dynamically add a new SSH machine to the connection manager.

        Args:
            params: Machine configuration parameters:
                - name: Machine name (required)
                - host: Hostname or IP address (required)
                - port: SSH port (default: 22)
                - username: SSH username (required)
                - auth_method: Authentication method - 'key', 'password', or 'agent' (default: 'key')
                - password: Password for password auth (optional)
                - key_path: Path to SSH private key (optional, default: ~/.ssh/id_rsa)
                - tags: List of tags for grouping (optional)
                - persistent: Save to config file (default: False)
                - max_connections: Max parallel connections (default: 3)

        Returns:
            Dict with success status and machine info
        """
        from .models import MachineConfig

        status = params.get('_status')

        # Extract and validate required parameters
        name = params.get('name')
        host = params.get('host')
        username = params.get('username')

        if not name or not host or not username:
            error_msg = "Missing required parameters: name, host, and username are required"
            if status:
                await status.error(error_msg)
            return {'success': False, 'error': error_msg}

        # Check for duplicate name
        if name in self.connection_manager.machines:
            error_msg = f"Machine '{name}' already exists"
            if status:
                await status.error(error_msg)
            return {'success': False, 'error': error_msg}

        # Send status update
        if status:
            await status.progress(f"Adding machine: {name} ({username}@{host})")

        # Build machine config
        port = params.get('port', 22)
        auth_method = params.get('auth_method', 'key')
        password = params.get('password')
        key_path = params.get('key_path', '~/.ssh/id_rsa')
        tags = params.get('tags', [])
        persistent = params.get('persistent', False)
        max_connections = params.get('max_connections', 3)

        try:
            # Create MachineConfig
            machine_config = MachineConfig(
                name=name,
                host=host,
                port=port,
                username=username,
                auth_method=auth_method,
                password=password,
                key_path=key_path,
                tags=tags if isinstance(tags, list) else [],
                max_connections=max_connections
            )

            # Test connection before adding
            if status:
                await status.progress(f"Testing connection: {name}")

            logger.info(f"Testing SSH connection to {name} ({username}@{host}:{port})")

            # Import connection test
            import asyncssh
            from .auth import SSHAuthenticator

            try:
                # Attempt to create a connection
                conn = await asyncio.wait_for(
                    SSHAuthenticator.create_connection(
                        machine_config,
                        self.connection_manager.known_hosts_file,
                        self.connection_manager.strict_host_key_checking
                    ),
                    timeout=10.0
                )

                # Test with simple command
                result = await asyncio.wait_for(
                    conn.run('echo "Connection test"', check=False),
                    timeout=5.0
                )

                conn.close()

                if result.exit_status != 0:
                    error_msg = f"Connection test failed: exit code {result.exit_status}"
                    if status:
                        await status.error(error_msg)
                    return {'success': False, 'error': error_msg}

                logger.info(f"Connection test successful for {name}")

            except asyncio.TimeoutError:
                error_msg = f"Connection timeout for {name} after 10 seconds"
                if status:
                    await status.error(error_msg)
                return {'success': False, 'error': error_msg}
            except asyncssh.Error as e:
                error_msg = f"SSH connection failed: {e}"
                if status:
                    await status.error(error_msg)
                return {'success': False, 'error': error_msg}
            except Exception as e:
                error_msg = f"Connection test failed: {e}"
                if status:
                    await status.error(error_msg)
                return {'success': False, 'error': error_msg}

            # Add to connection manager
            self.connection_manager.machines[name] = machine_config
            logger.info(f"Added machine '{name}' to connection manager")

            # Persist if requested. What actually HAPPENED is tracked here:
            # the except deliberately does not fail the operation, but the
            # end line and the result used to claim persistence from the
            # requested flag either way.
            config_persisted = False
            config_error: str | None = None
            config_path = machine_store.store_path(self.name)
            if persistent:
                if status:
                    await status.progress(f"Saving to the machine store: {name}")

                # Password and passphrase are deliberately left out -- the
                # store is a plain file, and a key path is a reference while
                # a password is the secret itself.
                stored = {
                    'name': name,
                    'host': host,
                    'port': port,
                    'username': username,
                    'auth_method': auth_method,
                    'max_connections': max_connections,
                }
                if auth_method == 'key':
                    # ALWAYS, including the default path: an entry without it
                    # is read back at the next start and then dies in auth.py
                    # with "Key path required", while occupying the name.
                    # Omitting it was harmless only while nothing read the
                    # file back.
                    stored['key_path'] = key_path
                if tags:
                    stored['tags'] = tags

                # A machine that cannot be restored is not stored at all --
                # see machine_store.unrestorable_reason. Reported here, while
                # the operator is present, instead of failing at the next
                # start with a name that can no longer be re-added.
                config_error = machine_store.unrestorable_reason(stored)
                if config_error:
                    logger.info(f"Not storing '{name}': {config_error}")
                else:
                    try:
                        machine_store.add(self.name, stored)
                        logger.info(f"Stored machine '{name}' in {config_path}")
                        config_persisted = True
                    except Exception as e:
                        config_error = str(e)
                        logger.error(f"Failed to store machine '{name}': {e}", exc_info=True)
                        # Don't fail the operation, just log the error

            # Send completion status
            if persistent and config_persisted:
                where = f", saved to {config_path}"
            elif persistent:
                where = f", NOT stored: {config_error}"
            else:
                where = " (this session only)"
            if status:
                await status.end(
                    f"Added machine: {name} ({username}@{host}:{port}){where}",
                    meta={'machine': name, 'host': host,
                          'persistent': config_persisted}
                )

            return {
                'success': True,
                'machine': name,
                'host': host,
                'port': port,
                'username': username,
                'auth_method': auth_method,
                'tags': tags,
                'persistent': config_persisted,
                'config_error': config_error,
                'message': f"Machine '{name}' added successfully{where}"
            }

        except Exception as e:
            error_msg = f"Failed to add machine: {e}"
            logger.error(error_msg, exc_info=True)
            if status:
                await status.error(error_msg)
            return {'success': False, 'error': error_msg}

    async def remove_machine(self, params: dict[str, Any]) -> dict[str, Any]:
        """Remove a dynamically added SSH machine from the connection manager.

        Args:
            params: Parameters containing:
                - name: Machine name to remove (required)
                - remove_from_config: Also remove from config file if persistent (default: False)

        Returns:
            Dict with success status
        """

        status = params.get('_status')
        name = params.get('name')
        remove_from_config = params.get('remove_from_config', False)

        if not name:
            error_msg = "Missing required parameter: name"
            if status:
                await status.error(error_msg)
            return {'success': False, 'error': error_msg}

        # Check if machine exists
        if name not in self.connection_manager.machines:
            error_msg = f"Machine '{name}' not found"
            if status:
                await status.error(error_msg)
            return {'success': False, 'error': error_msg}

        # Send status update
        if status:
            await status.progress(f"Removing machine: {name}")

        try:
            # Close all connections for this machine
            if name in self.connection_manager.pools:
                logger.info(f"Closing connection pool for '{name}'")
                await self.connection_manager.pools[name].close_all()
                del self.connection_manager.pools[name]

            # Remove from machines dict
            del self.connection_manager.machines[name]
            logger.info(f"Removed machine '{name}' from connection manager")

            # Remove from the store if requested. Same as add_machine: what
            # the end line reports is what HAPPENED, not what was asked for.
            config_removed = False
            config_error: str | None = None
            config_path = machine_store.store_path(self.name)
            if remove_from_config:
                if status:
                    await status.progress(f"Removing from the machine store: {name}")

                try:
                    config_removed = machine_store.remove(self.name, name)
                    if config_removed:
                        logger.info(f"Removed machine '{name}' from {config_path}")
                    else:
                        # Not an error: a machine from config/agents/*.yaml was
                        # never in the store, and saying so beats claiming a
                        # removal that did not happen.
                        config_error = f"'{name}' is not in {config_path}"
                except Exception as e:
                    config_error = str(e)
                    logger.error(f"Failed to remove '{name}' from the store: {e}", exc_info=True)
                    # Don't fail the operation, just log the error

            # Send completion status
            if remove_from_config and config_removed:
                where = f", deleted from {config_path}"
            elif remove_from_config:
                where = f", NOT deleted from the store: {config_error}"
            else:
                where = " (this session only)"
            if status:
                await status.end(
                    f"Removed machine: {name}{where}",
                    meta={'machine': name, 'removed_from_config': config_removed}
                )

            return {
                'success': True,
                'machine': name,
                'removed_from_config': config_removed,
                'config_error': config_error,
                'message': f"Machine '{name}' removed successfully{where}"
            }

        except Exception as e:
            error_msg = f"Failed to remove machine: {e}"
            logger.error(error_msg, exc_info=True)
            if status:
                await status.error(error_msg)
            return {'success': False, 'error': error_msg}

    async def close(self) -> None:
        """Clean up resources."""
        logger.info(f"Closing SSH Control Tool Server '{self.name}'")
        await self.connection_manager.close_all()
