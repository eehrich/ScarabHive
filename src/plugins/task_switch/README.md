# Task Switch Plugin

State machine control for LLM agents - enables dynamic prompt switching with optional workflow gates.

## Overview

The `task_switch` plugin allows agents to switch between different task states at runtime. The current task variable becomes available in Jinja2 prompts, enabling conditional prompt sections. Optionally, task transitions can be gated by preconditions that must be satisfied before switching.

## Features

- **Dynamic task switching** - Change agent behavior at runtime
- **Jinja2 integration** - Task variable available in prompts as `{{ current_task }}`
- **Task validation** - Optional restriction to allowed task names
- **Precondition gates** - Block task transitions until conditions are met
- **Template params** - Use `{{ var }}` in precondition parameters

## Usage

### Basic Configuration

```yaml
my_agent:
  agent_config:
    template_vars:
      current_task: "init"
    tools:
      allowed:
        - "task_switch/*"
    system_prompt: |
      Current task: {{ current_task }}
      
      {% if current_task == 'analyze' %}
      Analyze the problem thoroughly.
      {% elif current_task == 'execute' %}
      Execute the solution.
      {% endif %}
```

### Tool

```
task_switch_set_task(task_name: str) -> {"status", "previous_task", "current_task"}
```

**Return values:**
- `status: "success"` - Task switched successfully
- `status: "error"` - Invalid task name (not in allowed list)
- `status: "blocked"` - Precondition not met (gate closed)

### Workflow Example

```
User: "Fix the bug"
Agent (task=init) → Calls set_task("analyze")
Agent (task=analyze) → Calls set_task("execute")  
Agent (task=execute) → Completes task
```

## Configuration

### Basic Config

```yaml
plugins:
  servers:
    task_switch:
      type: task_switch
      enabled: true
      config:
        task_var_name: "workflow_state"  # Default: "current_task"
        allowed_tasks:                    # Optional: restrict valid states
          - init
          - analyze
          - execute
          - review
```

### With Preconditions (Gate Checks)

```yaml
plugins:
  servers:
    writer_workflow:
      type: task_switch
      enabled: true
      config:
        task_var_name: "workflow_phase"
        allowed_tasks:
          - planning
          - structure
          - content
          - review
        
        task_preconditions:
          structure:
            # Call this tool before allowing switch to "structure"
            tool: "writer_content_production_status"
            params:
              book_id: "{{ book_id }}"  # Rendered from agent's template_vars
              scope: "story"
            gate_field: "summary.can_proceed"  # Dot-notation for nested access
            error_field: "summary.gate_reason"  # Optional: field with error message
          
          content:
            tool: "writer_content_production_status"
            params:
              book_id: "{{ book_id }}"
              scope: "structure"
            gate_field: "summary.can_proceed"
```

## Precondition Configuration

| Field | Required | Description |
|-------|----------|-------------|
| `tool` | Yes | Tool name to call for validation |
| `params` | No | Parameters passed to tool (supports `{{ var }}` templates) |
| `gate_field` | No | Field in result that must be `true` (default: `can_proceed`) |
| `error_field` | No | Field with error message when blocked (default: `gate_reason`) |

### Template Variables

Precondition params support `{{ var }}` syntax to inject values from the agent's `template_vars`:

```yaml
params:
  book_id: "{{ book_id }}"    # Replaced with agent's template_vars["book_id"]
  scope: "{{ current_scope }}"
```

### Nested Field Access

Use dot-notation to access nested fields in the validation tool's result:

```yaml
gate_field: "summary.can_proceed"  # Accesses result["summary"]["can_proceed"]
error_field: "data.status.reason"  # Accesses result["data"]["status"]["reason"]
```

## Response Formats

### Success

```json
{
  "status": "success",
  "previous_task": "planning",
  "current_task": "structure"
}
```

### Error (Invalid Task)

```json
{
  "status": "error",
  "error": "Invalid task 'invalid'. Allowed tasks: ['init', 'analyze', 'execute', 'review']"
}
```

### Blocked (Gate Closed)

```json
{
  "status": "blocked",
  "task_name": "content",
  "gate_closed": true,
  "reason": "Structure has 2/4 approvals - needs 2 more",
  "gate_field": "summary.can_proceed",
  "gate_value": false,
  "precondition_result": {
    "status": "success",
    "summary": {
      "can_proceed": false,
      "gate_reason": "Structure has 2/4 approvals - needs 2 more",
      "valid_approvals": 2,
      "required_approvals": 4
    }
  }
}
```

The `precondition_result` contains the full response from the validation tool, allowing the LLM to understand why the gate is closed and what needs to be done.

## Use Cases

### 1. Simple Task State Machine

Agent switches between analysis, planning, and execution phases.

### 2. Workflow Gates (Writer System)

Prevent agents from skipping workflow steps:

```
planning → [story approved?] → structure → [structure approved?] → content → review
```

If story isn't approved, switching to "structure" returns `status: "blocked"` with the reason, forcing the agent to complete story approval first.

### 3. Multi-Phase Projects

Control complex multi-step processes where each phase has prerequisites.

## Integration with Agents

### Agent YAML

```yaml
book_architect:
  agent_config:
    template_vars:
      workflow_phase: "planning"
      book_id: ""
    tools:
      allowed:
        - "+writer_workflow/*"
```

### Prompt Usage

```jinja2
{% if workflow_phase == 'planning' %}
## Planning Phase
Focus on story design and outline.
{% elif workflow_phase == 'structure' %}
## Structure Phase
Build chapters, scenes, and paths.
{% elif workflow_phase == 'content' %}
## Content Phase
Write scene content.
{% endif %}
```
