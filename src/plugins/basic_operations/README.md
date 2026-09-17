# Basic Operations Plugin

The Basic Operations plugin provides simple utility tools for testing and orchestration within the agent system. It exposes small, deterministic tools that are useful for health checks, simple time-based waits (with periodic status updates), and quick ping-style probes.

## Overview

This plugin is intentionally minimal and designed for two main purposes:

- Provide a lightweight, schema-driven toolset that demonstrates how to implement tools using the SchemaBasedToolServer pattern.
- Offer dependable utilities useful in integration tests and as primitives for agents (for example, waiting with periodic status updates or returning a timestamped ping response).

## Features

 - wait: Pause for a specified number of seconds while emitting periodic status updates via the status context.
- ping: A trivial tool that returns a timestamp and echoes provided details — useful for connectivity and latency checks.

## Configuration

Enable the plugin in `config/mcp.yaml` by adding it to `mcp.enabled_servers` and configuring the server entry. Example:

```yaml
mcp:
  enabled_servers:
    - basic_operations

servers:
  basic_operations:
    type: basic_operations
    # Optional configuration
    # max_wait_seconds: 3600
```

Configuration options
- `max_wait_seconds` (number, default: 3600): The maximum number of seconds the `wait` tool will accept. This value is exposed to the plugin schema as a numeric template variable and used to validate the `seconds` parameter.
- `default_update_interval` (float, optional): Default frequency (in seconds) for status updates emitted during a `wait` call. The plugin ignores any caller-supplied `update_interval` and always uses this server-side configuration.

## Usage Examples

### wait
Pause for a few seconds while receiving periodic status updates.

Request payload:

```json
{
  "seconds": 5,
  "message": "Waiting for external condition"
}
```

Behavior:
- Emits an initial progress message when the wait starts.
- Emits progress updates approximately every server-configured `default_update_interval` seconds with the remaining time and an optional message. Caller-provided `update_interval` values are ignored.
- On completion, emits a final progress message and returns a JSON object containing `requested_seconds`, `actual_seconds`, and `user_message`.

Example response:

```json
{
  "requested_seconds": 5,
  "actual_seconds": 5.01,
  "user_message": "Waiting for external condition"
}
```

Notes:
- The `wait` tool validates that `seconds` is >= 0.1 and <= `max_wait_seconds` (default 3600).
- The plugin calls `await status.progress(...)` unconditionally to emit progress updates — callers should provide a status context that implements `progress` (the tool runtime does this).

### ping
A fast, idempotent probe returning the current timestamp and echoing provided details.

Request payload examples:

```json
# Simple ping
{ "message": "hello" }

# Ping with additional data
{ "message": "check", "extra": { "session": "abc123" } }
```

Example response:

```json
{
  "timestamp": "2025-09-28T12:34:56.789Z",
  "message": "hello",
  "details": {}
}
```

## API Reference

Tools provided by this plugin (as exposed in the plugin schema):

### wait
Pauses execution for a specified duration while emitting periodic status updates.

Parameters:
- `seconds` (number, required): Seconds to wait. Minimum: 0.1. Maximum: `max_wait_seconds` (configurable).
- `message` (string, optional): Human-readable message included in status updates.

Returns: JSON object with fields:
- `requested_seconds` (float)
- `actual_seconds` (float)
- `user_message` (string|null)

Behavioral notes:
- Emits `status.progress(...)` at start, periodically during the wait, and on completion.
- Assumes the tool runtime provides a valid `status` context. The plugin intentionally does not guard these calls with safety checks.

### ping
Quick probe that returns a timestamp and echoes supplied data.

Parameters:
- `message` (string, optional)
- `details` (object, optional): Arbitrary JSON to echo back.

Returns:
- `timestamp` (ISO 8601 string)
- `message` (string|null)
- `details` (object)

## Response Formats

Responses are plain JSON objects. The `wait` tool returns a small summary object (see above). The `ping` tool returns a timestamped envelope.

## Testing

Unit and integration tests for the plugin are located in `tests/test_plugin_basic_operations.py`. The tests mock the status context and inspect that `status.progress` is called with expected messages and payloads. To run only the plugin tests:

```bash
.venv/Scripts/python.exe -m pytest tests/test_plugin_basic_operations.py -q
```

Note: In CI and local test runs, external LLM calls are mocked across the project; this plugin's tests do not perform network calls.

## Troubleshooting

- If `wait` fails with a validation error, verify that `seconds` is within the allowed range and that `max_wait_seconds` (if configured) is a number in `config/mcp.yaml`.
- If status updates are not visible, ensure the caller/runtime provides a status context implementing an async `progress` method.

## Contributing

This plugin follows the repository conventions for plugins. When contributing:

- Add unit tests under `tests/` covering new behavior.
- Update `schema.yaml` when adding or changing tools.
- Maintain numeric template variables in the schema unquoted so they render with correct types.
- Run the test suite and formatting/type checks (`ruff`, `mypy`) before opening a PR.

---

If you want, I can also add a short example `curl`/HTTP invocation using the tool server API for local testing — tell me and I'll append it.