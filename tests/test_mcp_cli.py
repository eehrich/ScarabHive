"""Tests for MCP server CLI interfaces."""

import pytest
import subprocess
import sys
import os
from pathlib import Path

# Add the src directory to the Python path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

class TestMCPServerCLI:
    """Test CLI interfaces for all MCP servers."""
    
    def test_weather_cli_help(self):
        """Test weather server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "plugins.weather",
            "--help"
        ], capture_output=True, text=True, timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "Weather MCP Server" in result.stdout
        assert "--location" in result.stdout
        assert "--source" in result.stdout
        assert "--days" in result.stdout
    
    def test_duckduckgo_cli_help(self):
        """Test DuckDuckGo search server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "plugins.duckduckgo_search",
            "--help"
        ], capture_output=True, text=True, timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "DuckDuckGo Search MCP Server" in result.stdout
        assert "--query" in result.stdout
        assert "--max-results" in result.stdout
    
    def test_yahoo_finance_cli_help(self):
        """Test Yahoo Finance server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "agent_system.servers.yahoo_finance",
            "--help"
        ], capture_output=True, text=True, timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "Yahoo Finance MCP Server" in result.stdout
        assert "--symbol" in result.stdout
        assert "--period" in result.stdout
        assert "--interval" in result.stdout
    
    def test_twitter_search_cli_help(self):
        """Test Twitter search server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "agent_system.servers.twitter_search",
            "--help"
        ], capture_output=True, text=True, timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "Twitter Search MCP Server" in result.stdout
        assert "--query" in result.stdout
        assert "--max-results" in result.stdout
        assert "--lang" in result.stdout
    
    def test_llm_router_cli_help(self):
        """Test LLM Router server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "agent_system.servers.llm_router",
            "--help"
        ], capture_output=True, text=True, timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "LLM Router MCP Server" in result.stdout
        assert "--prompt" in result.stdout
        assert "--provider" in result.stdout
        assert "--model" in result.stdout
    
    def test_google_search_cli_help(self):
        """Test Google Search server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "agent_system.servers.google_search",
            "--help"
        ], capture_output=True, text=True, timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "Google Search MCP Server" in result.stdout
        assert "--query" in result.stdout
        assert "--max-results" in result.stdout
        assert "--api-key" in result.stdout
        assert "--cx" in result.stdout
    
    def test_datetime_cli_help(self):
        """Test DateTime server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "plugins.datetime",
            "--help"
        ], capture_output=True, text=True, timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "DateTime MCP Server" in result.stdout
        assert "--timezone" in result.stdout
        assert "--format" in result.stdout

    def test_cli_with_server_mode(self):
        """Test that --server flag is recognized."""
        # Test with weather server
        result = subprocess.run([
            sys.executable, "-m", "plugins.weather",
            "--server", "--help"
        ], capture_output=True, text=True, timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "--server" in result.stdout
        assert "--port" in result.stdout

    def test_cli_invalid_arguments(self):
        """Test CLI with invalid arguments."""
        result = subprocess.run([
            sys.executable, "-m", "plugins.weather",
            "--invalid-argument"
        ], capture_output=True, text=True, timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode != 0
        assert "unrecognized arguments" in result.stderr or "error" in result.stderr.lower()

    def test_all_servers_importable(self):
        """Test that all server modules can be imported and have main functions."""
        servers = [
            "weather",
            "duckduckgo_search", 
            "yahoo_finance",
            "twitter_search",
            "llm_router",
            "google_search",
            "datetime"
        ]
        
        for server in servers:
            # Use plugins package for datetime, duckduckgo_search, weather; otherwise
            # import the legacy server shim under agent_system.servers.
            if server in ("datetime", "duckduckgo_search", "weather"):
                module = f"plugins.{server}.__main__"
            else:
                module = f"agent_system.servers.{server}.__main__"

            result = subprocess.run([
                sys.executable, "-c", f"import {module}; print('OK')"
            ], capture_output=True, text=True, timeout=30, cwd=Path(__file__).parent.parent)

            assert result.returncode == 0, f"Failed to import {server} server"
            assert "OK" in result.stdout, f"Import test failed for {server}"
