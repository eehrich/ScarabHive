#!/usr/bin/env python3
"""
Demo script showing new type checking functionality in script_interpreter.
"""

async def demo_type_checking():
    import sys
    import os
    sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))
    from plugins.script_interpreter.server import ScriptInterpreterServer
    
    class MockStatus:
        async def progress(self, msg): print(f"Progress: {msg}")
        async def error(self, msg): print(f"Error: {msg}")
        async def end(self, msg, meta=None): print(f"End: {msg}")

    server = ScriptInterpreterServer("demo", {})
    status = MockStatus()
    
    print("🔍 Type Checking Demo for Script Interpreter")
    print("=" * 50)
    
    # Demo 1: Basic isinstance usage
    print("\n1. Basic isinstance() usage:")
    code = """
data = [42, "hello", 3.14, [1, 2, 3], True, {"key": "value"}]
for item in data:
    if isinstance(item, int):
        print(f"{item} is an integer")
    elif isinstance(item, str):
        print(f"'{item}' is a string")
    elif isinstance(item, float):
        print(f"{item} is a float")
    elif isinstance(item, list):
        print(f"{item} is a list")
    elif isinstance(item, dict):
        print(f"{item} is a dict")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": status})
    print(result.get("result", result.get("error")))
    
    # Demo 2: Type conversion functions
    print("\n2. Type conversion functions:")
    code = """
print(f"int('42'): {int('42')}")
print(f"float('3.14'): {float('3.14')}")  
print(f"str(123): '{str(123)}'")
print(f"bool(1): {bool(1)}, bool(0): {bool(0)}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": status})
    print(result.get("result", result.get("error")))
    
    # Demo 3: type() function with __name__ access
    print("\n3. type() function with __name__ access:")
    code = """
values = [42, "hello", 3.14, [], {}]
for val in values:
    print(f"type({val}).__name__ = {type(val).__name__}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": status})
    print(result.get("result", result.get("error")))
    
    # Demo 4: Multiple type checks with isinstance
    print("\n4. Multiple type checks with isinstance:")
    code = """
test_data = [1, 2.5, "hello", [1, 2], {"a": 1}, True, (1, 2), {1, 2, 3}]
numbers = 0
strings = 0
collections = 0
others = 0

for item in test_data:
    if isinstance(item, str):
        strings += 1
        print(f"String: '{item}'")
    elif isinstance(item, (list, dict, tuple, set)):
        collections += 1
        print(f"Collection: {item} (type: {type(item).__name__})")
    elif isinstance(item, bool):
        others += 1
        print(f"Boolean: {item}")
    elif isinstance(item, (int, float)):
        numbers += 1
        print(f"Number: {item} (type: {type(item).__name__})")
    else:
        others += 1
        print(f"Other: {item}")

print(f"\\nCounts - Numbers: {numbers}, Strings: {strings}, Collections: {collections}, Others: {others}")
"""
    result = await server.call("execute_python_sandbox", {"code": code, "_status": status})
    print(result.get("result", result.get("error")))

if __name__ == "__main__":
    import asyncio
    asyncio.run(demo_type_checking())