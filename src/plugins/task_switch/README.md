# Task Switch Plugin

State machine control for LLM agents - enables dynamic prompt switching.

## Overview

The `task_switch` plugin allows agents to switch between different task states at runtime. The `current_task` variable becomes available in Jinja2 prompts, enabling conditional prompt sections.

## Usage

### 1. Configure Agent

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

### 2. Tool

```
task_switch_set_task(task_name: str) -> {"status", "previous_task", "current_task"}
```

### 3. Workflow Example

```
User: "Fix the bug"
Agent (task=init) → Calls set_task("analyze")
Agent (task=analyze) → Calls set_task("execute")  
Agent (task=execute) → Completes task
```

## Configuration

Configure in `plugins.yaml`:

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

### allowed_tasks

When `allowed_tasks` is configured:
- The tool schema shows an enum with valid values (LLM sees options)
- Invalid task names return an error to the LLM
- Without this config, any task name is allowed

Example error for invalid task:
```json
{"status": "error", "error": "Invalid task 'invalid'. Allowed tasks: ['init', 'analyze', 'execute', 'review']"}
```
