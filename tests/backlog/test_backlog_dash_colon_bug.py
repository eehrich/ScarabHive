"""Test for backlog parser bug with dash-colon patterns in description/notes.

The bug: When description or notes contain lines like:
  - API key: value
  - pre_llm_call: description

These lines are incorrectly parsed as field keys and removed on subsequent writes.
"""
from scripts.backlog_tool.parser import parse, build_markdown


def test_description_with_dash_colon_preserved():
    """Test that description lines with '- key: value' format are preserved."""
    backlog_md = """# Backlog

## 1. Epics - open

- ☐ Epic 0001: Test Epic
  - status: open
  - added: 2025-10-12
  - description:
    Configure API endpoints:
    - GET /api/users: List all users
    - POST /api/users: Create new user
    - DELETE /api/users/{id}: Delete user
  - tasks:
    - ☐ Task 0001: Implement endpoints
      - status: open
      - added: 2025-10-12
      - description:
        Implementation steps:
        - auth_system: Add authentication middleware
        - user_controller: Create CRUD operations
        - api_routes: Register routes

## 2. Epics - finished

## 3. Ideas
"""
    
    lines = backlog_md.strip().split('\n')
    backlog = parse(lines)
    
    # Verify epic description was parsed correctly
    assert len(backlog.epics_open) == 1
    epic = backlog.epics_open[0]
    assert epic.id == "0001"
    assert len(epic.description) > 0
    
    # Check that all lines are preserved
    desc_text = '\n'.join(epic.description)
    assert "- GET /api/users: List all users" in desc_text
    assert "- POST /api/users: Create new user" in desc_text
    assert "- DELETE /api/users/{id}: Delete user" in desc_text
    
    # Verify task description
    assert len(epic.tasks) == 1
    task = epic.tasks[0]
    assert len(task.description) > 0
    
    task_desc = '\n'.join(task.description)
    assert "- auth_system: Add authentication middleware" in task_desc
    assert "- user_controller: Create CRUD operations" in task_desc
    assert "- api_routes: Register routes" in task_desc
    
    # Now rebuild and re-parse to ensure round-trip preservation
    rebuilt = build_markdown(backlog)
    backlog2 = parse(rebuilt.split('\n'))
    
    epic2 = backlog2.epics_open[0]
    desc_text2 = '\n'.join(epic2.description)
    assert "- GET /api/users: List all users" in desc_text2
    assert "- POST /api/users: Create new user" in desc_text2
    
    task2 = epic2.tasks[0]
    task_desc2 = '\n'.join(task2.description)
    assert "- auth_system: Add authentication middleware" in task_desc2


def test_notes_with_dash_colon_preserved():
    """Test that notes lines with '- key: value' format are preserved."""
    backlog_md = """# Backlog

## 1. Epics - open

- ☐ Epic 0002: Hook System
  - status: open
  - added: 2025-10-12
  - notes:
    Hook types available:
    - pre_llm_call: Before LLM is called
    - post_llm_call: After LLM response
    - format_output: Before displaying to user
    Plugin configuration example:
    - context_optimizer: Reduces context size
    - message_validator: Validates messages
  - tasks:
    - ☐ Task 0002: Implement hooks
      - status: open
      - added: 2025-10-12
      - notes:
        Dependencies:
        - hook_registry: Core registry system
        - plugin_loader: Load hook plugins
        - config_parser: Parse hook configs

## 2. Epics - finished

## 3. Ideas
"""
    
    lines = backlog_md.strip().split('\n')
    backlog = parse(lines)
    
    # Verify epic notes
    # Note: Parser strips leading '- ' from notes lines, builder re-adds them
    epic = backlog.epics_open[0]
    notes_text = '\n'.join(epic.notes)
    assert "pre_llm_call: Before LLM is called" in notes_text
    assert "post_llm_call: After LLM response" in notes_text
    assert "format_output: Before displaying to user" in notes_text
    assert "context_optimizer: Reduces context size" in notes_text
    
    # Verify task notes
    task = epic.tasks[0]
    task_notes = '\n'.join(task.notes)
    assert "hook_registry: Core registry system" in task_notes
    assert "plugin_loader: Load hook plugins" in task_notes
    assert "config_parser: Parse hook configs" in task_notes
    
    # Round-trip test - builder re-adds '- ' prefix
    rebuilt = build_markdown(backlog)
    backlog2 = parse(rebuilt.split('\n'))
    
    epic2 = backlog2.epics_open[0]
    notes_text2 = '\n'.join(epic2.notes)
    # After round-trip, content is preserved (dash is normalized by builder)
    assert "pre_llm_call: Before LLM is called" in notes_text2
    
    task2 = epic2.tasks[0]
    task_notes2 = '\n'.join(task2.notes)
    assert "hook_registry: Core registry system" in task_notes2


def test_mixed_dash_colon_in_description():
    """Test mixed content with both regular text and dash-colon patterns."""
    backlog_md = """# Backlog

## 1. Epics - open

- ☐ Epic 0003: Complex Description
  - status: open
  - added: 2025-10-12
  - description:
    This epic has multiple types of content.
    
    API Endpoints:
    - GET /sessions: List user sessions
    - POST /sessions/create: Create new session
    - PATCH /sessions/{id}: Update session metadata
    
    Configuration files:
    - llm_config: LLM settings
    - agent_config: Agent configuration
    - plugin_config: Plugin settings
    
    Regular paragraph without dashes or colons.
    Another line with - dash but no colon.
    Line with colon: but no dash prefix.

## 2. Epics - finished

## 3. Ideas
"""
    
    lines = backlog_md.strip().split('\n')
    backlog = parse(lines)
    
    epic = backlog.epics_open[0]
    desc_text = '\n'.join(epic.description)
    
    # Check all patterns are preserved
    assert "- GET /sessions: List user sessions" in desc_text
    assert "- POST /sessions/create: Create new session" in desc_text
    assert "- PATCH /sessions/{id}: Update session metadata" in desc_text
    assert "- llm_config: LLM settings" in desc_text
    assert "- agent_config: Agent configuration" in desc_text
    assert "- plugin_config: Plugin settings" in desc_text
    assert "Regular paragraph without dashes or colons." in desc_text
    assert "Another line with - dash but no colon." in desc_text
    assert "Line with colon: but no dash prefix." in desc_text
    
    # Round-trip
    rebuilt = build_markdown(backlog)
    backlog2 = parse(rebuilt.split('\n'))
    
    epic2 = backlog2.epics_open[0]
    desc_text2 = '\n'.join(epic2.description)
    assert "- GET /sessions: List user sessions" in desc_text2
    assert "- llm_config: LLM settings" in desc_text2


def test_known_field_names_not_confused():
    """Test that actual field names (status, added, etc.) are still parsed correctly."""
    backlog_md = """# Backlog

## 1. Epics - open

- ☐ Epic 0004: Field Name Test
  - status: open
  - added: 2025-10-12
  - notes:
    This notes section mentions field names:
    - status: Can be open, done, failed
    - added: Date when item was added
    - closed: Date when item was closed
    These should be preserved as content, not parsed as fields.

## 2. Epics - finished

## 3. Ideas
"""
    
    lines = backlog_md.strip().split('\n')
    backlog = parse(lines)
    
    epic = backlog.epics_open[0]
    
    # Verify field values are correct
    assert epic.status == "open"
    assert epic.added == "2025-10-12"
    
    # Verify notes contain the dash-colon patterns (parser strips leading '- ')
    notes_text = '\n'.join(epic.notes)
    assert "status: Can be open, done, failed" in notes_text
    assert "added: Date when item was added" in notes_text
    assert "closed: Date when item was closed" in notes_text
    
    # Round-trip
    rebuilt = build_markdown(backlog)
    backlog2 = parse(rebuilt.split('\n'))
    
    epic2 = backlog2.epics_open[0]
    assert epic2.status == "open"  # Field value unchanged
    notes_text2 = '\n'.join(epic2.notes)
    assert "status: Can be open, done, failed" in notes_text2  # Content preserved after round-trip

