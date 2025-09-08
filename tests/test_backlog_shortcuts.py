"""Tests for command shortcuts functionality."""

import pytest
from unittest.mock import patch
from scripts.backlog import handle_command_shortcuts, main


class TestCommandShortcuts:
    """Test command shortcut functionality."""

    def test_shortcut_a_to_add_task(self):
        """Test 'a' shortcut converts to 'add-task'."""
        argv = ['a', '--title', 'Test task']
        result = handle_command_shortcuts(argv)
        assert result == ['add-task', '--title', 'Test task']

    def test_shortcut_e_to_edit(self):
        """Test 'e' shortcut converts to 'edit'."""
        argv = ['e', '123', '--title', 'Updated title']
        result = handle_command_shortcuts(argv)
        assert result == ['edit', '123', '--title', 'Updated title']

    def test_shortcut_l_to_list(self):
        """Test 'l' shortcut converts to 'list'."""
        argv = ['l', '--status', 'open']
        result = handle_command_shortcuts(argv)
        assert result == ['list', '--status', 'open']

    def test_shortcut_s_to_show(self):
        """Test 's' shortcut converts to 'show'."""
        argv = ['s', '123']
        result = handle_command_shortcuts(argv)
        assert result == ['show', '123']

    def test_no_shortcut_unchanged(self):
        """Test that non-shortcut commands remain unchanged."""
        argv = ['add-task', '--title', 'Test task']
        result = handle_command_shortcuts(argv)
        assert result == argv

    def test_empty_argv_unchanged(self):
        """Test that empty argv remains unchanged."""
        argv = []
        result = handle_command_shortcuts(argv)
        assert result == argv

    def test_shortcut_with_no_args(self):
        """Test shortcut with no additional arguments."""
        argv = ['a']
        result = handle_command_shortcuts(argv)
        assert result == ['add-task']

    @patch('scripts.backlog.load_config')
    @patch('scripts.backlog.build_parser')
    def test_main_with_shortcut(self, mock_build_parser, mock_load_config):
        """Test that main function properly handles shortcuts."""
        # Mock the parser and config
        mock_parser = mock_build_parser.return_value
        
        # Create a proper mock args object
        class MockArgs:
            def __init__(self):
                self.func = lambda args: 0
        
        mock_args = MockArgs()
        mock_parser.parse_args.return_value = mock_args
        mock_load_config.return_value = {}

        # Test with shortcut
        result = main(['a', '--title', 'Test'])

        # Verify shortcut was converted
        mock_parser.parse_args.assert_called_with(['add-task', '--title', 'Test'])
        assert result == 0

    @patch('scripts.backlog.load_config')
    @patch('scripts.backlog.build_parser')
    def test_main_without_shortcut(self, mock_build_parser, mock_load_config):
        """Test that main function works normally without shortcuts."""
        # Mock the parser and config
        mock_parser = mock_build_parser.return_value
        
        # Create a proper mock args object
        class MockArgs:
            def __init__(self):
                self.func = lambda args: 0
        
        mock_args = MockArgs()
        mock_parser.parse_args.return_value = mock_args
        mock_load_config.return_value = {}

        # Test without shortcut
        result = main(['add-task', '--title', 'Test'])

        # Verify original args were used
        mock_parser.parse_args.assert_called_with(['add-task', '--title', 'Test'])
        assert result == 0
