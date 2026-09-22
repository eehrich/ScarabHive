"""A stand-in for `claude -p --output-format stream-json`, replaying the event
shapes measured on Claude Code 2.1.257 (docs/coding_cli_plugin_konzept.md §2).

The task on stdin is a script, one command per line:
  WRITE <name> <text>   write a file in the working directory (a Write tool call)
  SLEEP <seconds>       wait
  RATE <utilization>    the five-hour window's utilization in the rate_limit_event
  REJECT                the rate_limit_event says the subscription refuses requests
  TORN                  a Write tool call whose line arrives in two halves
  ERROR                 end with an error result
  DIE                   exit without a result, "boom" on stderr
  GARBAGE               a line that is no JSON
With FAKE_CLAUDE_LOG set, argv and the environment's names go there as JSON.
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main():
    task = sys.stdin.buffer.read().decode("utf-8")
    argv = sys.argv[1:]
    if os.environ.get("FAKE_CLAUDE_LOG"):
        Path(os.environ["FAKE_CLAUDE_LOG"]).write_text(json.dumps(
            {"argv": argv, "env": sorted(os.environ), "task": task, "cwd": os.getcwd()}), encoding="utf-8")
    session = argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4())
    emit({"type": "system", "subtype": "init", "session_id": session, "cwd": os.getcwd(),
          "model": "claude-haiku-4-5-20251001", "permissionMode": "acceptEdits", "apiKeySource": "none",
          "tools": argv[argv.index("--tools") + 1].split(",") if "--tools" in argv else []})
    rate = next((float(line.split()[1]) for line in task.splitlines() if line.startswith("RATE ")), 0.34)
    emit({"type": "rate_limit_event", "session_id": session, "rate_limit_info": {
        "status": "rejected" if "REJECT" in task.splitlines() else "allowed",
        "resetsAt": time.time() + 3600, "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": rate, "resetsAt": time.time() + 3600},
                           "seven_day": {"utilization": 0.51, "resetsAt": time.time() + 86400}}}})
    turns, error = 1, False
    for line in task.splitlines():
        word, _, rest = line.partition(" ")
        if word == "WRITE":
            name, _, text = rest.partition(" ")
            path = Path(os.getcwd()) / name
            emit({"type": "assistant", "session_id": session, "message": {"content": [
                {"type": "tool_use", "id": f"t{turns}", "name": "Write", "input": {"file_path": str(path), "content": text}}]}})
            # Replaced, not opened: Windows refuses to open a hidden file (git hides .git) for writing.
            path.unlink(missing_ok=True)
            path.write_text(text, encoding="utf-8")
            emit({"type": "user", "session_id": session, "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"t{turns}", "content": f"File created successfully at: {path}"}]}})
            turns += 1
        elif word == "TORN":
            line = json.dumps({"type": "assistant", "session_id": session, "message": {"content": [
                {"type": "tool_use", "id": "torn", "name": "Write",
                 "input": {"file_path": str(Path(os.getcwd()) / "torn.txt"), "content": "x"}}]}}) + "\n"
            sys.stdout.write(line[:len(line) // 2])
            sys.stdout.flush()
            time.sleep(0.6)
            sys.stdout.write(line[len(line) // 2:])
            sys.stdout.flush()
        elif word == "SLEEP":
            time.sleep(float(rest))
        elif word == "ERROR":
            error = True
        elif word == "DIE":
            sys.stderr.write("boom\n")
            sys.exit(3)
        elif word == "GARBAGE":
            sys.stdout.write("this is no json\n")
            sys.stdout.flush()
    emit({"type": "assistant", "session_id": session, "message": {"content": [{"type": "text", "text": "done\nall of it"}]}})
    emit({"type": "result", "subtype": "error_during_execution" if error else "success", "is_error": error,
          "num_turns": turns, "duration_ms": 5002, "result": "failed" if error else f"did: {task.splitlines()[0]}",
          "session_id": session, "total_cost_usd": 0.07, "permission_denials": [
              {"tool_name": "Write", "tool_use_id": "x", "tool_input": {"file_path": "/elsewhere/outside.txt"}}],
          "usage": {"input_tokens": 18, "output_tokens": 291}})


if __name__ == "__main__":
    main()
