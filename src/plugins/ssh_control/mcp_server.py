"""SSH Control MCP Server Component

Provides MCP tools for SSH-based remote machine control.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer

from .connection_manager import SSHConnectionManager
from .models import CommandResult

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
        
        # Initialize connection manager
        config_dict = mcp_config if isinstance(mcp_config, dict) else vars(mcp_config)
        self.connection_manager = SSHConnectionManager(config_dict)
        
        # Shared command history for web UI
        self.command_history = command_history if command_history is not None else deque(maxlen=1000)
        
        logger.info(
            f"SSH Control MCP Server '{name}' initialized with "
            f"{len(self.connection_manager.machines)} machines"
        )
    
    def _log_command(self, machine: str, command: str, result: CommandResult) -> None:
        """Log command execution to history.
        
        Args:
            machine: Machine name
            command: Command executed
            result: Command result
        """
        self.command_history.append({
            'timestamp': time.time(),
            'machine': machine,
            'command': command,
            'exit_code': result.exit_code,
            'duration': result.duration,
            'stdout_preview': result.stdout[:200] if result.stdout else '',
            'stderr_preview': result.stderr[:200] if result.stderr else '',
            'success': result.exit_code == 0
        })
    
    # MCP Tool Handlers - auto-dispatched by SchemaBasedMCPServer
    
    async def ssh_control_list_machines(self, params: dict[str, Any]) -> dict[str, Any]:
        """List all configured SSH machines.
        
        Args:
            params: Parameters containing optional tags filter
            
        Returns:
            Dict with list of machines
        """
        tags = params.get('tags')
        machines = self.connection_manager.list_machines(tags=tags)
        
        logger.debug(f"Listed {len(machines)} machines" + (f" with tags {tags}" if tags else ""))
        
        return {
            'machines': machines,
            'count': len(machines)
        }
    
    async def ssh_control_execute(self, params: dict[str, Any]) -> dict[str, Any]:
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
        
        if not machine:
            raise ValueError("Missing required parameter: machine")
        if not command:
            raise ValueError("Missing required parameter: command")
        
        # Handle single machine or list of machines
        machines = [machine] if isinstance(machine, str) else machine
        
        logger.info(f"Executing command on {len(machines)} machine(s): {command}")
        
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
                logger.error(f"Command failed on {machine_name}: {result}", exc_info=True)
                response = {
                    'machine': machine_name,
                    'command': command,
                    'error': str(result),
                    'success': False
                }
            else:
                # Log to history
                self._log_command(machine_name, command, result)
                
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
        
        return {
            'results': responses,
            'total_machines': len(machines),
            'successful': sum(1 for r in responses if r.get('success', False)),
            'failed': sum(1 for r in responses if not r.get('success', False))
        }
    
    async def ssh_control_upload_file(self, params: dict[str, Any]) -> dict[str, Any]:
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
        
        if not machine:
            raise ValueError("Missing required parameter: machine")
        if not local_path:
            raise ValueError("Missing required parameter: local_path")
        if not remote_path:
            raise ValueError("Missing required parameter: remote_path")
        
        # Handle single machine or list of machines
        machines = [machine] if isinstance(machine, str) else machine
        
        logger.info(f"Uploading file to {len(machines)} machine(s): {local_path} -> {remote_path}")
        
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
                logger.error(f"Upload failed on {machine_name}: {result}", exc_info=True)
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
        
        return {
            'results': responses,
            'total_machines': len(machines),
            'successful': sum(1 for r in responses if r.get('success', False)),
            'failed': sum(1 for r in responses if not r.get('success', False))
        }
    
    async def ssh_control_download_file(self, params: dict[str, Any]) -> dict[str, Any]:
        """Download file from remote machine.
        
        Args:
            params: Parameters containing machine, remote_path, local_path
            
        Returns:
            Dict with download result
        """
        machine = params.get('machine')
        remote_path = params.get('remote_path')
        local_path = params.get('local_path')
        
        if not machine:
            raise ValueError("Missing required parameter: machine")
        if not remote_path:
            raise ValueError("Missing required parameter: remote_path")
        if not local_path:
            raise ValueError("Missing required parameter: local_path")
        
        logger.info(f"Downloading file from {machine}: {remote_path} -> {local_path}")
        
        try:
            result = await self.connection_manager.download_file(
                machine,
                remote_path,
                local_path
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
            logger.error(f"Download failed: {e}", exc_info=True)
            return {
                'machine': machine,
                'error': str(e),
                'success': False
            }
    
    async def ssh_control_check_connection(self, params: dict[str, Any]) -> dict[str, Any]:
        """Check SSH connection health for machine(s).
        
        Args:
            params: Parameters containing optional machine filter
            
        Returns:
            Dict with connection status for each machine
        """
        machine = params.get('machine')
        
        # Determine which machines to check
        if machine is None:
            # Check all machines
            machines = list(self.connection_manager.machines.keys())
        elif isinstance(machine, str):
            machines = [machine]
        else:
            machines = machine
        
        logger.info(f"Checking connection health for {len(machines)} machine(s)")
        
        # Check connections in parallel
        tasks = []
        for machine_name in machines:
            task = self.connection_manager.check_connection(machine_name)
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
        
        return {
            'statuses': statuses,
            'total_machines': len(machines),
            'connected': sum(1 for s in statuses if s.get('connected', False)),
            'disconnected': sum(1 for s in statuses if not s.get('connected', False))
        }
    
    async def close(self) -> None:
        """Clean up resources."""
        logger.info(f"Closing SSH Control MCP Server '{self.name}'")
        await self.connection_manager.close_all()
