"""A stand-in for `agy --output-format stream-json --input-format stream-json --print=`, replaying the event
shapes measured on the Antigravity CLI 1.4.8 (2026-10-27).

The task is the content of the one stdin message; it is a script, one command per line:
  WRITE <name> <text>   write a file in the working directory (an edit_file tool step)
  DENY                  a shell command refused headless (denied_actions)
  ERROR                 end with an error result
  OUTPUT <json>         with --json-schema, the result's structured_output (else {"done": true})
With FAKE_CLAUDE_LOG set, argv, the environment's names and the task go there as JSON.
"""
import json
import os
import sys
import uuid
from pathlib import Path


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def step(conversation, index, state, **fields):
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": index,
                                                  "state": state, **fields}})


def main():
    message = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    task = message["message"]["content"]
    argv = sys.argv[1:]
    if os.environ.get("FAKE_CLAUDE_LOG"):
        Path(os.environ["FAKE_CLAUDE_LOG"]).write_text(json.dumps(
            {"argv": argv, "env": sorted(os.environ), "task": task, "cwd": os.getcwd(),
             "home": os.environ.get("HOME"), "userprofile": os.environ.get("USERPROFILE")}), encoding="utf-8")
    conversation = argv[argv.index("--conversation") + 1] if "--conversation" in argv else str(uuid.uuid4())
    emit({"event": "init", "conversation_id": conversation, "init": {
        "model": argv[argv.index("--model") + 1] if "--model" in argv else "gemini-3.8-flash-medium",
        "cwd": os.getcwd(), "tools": ["edit_file", "read_file", "run_command", "search_web"],
        "permission_mode": "request-review"}})
    step(conversation, 0, "DONE", step_type="user_input")
    index, denied, error = 1, [], False
    for line in task.splitlines():
        word, _, rest = line.partition(" ")
        if word == "WRITE":
            name, _, text = rest.partition(" ")
            path = Path(os.getcwd()) / name
            info = {"name": "edit_file", "parameters": {"Path": str(path)}}
            step(conversation, index, "ACTIVE", step_type="tool", tool_name="edit_file", tool_info=info)
            path.unlink(missing_ok=True)
            path.write_text(text, encoding="utf-8")
            step(conversation, index, "DONE", step_type="tool", tool_name="edit_file", duration_seconds=0.03,
                 tool_info={**info, "output": "1 lines, 5 bytes"})
            index += 1
        elif word == "DENY":
            info = {"name": "run_command", "parameters": {"CommandLine": "curl.exe -s https://example.com"}}
            step(conversation, index, "ACTIVE", step_type="tool", tool_name="run_command", tool_info=info)
            step(conversation, index, "ERROR", step_type="tool", tool_name="run_command", tool_info={**info, "error": {
                "type": "TOOL_ERROR", "message": "permission check failed for unsandboxed \"curl.exe\": user denied"}})
            denied.append({"action": "command", "display_name": "RunCommand"})
            index += 1
        elif word == "ERROR":
            error = True
    step(conversation, index, "ACTIVE", step_type="agent_response", text_delta="did: ")
    step(conversation, index, "DONE", step_type="agent_response", text_delta=task.splitlines()[0],
         duration_seconds=1.2, usage={"input_tokens": 6865, "output_tokens": 63})
    result = {"conversation_id": conversation, "status": "ERROR" if error else "SUCCESS",
              "response": "" if error else f"did: {task.splitlines()[0]}", "duration_seconds": 7.47,
              "num_turns": 1, "usage": {"input_tokens": 6865, "output_tokens": 63, "total_tokens": 6928}}
    if error:
        result["error"] = "the model request failed with an unspecified error"
    if denied:
        result["denied_actions"] = denied
    if "--json-schema" in argv:
        result["structured_output"] = next((json.loads(line[7:]) for line in task.splitlines()
                                            if line.startswith("OUTPUT ")), {"done": True})
    emit({"event": "result", "result": result})
    sys.exit(1 if error else 0)


if __name__ == "__main__":
    main()
