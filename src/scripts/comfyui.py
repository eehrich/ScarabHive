#!/usr/bin/env python
"""ComfyUI CLI - Command-line interface for ComfyUI plugin.

Usage:
    comfyui status          - Check if ComfyUI server is running
    comfyui list            - List available workflows
    comfyui execute <id>    - Execute a workflow
    comfyui jobs            - Show active and recent jobs
    comfyui result <id>     - Get results for a completed job
    comfyui cancel <id>     - Cancel a running job
    comfyui monitor         - Open web monitor in browser
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def get_config() -> dict:
    """Load ComfyUI plugin configuration."""
    import yaml
    
    # Try multiple possible config locations
    possible_paths = [
        Path.cwd() / "config" / "plugins.yaml",
        Path(__file__).parent.parent.parent.parent / "config" / "plugins.yaml",
        Path(__file__).parent.parent.parent / "config" / "plugins.yaml",
    ]
    
    config_path = None
    for path in possible_paths:
        if path.exists():
            config_path = path
            break
    
    if not config_path:
        print("Error: Config file not found. Searched:", file=sys.stderr)
        for p in possible_paths:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)
    
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    
    comfyui_config = config.get("plugins", {}).get("servers", {}).get("comfyui", {})
    if not comfyui_config:
        print("Error: ComfyUI plugin not configured in plugins.yaml", file=sys.stderr)
        sys.exit(1)
    
    return comfyui_config


def get_client():
    """Create ComfyUI client from config."""
    from plugins.comfyui.comfyui_client import ComfyUIClient
    
    config = get_config()
    return ComfyUIClient(
        host=config.get("host", "127.0.0.1"),
        port=config.get("port", 8188),
        output_dir=Path(config.get("output_dir", "data/comfyui/outputs")),
        timeout=float(config.get("timeout_seconds", 300))
    )


def get_job_tracker():
    """Create job tracker from config."""
    from plugins.comfyui.job_tracker import ComfyUIJobTracker
    
    config = get_config()
    output_dir = Path(config.get("output_dir", "data/comfyui/outputs"))
    db_path = output_dir.parent / "jobs.db"
    return ComfyUIJobTracker(db_path)


async def cmd_status(args: argparse.Namespace) -> int:
    """Check ComfyUI server status."""
    client = get_client()
    result = await client.ping()
    
    if result.get("status") == "online":
        print("✅ ComfyUI server is ONLINE")
        print(f"   Host: {result.get('host')}")
        print(f"   Queue pending: {result.get('queue_pending', 0)}")
        print(f"   Queue running: {result.get('queue_running', 0)}")
        return 0
    else:
        print("❌ ComfyUI server is OFFLINE")
        print(f"   Error: {result.get('error', 'Unknown')}")
        return 1


async def cmd_list(args: argparse.Namespace) -> int:
    """List available workflows."""
    
    config = get_config()
    workflows = config.get("workflows", [])
    
    if not workflows:
        print("No workflows configured.")
        print("\nAdd workflows to config/plugins.yaml under comfyui.workflows")
        return 1
    
    print(f"Available Workflows ({len(workflows)}):\n")
    
    for wf in workflows:
        print(f"  📋 {wf['id']}")
        print(f"     Name: {wf.get('name', 'N/A')}")
        print(f"     Category: {wf.get('category', 'general')}")
        if wf.get('description'):
            print(f"     Description: {wf['description']}")
        
        params = wf.get('parameters', [])
        if params:
            print("     Parameters:")
            for p in params:
                req = " (required)" if p.get('required') else ""
                default = f" [default: {p.get('default')}]" if 'default' in p else ""
                print(f"       - {p['name']}: {p.get('type', 'string')}{req}{default}")
                if p.get('description'):
                    print(f"         {p['description']}")
        print()
    
    return 0


async def cmd_execute(args: argparse.Namespace) -> int:
    """Execute a workflow."""
    
    config = get_config()
    workflows = {w['id']: w for w in config.get('workflows', [])}
    
    if args.workflow_id not in workflows:
        print(f"Error: Unknown workflow: {args.workflow_id}")
        print(f"Available: {', '.join(workflows.keys())}")
        return 1
    
    wf = workflows[args.workflow_id]
    workflow_file = Path(config.get('workflow_files_dir', 'config/comfyui_workflows')) / wf.get('workflow_file', f"{args.workflow_id}.json")
    
    if not workflow_file.exists():
        print(f"Error: Workflow file not found: {workflow_file}")
        return 1
    
    # Load workflow
    with open(workflow_file, "r", encoding="utf-8") as f:
        workflow_json = json.load(f)
    
    # Parse parameters from args
    params = {}
    if args.params:
        for param in args.params:
            if '=' in param:
                key, value = param.split('=', 1)
                # Try to parse as JSON for complex types
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
                params[key] = value
    
    # Inject parameters
    for param_def in wf.get('parameters', []):
        name = param_def['name']
        node_id = param_def.get('node_id')
        field_path = param_def.get('field', '')
        
        if name in params:
            value = params[name]
        elif param_def.get('required'):
            print(f"Error: Required parameter missing: {name}")
            return 1
        else:
            value = param_def.get('default')
        
        if node_id and field_path and value is not None:
            _inject_value(workflow_json, node_id, field_path, value)
    
    print(f"Executing workflow: {wf.get('name', args.workflow_id)}")
    if params:
        print(f"Parameters: {json.dumps(params, indent=2)}")
    
    # Queue workflow
    client = get_client()
    result = await client.queue_prompt(workflow_json)
    
    if result.get("status") == "error":
        print(f"❌ Error: {result.get('error')}")
        return 1
    
    prompt_id = result.get("prompt_id")
    print(f"✅ Job queued: {prompt_id}")
    
    # Register in tracker
    tracker = get_job_tracker()
    tracker.register_job(
        prompt_id=prompt_id,
        workflow_id=args.workflow_id,
        workflow_name=wf.get('name', args.workflow_id),
        parameters=params,
        output_prefix=args.prefix or "comfy"
    )
    
    if args.wait:
        print("Waiting for completion...")
        result = await client.wait_for_completion(prompt_id, poll_interval=2.0)
        if result.get("status") == "completed":
            print("✅ Job completed!")
            tracker.update_status(prompt_id, "completed")
            return await cmd_result_by_id(prompt_id, args.prefix or "comfy")
        else:
            print(f"❌ Job failed: {result.get('error')}")
            tracker.update_status(prompt_id, "failed", str(result.get('error')))
            return 1
    else:
        print(f"\nCheck status: comfyui result {prompt_id}")
    
    return 0


def _inject_value(workflow: dict, node_id: str, field_path: str, value) -> None:
    """Inject a value into the workflow."""
    if node_id not in workflow:
        return
    
    parts = field_path.split('.')
    obj = workflow[node_id]
    
    for part in parts[:-1]:
        if part not in obj:
            obj[part] = {}
        obj = obj[part]
    
    obj[parts[-1]] = value


async def cmd_jobs(args: argparse.Namespace) -> int:
    """Show active and recent jobs."""
    tracker = get_job_tracker()
    client = get_client()
    
    # Server status
    status = await client.ping()
    if status.get("status") == "online":
        print(f"🟢 Server: {status.get('host')} (queue: {status.get('queue_pending', 0)} pending, {status.get('queue_running', 0)} running)")
    else:
        print(f"🔴 Server: OFFLINE ({status.get('error', 'Unknown')})")
    
    print()
    
    # Active jobs
    active = tracker.get_active_jobs()
    if active:
        print(f"Active Jobs ({len(active)}):")
        for job in active:
            status_icon = "⏳" if job['status'] == 'queued' else "🔄"
            print(f"  {status_icon} {job['prompt_id'][:12]}... | {job['workflow_name']} | {job['status']}")
    else:
        print("No active jobs.")
    
    print()
    
    # Recent completed
    recent = tracker.get_recent_completed(limit=args.limit)
    if recent:
        print(f"Recent Completed ({len(recent)}):")
        for job in recent:
            status_icon = "✅" if job['status'] == 'completed' else "❌"
            outputs = job.get('outputs', {})
            output_count = sum(len(v) for v in outputs.values()) if outputs else 0
            print(f"  {status_icon} {job['prompt_id'][:12]}... | {job['workflow_name']} | {output_count} outputs")
    else:
        print("No recent jobs.")
    
    # Stats
    stats = tracker.get_stats()
    print(f"\nTotal: {stats['total']} | Completed: {stats['completed']} | Failed: {stats['failed']}")
    
    return 0


async def cmd_result(args: argparse.Namespace) -> int:
    """Get results for a job."""
    return await cmd_result_by_id(args.prompt_id, args.prefix)


async def cmd_result_by_id(prompt_id: str, prefix: str = "comfy") -> int:
    """Get results for a job by ID."""
    client = get_client()
    config = get_config()
    output_dir = Path(config.get("output_dir", "data/comfyui/outputs"))
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Check status first
    status = await client.get_status(prompt_id)
    
    if status.get("status") == "not_found":
        print(f"Job not found: {prompt_id}")
        return 1
    
    if status.get("status") == "queued":
        print(f"⏳ Job is queued (position: {status.get('position', 'unknown')})")
        return 0
    
    if status.get("status") == "running":
        print("🔄 Job is still running...")
        return 0
    
    if status.get("status") == "failed":
        print(f"❌ Job failed: {status.get('error')}")
        return 1
    
    # Get history/results
    history = await client.get_history(prompt_id)
    
    if prompt_id not in history:
        print("No results available yet.")
        return 0
    
    job_data = history[prompt_id]
    outputs = job_data.get("outputs", {})
    
    downloaded = []
    for node_id, node_output in outputs.items():
        for output_type in ["images", "audio", "video", "gifs"]:
            if output_type in node_output:
                for file_info in node_output[output_type]:
                    filename = file_info["filename"]
                    subfolder = file_info.get("subfolder", "")
                    file_type = file_info.get("type", "output")
                    
                    try:
                        data = await client.get_file(filename, subfolder, file_type)
                        local_name = f"{prefix}_{filename}"
                        local_path = output_dir / local_name
                        local_path.write_bytes(data)
                        downloaded.append(str(local_path))
                        print(f"✅ Downloaded: {local_path}")
                    except Exception as e:
                        print(f"❌ Failed to download {filename}: {e}")
    
    if downloaded:
        print(f"\n{len(downloaded)} file(s) saved to {output_dir}")
        
        # Update tracker
        tracker = get_job_tracker()
        tracker.update_status(prompt_id, "completed")
        tracker.set_outputs(prompt_id, {"files": downloaded})
    else:
        print("No output files found.")
    
    return 0


async def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancel a running job."""
    client = get_client()
    tracker = get_job_tracker()
    
    result = await client.cancel(args.prompt_id)
    
    if result.get("status") == "cancelled":
        print(f"✅ Job cancelled: {args.prompt_id}")
        tracker.update_status(args.prompt_id, "cancelled")
        return 0
    else:
        print(f"❌ Failed to cancel: {result.get('error')}")
        return 1


def cmd_monitor(args: argparse.Namespace) -> int:
    """Open web monitor in browser."""
    import webbrowser
    
    url = "http://127.0.0.1:8000/plugins/comfyui/"
    print(f"Opening {url}")
    webbrowser.open(url)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="ComfyUI CLI - Manage ComfyUI workflows from command line",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  comfyui status                          Check if ComfyUI is running
  comfyui list                            List available workflows
  comfyui execute example_txt2img         Execute with defaults
  comfyui execute example_txt2img -p prompt="a cat" -p steps=30
  comfyui execute example_txt2img --wait  Execute and wait for result
  comfyui jobs                            Show all jobs
  comfyui result abc123                   Download results for job
  comfyui cancel abc123                   Cancel a job
  comfyui monitor                         Open web UI
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # status
    subparsers.add_parser("status", help="Check ComfyUI server status")
    
    # list
    subparsers.add_parser("list", help="List available workflows")
    
    # execute
    p_exec = subparsers.add_parser("execute", help="Execute a workflow")
    p_exec.add_argument("workflow_id", help="Workflow ID to execute")
    p_exec.add_argument("-p", "--param", dest="params", action="append", 
                        help="Parameter in key=value format (can be repeated)")
    p_exec.add_argument("--prefix", default="comfy", help="Output filename prefix")
    p_exec.add_argument("--wait", "-w", action="store_true", 
                        help="Wait for completion and download results")
    
    # jobs
    p_jobs = subparsers.add_parser("jobs", help="Show active and recent jobs")
    p_jobs.add_argument("-n", "--limit", type=int, default=10, 
                        help="Number of recent jobs to show")
    
    # result
    p_result = subparsers.add_parser("result", help="Get results for a job")
    p_result.add_argument("prompt_id", help="Job/prompt ID")
    p_result.add_argument("--prefix", default="comfy", help="Output filename prefix")
    
    # cancel
    p_cancel = subparsers.add_parser("cancel", help="Cancel a running job")
    p_cancel.add_argument("prompt_id", help="Job/prompt ID to cancel")
    
    # monitor
    subparsers.add_parser("monitor", help="Open web monitor in browser")
    
    args = parser.parse_args(argv)
    
    if not args.command:
        parser.print_help()
        return 0
    
    # Route to command
    if args.command == "status":
        return asyncio.run(cmd_status(args))
    elif args.command == "list":
        return asyncio.run(cmd_list(args))
    elif args.command == "execute":
        return asyncio.run(cmd_execute(args))
    elif args.command == "jobs":
        return asyncio.run(cmd_jobs(args))
    elif args.command == "result":
        return asyncio.run(cmd_result(args))
    elif args.command == "cancel":
        return asyncio.run(cmd_cancel(args))
    elif args.command == "monitor":
        return cmd_monitor(args)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
