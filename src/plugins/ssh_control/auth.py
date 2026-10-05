"""SSH authentication handling."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import asyncssh

if TYPE_CHECKING:
    from .models import MachineConfig

logger = logging.getLogger(__name__)


class SSHAuthenticator:
    """Handle SSH authentication methods."""
    
    @staticmethod
    async def create_connection(
        machine_config: MachineConfig,
        known_hosts_file: str | None = None,
        strict_host_key_checking: bool = True
    ) -> asyncssh.SSHClientConnection:
        """Create authenticated SSH connection.
        
        Args:
            machine_config: Machine configuration with auth details
            known_hosts_file: Path to known_hosts file
            strict_host_key_checking: Whether to verify host keys
            
        Returns:
            Established SSH connection
            
        Raises:
            asyncssh.Error: On connection or authentication failure
        """
        connect_kwargs = {
            'host': machine_config.host,
            'port': machine_config.port,
            'username': machine_config.username,
            'connect_timeout': machine_config.connection_timeout,
        }
        
        # Handle known_hosts.
        # SECURITY: in asyncssh, known_hosts=None DISABLES host key
        # verification entirely. When strict checking is requested but the
        # known_hosts file is missing (common on fresh hosts/containers/CI),
        # the previous code silently downgraded to "accept any key" - a silent
        # MITM exposure while the operator believes strict checking is on.
        # Fail closed instead: only disable verification when strict checking
        # is explicitly False -- an empty YAML value (None) keeps it on.
        strict_host_key_checking = strict_host_key_checking is not False
        if strict_host_key_checking and not known_hosts_file:
            # An empty or null known_hosts_file used to fall into the else
            # branch below -- known_hosts=None, verification off -- while the
            # operator had strict checking on.
            raise ValueError(
                "strict_host_key_checking is enabled but no known_hosts_file is "
                "set. Refusing to connect without host key verification."
            )
        if strict_host_key_checking:
            expanded_known_hosts = os.path.expanduser(known_hosts_file)
            if os.path.exists(expanded_known_hosts):
                connect_kwargs['known_hosts'] = expanded_known_hosts
            else:
                raise FileNotFoundError(
                    f"strict_host_key_checking is enabled but known_hosts file "
                    f"was not found: {known_hosts_file}. Refusing to connect "
                    "without host key verification (set strict_host_key_checking "
                    "to false to explicitly opt out)."
                )
        else:
            connect_kwargs['known_hosts'] = None
        
        # Handle authentication method
        if machine_config.auth_method == 'key':
            if not machine_config.key_path:
                raise ValueError(f"Key path required for key authentication on {machine_config.name}")
            
            key_path = os.path.expanduser(machine_config.key_path)
            if not os.path.exists(key_path):
                raise FileNotFoundError(f"SSH key not found: {key_path}")
            
            connect_kwargs['client_keys'] = [key_path]
            if machine_config.key_passphrase:
                connect_kwargs['passphrase'] = machine_config.key_passphrase
            
            logger.debug(f"Connecting to {machine_config.name} with key authentication")
            
        elif machine_config.auth_method == 'password':
            if not machine_config.password:
                raise ValueError(f"Password required for password authentication on {machine_config.name}")
            
            connect_kwargs['password'] = machine_config.password
            logger.debug(f"Connecting to {machine_config.name} with password authentication")
            
        elif machine_config.auth_method == 'agent':
            # asyncssh's default: the agent named by SSH_AUTH_SOCK, plus the
            # default keys in ~/.ssh. (SSHAgentClient.get_agent_path, called
            # here before, does not exist -- every agent machine failed.)
            logger.debug(f"Connecting to {machine_config.name} with SSH agent")
            
        else:
            raise ValueError(f"Unknown auth method: {machine_config.auth_method}")
        
        try:
            connection = await asyncssh.connect(**connect_kwargs)
            logger.info(f"Successfully connected to {machine_config.name} ({machine_config.host})")
            return connection
        except asyncssh.Error as e:
            logger.error(f"Failed to connect to {machine_config.name}: {e}", exc_info=True)
            raise
