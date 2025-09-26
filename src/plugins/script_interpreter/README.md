# Script Interpreter Plugin

The Script Interpreter plugin provides secure Python code execution in a sandboxed environment. It supports mathematical expressions, basic operations, and simple programming constructs with built-in security restrictions and timeout protection.

## Overview

This plugin enables safe execution of Python code snippets for calculations, data processing, and simple programming tasks. It operates in a secure sandbox with restricted function access, timeout protection, and comprehensive error handling.

## Features

### Core Operations
- **Code Execution**: Execute Python expressions and statements
- **Syntax Validation**: Check Python syntax without execution
- **Sandbox Reset**: Clear variables and reset environment
- **Security Sandboxing**: Restricted function and import access
- **Timeout Protection**: Automatic termination of long-running code

### Security Features
- **Sandboxed Environment**: Isolated execution context
- **Function Whitelist**: Only safe built-in functions allowed
- **Import Restrictions**: No external library imports
- **Network Isolation**: No network access from executed code
- **Memory Limits**: Configurable memory usage limits
- **Timeout Enforcement**: Automatic termination after time limit

## Configuration

Configure the Script Interpreter plugin in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
  - script_interpreter

servers:
  script_interpreter:
    type: script_interpreter
    # Optional security configuration
    # timeout: 5.0
    # memory_limit: "64MB"
    # allow_imports: false
```

### Environment Variables
- `SCRIPT_TIMEOUT`: Default execution timeout (default: 5.0 seconds)
- `SCRIPT_MEMORY_LIMIT`: Memory limit (default: 64MB)
- `SCRIPT_MAX_OUTPUT`: Maximum output length (default: 1000 chars)

## Usage Examples

### Basic Code Execution
```python
# Simple mathematical expression
{
  "action": "eval",
  "code": "(2 + 3) * 4"
}

# Variable assignment and calculation
{
  "action": "eval",
  "code": "x = 42\ny = x * 2\nabs(y - 100)"
}
```

### Advanced Operations
```python
# Built-in function usage
{
  "action": "eval", 
  "code": "sum([1, 2, 3, 4, 5])"
}

# String operations
{
  "action": "eval",
  "code": "text = 'Hello World'\ntext.upper().replace('WORLD', 'Python')"
}

# List comprehensions
{
  "action": "eval",
  "code": "[x**2 for x in range(1, 6)]"
}
```

### Syntax Validation
```python
# Valid syntax check
{
  "action": "validate",
  "code": "x = 2 + 3\nprint(x)"
}

# Invalid syntax check
{
  "action": "validate", 
  "code": "x = 2 +"
}
```

### Environment Management
```python
# Reset sandbox environment
{
  "action": "reset"
}

# Check current variables after reset
{
  "action": "eval",
  "code": "locals().keys()"
}
```

## API Reference

### Parameters

- **action** (required): Operation to perform
  - `eval` - Execute Python code (default)
  - `validate` - Check syntax without execution
  - `reset` - Clear sandbox environment

- **code** (required for eval/validate): Python code to execute or validate
  - Supports expressions and statements
  - Multiple lines supported with `\n`
  - Variable assignments persist within session

### Response Format

#### Execution Response (eval)
```json
{
  "result": "20",
  "variables": {
    "x": 10,
    "y": 10,
    "_": 20
  },
  "execution_time": 0.002,
  "output": "Result: 20",
  "type": "int"
}
```

#### Validation Response (validate)
```json
{
  "valid": true,
  "message": "Syntax is valid",
  "code_type": "assignment"
}
```

#### Error Response
```json
{
  "error": "NameError: name 'undefined_var' is not defined",
  "suggestion": "Check that all variables are defined before use. Available variables: x, y",
  "line_number": 2,
  "error_type": "NameError"
}
```

#### Reset Response
```json
{
  "message": "Sandbox environment reset successfully",
  "status": "success",
  "variables_cleared": 5
}
```

## Allowed Functions

The sandbox includes these safe built-in functions:

### Mathematical Functions
- `abs()` - Absolute value
- `min()`, `max()` - Minimum and maximum values
- `sum()` - Sum of iterable
- `round()` - Round to nearest integer
- `pow()` - Power function
- `divmod()` - Division and remainder

### Data Type Functions
- `len()` - Length of sequence
- `str()`, `int()`, `float()` - Type conversions
- `bool()` - Boolean conversion
- `list()`, `tuple()`, `dict()`, `set()` - Collection constructors

### Iteration Functions
- `all()`, `any()` - Boolean operations on iterables
- `enumerate()` - Add counter to iterable
- `zip()` - Combine iterables
- `sorted()`, `reversed()` - Sorting and reversing

### String Functions
- `chr()`, `ord()` - Character/ASCII conversions
- String methods: `.upper()`, `.lower()`, `.strip()`, etc.

### Number Base Conversions  
- `bin()` - Binary representation
- `hex()` - Hexadecimal representation
- `oct()` - Octal representation

## Security Restrictions

### Blocked Operations
- **Import Statements**: No `import` or `from` statements
- **File Operations**: No file system access
- **Network Access**: No socket or HTTP operations
- **System Calls**: No `os`, `sys`, or subprocess access
- **Dangerous Functions**: No `eval()`, `exec()`, `compile()`

### Safe Programming Constructs
- **Variables**: Assignment and reference
- **Arithmetic**: All mathematical operations
- **String Operations**: String methods and formatting
- **List Operations**: List comprehensions, slicing, methods
- **Control Flow**: `if`, `for`, `while` statements (with timeout)
- **Functions**: `def` statements with restrictions

## Example Use Cases

### Mathematical Calculations
```python
# Complex mathematical expression
{
  "action": "eval",
  "code": "import math\nmath.sqrt(144) + math.pi * 2"
}
# Note: This will fail due to import restrictions

# Alternative using built-ins
{
  "action": "eval", 
  "code": "144**0.5 + 3.14159 * 2"
}
```

### Data Processing
```python
# List processing
{
  "action": "eval",
  "code": "data = [1, 2, 3, 4, 5]\nprocessed = [x * 2 for x in data if x % 2 == 1]\nsum(processed)"
}

# String analysis
{
  "action": "eval",
  "code": "text = 'Hello World'\nwords = text.split()\nlengths = [len(word) for word in words]\nmax(lengths)"
}
```

### Algorithm Implementation
```python
# Simple algorithms (within time limits)
{
  "action": "eval",
  "code": "def fibonacci(n):\n    if n <= 1:\n        return n\n    return fibonacci(n-1) + fibonacci(n-2)\nfibonacci(8)"
}
```

## Performance Considerations

### Execution Limits
- **Timeout**: 5 second default execution limit
- **Memory**: 64MB default memory limit  
- **Output**: 1000 character output limit
- **Recursion**: Python default recursion limit applies

### Optimization Tips
- **Simple Operations**: Prefer built-in functions over loops
- **Avoid Recursion**: Use iterative approaches when possible
- **Limit Output**: Keep print statements brief
- **Variable Cleanup**: Use reset action to clear memory

## Error Handling

### Syntax Errors
```json
{
  "error": "SyntaxError: invalid syntax",
  "suggestion": "Check for missing colons, parentheses, or indentation errors",
  "line_number": 1,
  "character_position": 8
}
```

### Runtime Errors
```json
{
  "error": "ZeroDivisionError: division by zero", 
  "suggestion": "Add a check to ensure the denominator is not zero",
  "line_number": 2
}
```

### Security Violations
```json
{
  "error": "SecurityError: Import statements are not allowed",
  "suggestion": "Use only built-in functions. Available functions: abs, min, max, sum, len, etc."
}
```

### Timeout Errors
```json
{
  "error": "TimeoutError: Code execution exceeded 5.0 seconds",
  "suggestion": "Simplify the code or use more efficient algorithms to reduce execution time"
}
```

## Troubleshooting

### Common Issues

1. **Import Errors**
   - **Problem**: Cannot import external libraries
   - **Solution**: Use only built-in functions and standard operations
   - **Alternative**: Implement functionality using available built-ins

2. **Timeout Errors**
   - **Problem**: Code takes too long to execute
   - **Solution**: Simplify algorithms, avoid deep recursion
   - **Alternative**: Break complex operations into smaller steps

3. **Variable Persistence**
   - **Problem**: Variables from previous executions interfering
   - **Solution**: Use `reset` action to clear environment
   - **Alternative**: Use unique variable names

4. **Output Truncation**
   - **Problem**: Long outputs are cut off
   - **Solution**: Limit output length in code
   - **Alternative**: Process results in smaller chunks

### Best Practices

- **Keep It Simple**: Use straightforward Python constructs
- **Test Incrementally**: Build complex operations step by step
- **Handle Errors**: Use try/except blocks where appropriate
- **Reset Regularly**: Clear environment between unrelated operations
- **Validate First**: Use validate action for complex code before execution