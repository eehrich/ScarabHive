"""Tests for the example plugin calculator tool."""

from __future__ import annotations

import pytest

from plugins.example.server import ExampleServer


class TestCalculator:
    """Test the calculator tool functionality."""
    
    @pytest.fixture
    def server(self):
        """Create server instance for testing."""
        return ExampleServer(name="test", config={"precision": 2})
    
    @pytest.fixture
    def high_precision_server(self):
        """Create high precision server for testing."""
        return ExampleServer(name="test", config={"precision": 6})
    
    async def test_example_calculator_addition(self, server):
        """Test basic addition operation."""
        result = await server.call("test_calculator", {
            "operation": "add",
            "a": 10,
            "b": 5
        })
        
        assert result["operation"] == "add"
        assert result["operands"] == [10.0, 5.0]
        assert result["result"] == 15.0
        assert result["precision"] == 2
    
    async def test_example_calculator_subtraction(self, server):
        """Test subtraction operation."""
        result = await server.call("test_calculator", {
            "operation": "subtract",
            "a": 10,
            "b": 3
        })
        
        assert result["operation"] == "subtract"
        assert result["operands"] == [10.0, 3.0]
        assert result["result"] == 7.0
    
    async def test_example_calculator_multiplication(self, server):
        """Test multiplication operation."""
        result = await server.call("test_calculator", {
            "operation": "multiply",
            "a": 4,
            "b": 2.5
        })
        
        assert result["operation"] == "multiply"
        assert result["operands"] == [4.0, 2.5]
        assert result["result"] == 10.0
    
    async def test_example_calculator_division(self, server):
        """Test division operation."""
        result = await server.call("test_calculator", {
            "operation": "divide",
            "a": 10,
            "b": 4
        })
        
        assert result["operation"] == "divide"
        assert result["operands"] == [10.0, 4.0]
        assert result["result"] == 2.5
    
    async def test_example_calculator_division_by_zero(self, server):
        """Test division by zero error handling."""
        with pytest.raises(ValueError, match="Division by zero is not allowed"):
            await server.call("test_calculator", {
                "operation": "divide",
                "a": 10,
                "b": 0
            })
    
    async def test_example_calculator_precision(self, high_precision_server):
        """Test calculation precision."""
        result = await high_precision_server.call("test_calculator", {
            "operation": "divide",
            "a": 1,
            "b": 3
        })
        
        assert result["precision"] == 6
        # 1/3 with 6 decimal places
        assert abs(result["result"] - 0.333333) < 1e-6
    
    async def test_example_calculator_invalid_operation(self, server):
        """Test invalid operation error."""
        with pytest.raises(ValueError, match="Invalid operation 'power'"):
            await server.call("test_calculator", {
                "operation": "power",
                "a": 2,
                "b": 3
            })
    
    async def test_example_calculator_missing_parameters(self, server):
        """Test missing parameters error."""
        with pytest.raises(ValueError, match="Missing required parameters"):
            await server.call("test_calculator", {
                "operation": "add",
                "a": 5
            })
    
    async def test_example_calculator_invalid_number_format(self, server):
        """Test invalid number format error."""
        with pytest.raises(TypeError, match="Invalid number format"):
            await server.call("test_calculator", {
                "operation": "add",
                "a": "not_a_number",
                "b": 5
            })
    
    async def test_example_calculator_string_numbers(self, server):
        """Test that string numbers are converted properly."""
        result = await server.call("test_calculator", {
            "operation": "add",
            "a": "10.5",
            "b": "2.3"
        })
        
        assert result["result"] == 12.8
        assert result["operands"] == [10.5, 2.3]
    
    async def test_example_calculator_negative_numbers(self, server):
        """Test operations with negative numbers."""
        result = await server.call("test_calculator", {
            "operation": "subtract",
            "a": -5,
            "b": 3
        })
        
        assert result["result"] == -8.0
        assert result["operands"] == [-5.0, 3.0]
    
    async def test_example_calculator_large_numbers(self, server):
        """Test operations with large numbers."""
        result = await server.call("test_calculator", {
            "operation": "multiply",
            "a": 1e6,
            "b": 1e6
        })
        
        assert result["result"] == 1e12
    
    async def test_example_calculator_zero_precision(self):
        """Test calculator with zero precision."""
        server = ExampleServer(name="test", config={"precision": 0})
        
        result = await server.call("test_calculator", {
            "operation": "divide",
            "a": 7,
            "b": 3
        })
        
        # Should round to nearest integer
        assert result["result"] == 2.0
        assert result["precision"] == 0