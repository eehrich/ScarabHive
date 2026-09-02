"""SSH Control MCP Server Component

Provides MCP tools for SSH-based remote machine control.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer

from .connection_manager import SSHConnectionManager

if TYPE_CHECKING:
    from agent_system.config.models import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)


class SSHControlMCPServer(SchemaBasedMCPServer):
    """MCP server component for SSH control plugin."""

    def __init__(
        self,
        name: str,
        system_config: AgentSystemConfig,
        mcp_config: MCPConfig,
        command_history: deque | None = None
    ):
        """Initialize SSH control MCP server.

        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
            command_history: Shared command history deque (for web UI)
        """
        super().__init__(name, system_config, mcp_config)

        # Shared command history for web UI
        self.command_history = command_history if command_history is not None else deque(maxlen=1000)

        # Initialize connection manager with shared command history
        if isinstance(mcp_config, dict):
            config_dict = mcp_config
        elif hasattr(mcp_config, 'model_dump'):
            # Pydantic v2
            config_dict = mcp_config.model_dump()
        else:
            # Fallback
            config_dict = dict(mcp_config)
        self.connection_manager = SSHConnectionManager(config_dict, command_history=self.command_history)

        logger.info(
            f"SSH Control MCP Server '{name}' initialized with "
            f"{len(self.connection_manager.machines)} machines"
        )

    # MCP Tool Handlers - auto-dispatched by SchemaBasedMCPServer

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
                    'success': result.exit_code == 0
                }

                # Check exit code if required
                if check_exit_code and result.exit_code != 0:
                    response['error'] = f"Command failed with exit code {result.exit_code}"

            responses.append(response)

        # Send completion status with summary
        if status:
            successful = sum(1 for r in responses if r.get('success', False))
            failed = sum(1 for r in responses if not r.get('success', False))

            # Truncate command for display
            cmd_display = command if len(command) <= 50 else command[:47] + "..."
            machine_str = machines[0] if len(machines) == 1 else f"{len(machines)} machines"

            if failed == 0:
                await status.end(
                    f"Executed: {machine_str}: {cmd_display}",
                    meta={
                        'successful': successful,
                        'failed': failed,
                        'command': command
                    }
                )
            else:
                await status.error(
                    f"Executed: {machine_str}: {cmd_display} ({successful} ok, {failed} failed)",
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
            # Use lazy=False for explicit connection checks via MCP tool
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
        import yaml
        from pathlib import Path

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

            # Persist to config if requested. What actually HAPPENED is
            # tracked here: the except below deliberately does not fail the
            # operation, but the end line and the result used to claim
            # persistence from the requested flag either way.
            config_persisted = False
            config_error: str | None = None
            config_path = Path('config/mcp.yaml')
            if persistent:
                if status:
                    await status.progress(f"Saving to config: {name}")

                try:

                    # Load existing config
                    if config_path.exists():
                        with open(config_path, 'r', encoding='utf-8') as f:
                            config = yaml.safe_load(f) or {}
                    else:
                        config = {}

                    # Ensure mcp_system.servers.ssh_control structure exists
                    if 'plugins' not in config or not isinstance(config['plugins'], dict):
                        config['plugins'] = {}

                    mcp_sys = config['plugins']
                    if 'servers' not in mcp_sys or not isinstance(mcp_sys['servers'], dict):
                        mcp_sys['servers'] = {}

                    if 'ssh_control' not in mcp_sys['servers'] or not isinstance(mcp_sys['servers']['ssh_control'], dict):
                        mcp_sys['servers']['ssh_control'] = {}

                    target = mcp_sys['servers']['ssh_control']

                    # Ensure machines list exists
                    if 'machines' not in target or not isinstance(target['machines'], list):
                        target['machines'] = []

                    # Add machine config
                    machine_dict = {
                        'name': name,
                        'host': host,
                        'port': port,
                        'username': username,
                        'auth_method': auth_method,
                        'max_connections': max_connections
                    }

                    if key_path != '~/.ssh/id_rsa':
                        machine_dict['key_path'] = key_path

                    if tags:
                        machine_dict['tags'] = tags

                    # Don't save password to config for security
                    # Key path is saved, but password is not

                    target['machines'].append(machine_dict)

                    # Write back to config
                    with open(config_path, 'w', encoding='utf-8') as f:
                        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

                    logger.info(f"Persisted machine '{name}' to {config_path}")
                    config_persisted = True

                except Exception as e:
                    config_error = str(e)
                    logger.error(f"Failed to persist machine config: {e}", exc_info=True)
                    # Don't fail the operation, just log the error

            # Send completion status
            if persistent and config_persisted:
                where = f", saved to {config_path}"
            elif persistent:
                where = f", NOT saved to config: {config_error}"
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
        import yaml
        from pathlib import Path

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

            # Remove from config if requested. Same as add_machine: what the
            # end line reports is what HAPPENED, not what was asked for.
            config_removed = False
            config_error: str | None = None
            config_path = Path('config/mcp.yaml')
            if remove_from_config:
                if status:
                    await status.progress(f"Removing from config: {name}")

                try:
                    if not config_path.exists():
                        config_error = f"{config_path} does not exist"
                    else:
                        with open(config_path, 'r', encoding='utf-8') as f:
                            config = yaml.safe_load(f) or {}

                        # Navigate to machines list
                        # Same nesting add_machine writes ('plugins' first) --
                        # without it this branch never found what add wrote and
                        # reported a removal that never happened.
                        section = (config.get('plugins') or {}).get('servers', {}).get('ssh_control')
                        machines = (section or {}).get('machines')
                        if not machines:
                            config_error = f"no machines section for ssh_control in {config_path}"
                        elif not any(m.get('name') == name for m in machines):
                            config_error = f"'{name}' is not in {config_path}"
                        else:
                            section['machines'] = [
                                m for m in machines if m.get('name') != name
                            ]

                            # Write back to config
                            with open(config_path, 'w', encoding='utf-8') as f:
                                yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

                            logger.info(f"Removed machine '{name}' from {config_path}")
                            config_removed = True

                except Exception as e:
                    config_error = str(e)
                    logger.error(f"Failed to remove from config: {e}", exc_info=True)
                    # Don't fail the operation, just log the error

            # Send completion status
            if remove_from_config and config_removed:
                where = f", deleted from {config_path}"
            elif remove_from_config:
                where = f", NOT deleted from config: {config_error}"
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
        logger.info(f"Closing SSH Control MCP Server '{self.name}'")
        await self.connection_manager.close_all()
