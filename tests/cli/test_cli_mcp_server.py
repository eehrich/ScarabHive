"""Tests for tool server CLI interfaces."""

import subprocess
import sys
from pathlib import Path

# Add the src directory to the Python path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

class TestToolServerCLI:
    """Test CLI interfaces for all tool servers."""
    
    def test_weather_cli_help(self):
        """Test weather server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "plugins.weather",
            "--help"
    ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "Weather Tool Server" in result.stdout
        assert "--location" in result.stdout
        assert "--source" in result.stdout
        assert "--days" in result.stdout
    
    def test_duckduckgo_cli_help(self):
        """Test DuckDuckGo search server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "plugins.duckduckgo_search",
            "--help"
    ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "DuckDuckGo Search Tool Server" in result.stdout
        assert "--query" in result.stdout
        assert "--max-results" in result.stdout
    
    def test_yahoo_finance_cli_help(self):
        """Test Yahoo Finance server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "plugins_trading.yahoo_finance",
            "--help"
    ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "Yahoo Finance Tool Server" in result.stdout
        assert "--symbol" in result.stdout
        assert "--period" in result.stdout
        assert "--interval" in result.stdout
    
    def test_twitter_search_cli_help(self):
        """Test Twitter search server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "plugins.twitter_search",
            "--help"
    ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "Twitter Search Tool Server" in result.stdout
        assert "--query" in result.stdout
        assert "--max-results" in result.stdout
        assert "--lang" in result.stdout
    
    def test_llm_router_cli_help(self):
        """Test LLM Router server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "plugins.llm_router",
            "--help"
    ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "LLM Router Tool Server" in result.stdout
        assert "--prompt" in result.stdout
        assert "--provider" in result.stdout
        assert "--model" in result.stdout
    
    # The Google Search plugin was removed from the project. Tests that
    # referenced it have been deleted or updated to use existing search
    # plugins such as DuckDuckGo. Keep other server CLI help tests intact.
    
    def test_datetime_cli_help(self):
        """Test DateTime server CLI help."""
        result = subprocess.run([
            sys.executable, "-m", "plugins.datetime",
            "--help"
    ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "DateTime Tool Server" in result.stdout
        assert "--timezone" in result.stdout
        assert "--format" in result.stdout

    def test_cli_with_server_mode(self):
        """Test that --server flag is recognized."""
        # Test with weather server
        result = subprocess.run([
            sys.executable, "-m", "plugins.weather",
            "--server", "--help"
        ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode == 0
        assert "--server" in result.stdout
        assert "--port" in result.stdout

    def test_cli_invalid_arguments(self):
        """Test CLI with invalid arguments."""
        result = subprocess.run([
            sys.executable, "-m", "plugins.weather",
            "--invalid-argument"
        ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, cwd=Path(__file__).parent.parent)
        
        assert result.returncode != 0
        assert "unrecognized arguments" in result.stderr or "error" in result.stderr.lower()

    def test_all_servers_importable(self):
        """Test that all server modules can be imported and have main functions."""
        servers = [
            "weather",
            "duckduckgo_search",
            "web_scraper",
            "yahoo_finance",
            "twitter_search",
            "llm_router",
            "datetime"
        ]
        # Note: google_search plugin not implemented yet
        
        for server in servers:
            # All servers have been migrated to plugins (except yahoo_finance in plugins_trading)
            module_name = f"plugins_trading.{server}" if server == "yahoo_finance" else f"plugins.{server}"
            py = (
                "import importlib\n"
                f"try:\n"
                f"    import {module_name}.__main__\n"
                f"    print('OK')\n"
                f"except Exception as e:\n"
                f"    print(f'FAILED: {{e}}')\n"
            )

            result = subprocess.run([
                sys.executable, "-c", py
            ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30, cwd=Path(__file__).parent.parent)

            assert result.returncode == 0, f"Failed to import {server} server"
            assert "OK" in result.stdout, f"Import test failed for {server}"
