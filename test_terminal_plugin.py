"""
Quick terminal plugin test script.
"""
import asyncio
import sys
sys.path.insert(0, 'src')

from plugins.terminal.server import TerminalServer


class MockStatus:
    async def update(self, msg):
        print(f"[STATUS] {msg}")
    
    async def progress(self, msg):
        print(f"[PROGRESS] {msg}")
    
    async def complete(self, msg, meta=None):
        print(f"[COMPLETE] {msg}")
        if meta:
            print(f"  Meta: {meta}")
    
    async def error(self, msg):
        print(f"[ERROR] {msg}")


async def main():
    print("=== Terminal Plugin Test ===\n")
    
    server = TerminalServer('terminal', {}, {})
    status = MockStatus()
    
    # Test 1: Simple echo command
    print("Test 1: Simple echo command")
    result = await server.execute({
        'command': 'echo "Hello from Terminal Plugin"',
        '_status': status
    })
    print(f"Status: {result['status']}")
    print(f"Output: {result.get('stdout', '').strip()}")
    print()
    
    # Test 2: Background process
    print("Test 2: Background process")
    bg_result = await server.execute({
        'command': 'python -u -c "import time; print(\'bg output\'); time.sleep(1)"',
        'background': True,
        '_status': status
    })
    print(f"Process ID: {bg_result.get('process_id')}")
    print(f"PID: {bg_result.get('pid')}")
    
    # Wait and get output
    await asyncio.sleep(0.5)
    output = await server.get_output({
        'process_id': bg_result['process_id'],
        '_status': status
    })
    print(f"BG Output: {output.get('stdout', '').strip()}")
    print()
    
    # Test 3: Command with environment variable
    print("Test 3: Environment variable")
    env_result = await server.execute({
        'command': 'echo $MY_TEST_VAR',
        'env_vars': {'MY_TEST_VAR': 'test_value_123'},
        '_status': status
    })
    print(f"Output: {env_result.get('stdout', '').strip()}")
    print()
    
    # Test 4: Dangerous command (should be blocked)
    print("Test 4: Dangerous command (should fail)")
    danger_result = await server.execute({
        'command': 'rm -rf /',
        '_status': status
    })
    print(f"Status: {danger_result['status']}")
    print(f"Error: {danger_result.get('error', 'N/A')}")
    print()
    
    await server.cleanup()
    print("=== Tests Complete ===")


if __name__ == "__main__":
    asyncio.run(main())
