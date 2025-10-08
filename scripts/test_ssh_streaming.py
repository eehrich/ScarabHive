"""Manual test script for SSH Control SSE streaming.

This script demonstrates and tests the SSE streaming functionality.
Run this manually with a real SSH server configured.

Usage:
    python -m scripts.test_ssh_streaming
"""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from plugins.ssh_control.connection_manager import SSHConnectionManager


async def test_streaming():
    """Test SSH streaming with real server."""
    
    # Configure test machine (adjust to your setup)
    config = {
        'machines': [
            {
                'name': 'test-server',
                'host': '192.0.2.114',  # Adjust to your server
                'port': 22,
                'username': 'root',
                'auth_method': 'key',
                'key_path': '~/.ssh/id_rsa',
                'tags': ['test']
            }
        ],
        'defaults': {},
        'known_hosts_file': None,
        'strict_host_key_checking': False,
        'audit_log_enabled': True
    }
    
    manager = SSHConnectionManager(config)
    
    print("=" * 80)
    print("SSH Streaming Test")
    print("=" * 80)
    
    # Test 1: Simple command with output
    print("\n[Test 1] Simple command: echo hello")
    print("-" * 80)
    
    try:
        async for event in manager.execute_command_stream(
            'test-server',
            'echo "Hello from SSH streaming!"'
        ):
            event_type = event['type']
            data = event['data']
            
            if event_type == 'start':
                print(f"✓ START: {data['command']}")
            elif event_type == 'stdout':
                print(f"  STDOUT: {data}")
            elif event_type == 'stderr':
                print(f"  STDERR: {data}")
            elif event_type == 'exit':
                print(f"✓ EXIT: code={data['exit_code']}, duration={data['duration']:.2f}s, success={data['success']}")
            elif event_type == 'error':
                print(f"✗ ERROR: {data}")
        
        print("✓ Test 1 passed\n")
    except Exception as e:
        print(f"✗ Test 1 failed: {e}\n")
    
    # Test 2: Command with multiple lines of output
    print("\n[Test 2] Multi-line output: ls -la /var/log")
    print("-" * 80)
    
    try:
        line_count = 0
        async for event in manager.execute_command_stream(
            'test-server',
            'ls -la /var/log | head -10'
        ):
            event_type = event['type']
            data = event['data']
            
            if event_type == 'start':
                print(f"✓ START: {data['command']}")
            elif event_type == 'stdout':
                line_count += 1
                print(f"  STDOUT [{line_count}]: {data}")
            elif event_type == 'stderr':
                print(f"  STDERR: {data}")
            elif event_type == 'exit':
                print(f"✓ EXIT: code={data['exit_code']}, lines={line_count}, duration={data['duration']:.2f}s")
        
        print("✓ Test 2 passed\n")
    except Exception as e:
        print(f"✗ Test 2 failed: {e}\n")
    
    # Test 3: Command with stderr
    print("\n[Test 3] Command with stderr: cat /nonexistent")
    print("-" * 80)
    
    try:
        async for event in manager.execute_command_stream(
            'test-server',
            'cat /nonexistent 2>&1'
        ):
            event_type = event['type']
            data = event['data']
            
            if event_type == 'start':
                print(f"✓ START: {data['command']}")
            elif event_type == 'stdout':
                print(f"  STDOUT: {data}")
            elif event_type == 'stderr':
                print(f"  STDERR: {data}")
            elif event_type == 'exit':
                print(f"✓ EXIT: code={data['exit_code']}, success={data['success']}")
        
        print("✓ Test 3 passed\n")
    except Exception as e:
        print(f"✗ Test 3 failed: {e}\n")
    
    # Test 4: Long-running command
    print("\n[Test 4] Long-running command: for loop")
    print("-" * 80)
    
    try:
        async for event in manager.execute_command_stream(
            'test-server',
            'for i in 1 2 3 4 5; do echo "Line $i"; sleep 0.5; done'
        ):
            event_type = event['type']
            data = event['data']
            
            if event_type == 'start':
                print(f"✓ START: {data['command']}")
            elif event_type == 'stdout':
                print(f"  STDOUT (real-time): {data}")
            elif event_type == 'stderr':
                print(f"  STDERR: {data}")
            elif event_type == 'exit':
                print(f"✓ EXIT: code={data['exit_code']}, duration={data['duration']:.2f}s")
        
        print("✓ Test 4 passed\n")
    except Exception as e:
        print(f"✗ Test 4 failed: {e}\n")
    
    # Cleanup
    await manager.close_all()
    
    print("=" * 80)
    print("All manual tests completed!")
    print("=" * 80)


if __name__ == '__main__':
    print("\nSSH Control - SSE Streaming Manual Test")
    print("=" * 80)
    print("\nThis script requires:")
    print("  1. A SSH server configured in the script (adjust host/username/key)")
    print("  2. SSH key authentication set up")
    print("  3. Network connectivity to the server")
    print("\nStarting tests...\n")
    
    try:
        asyncio.run(test_streaming())
    except KeyboardInterrupt:
        print("\n\nTests interrupted by user")
    except Exception as e:
        print(f"\n\nTests failed with error: {e}")
        import traceback
        traceback.print_exc()
