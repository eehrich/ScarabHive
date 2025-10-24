"""
TODO Management Plugin - CLI Interface

Provides command-line interface for task management operations.
Supports both direct CLI mode and MCP server mode.

Usage:
    # Direct CLI commands
    python -m plugins.todo_management create --title "Implement auth"
    python -m plugins.todo_management list --status in-progress
    python -m plugins.todo_management update task_001 --status completed
    python -m plugins.todo_management summary
    
    # MCP server mode
    python -m plugins.todo_management --server --port 9012
"""

import argparse
import asyncio
import json
import sys
from typing import Any, Dict

from plugins.todo_management.server import TodoManagementServer


def format_task_summary(task: Dict[str, Any], verbose: bool = False) -> str:
    """Format task for console output"""
    task_id = task.get("task_id", "???")
    title = task.get("title", "Untitled")
    status = task.get("status", "unknown")
    priority = task.get("priority", "medium")
    progress = task.get("progress", 0)
    is_blocked = task.get("is_blocked", False)
    
    # Status icon
    status_icons = {
        "not-started": "☐",
        "in-progress": "▶",
        "completed": "✓",
        "blocked": "🚫",
        "cancelled": "✗",
    }
    icon = status_icons.get(status, "?")
    
    # Priority color (ANSI)
    priority_colors = {
        "low": "\033[90m",      # Gray
        "medium": "\033[0m",    # Default
        "high": "\033[33m",     # Yellow
        "critical": "\033[31m", # Red
    }
    color = priority_colors.get(priority, "\033[0m")
    reset = "\033[0m"
    
    # Build summary line
    blocked_flag = " [BLOCKED]" if is_blocked else ""
    summary = f"{icon} {color}{task_id}{reset}: {title} ({status}, {progress}%){blocked_flag}"
    
    if verbose:
        # Add extra details
        tags = task.get("tags", [])
        thinking_session = task.get("thinking_session_id")
        thought_num = task.get("thought_number")
        
        details = []
        if tags:
            details.append(f"Tags: {', '.join(tags)}")
        if thinking_session:
            details.append(f"Thinking: {thinking_session[:8]}...")
        if thought_num is not None:
            details.append(f"Thought #{thought_num}")
        
        if details:
            summary += f"\n  {' | '.join(details)}"
    
    return summary


async def cli_create(server: TodoManagementServer, args: argparse.Namespace) -> int:
    """Handle 'create' command"""
    try:
        result = await server.create_todo(
            title=args.title,
            description=args.description,
            priority=args.priority,
            tags=args.tags or [],
            depends_on=args.depends_on or [],
            thinking_session_id=args.thinking_session,
            thought_number=args.thought_number,
            context={"session_id": args.session},
        )
        
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            task = result["task"]
            print(f"✓ Created {result['task_id']}: {task['title']}")
            if result.get("is_blocked"):
                print("  ⚠ Task is BLOCKED by incomplete dependencies")
        
        return 0
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cli_update(server: TodoManagementServer, args: argparse.Namespace) -> int:
    """Handle 'update' command"""
    try:
        result = await server.update_todo(
            task_id=args.task_id,
            new_status=args.status,
            progress=args.progress,
            priority=args.priority,
            add_note=args.note,
            add_tags=args.add_tags,
            remove_tags=args.remove_tags,
            context={"session_id": args.session},
        )
        
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            changes = result.get("changes", {})
            print(f"✓ Updated {result['task_id']}")
            for key, value in changes.items():
                print(f"  {key}: {value}")
        
        return 0
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cli_list(server: TodoManagementServer, args: argparse.Namespace) -> int:
    """Handle 'list' command"""
    try:
        result = await server.list_todos(
            filter_status=args.status,
            filter_priority=args.priority,
            filter_tags=args.tags,
            only_unblocked=args.unblocked,
            thinking_session_id=args.thinking_session,
            thought_number=args.thought_number,
            sort_by=args.sort_by,
            limit=args.limit,
            context={"session_id": args.session},
        )
        
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            tasks = result["tasks"]
            
            if not tasks:
                print("No tasks found.")
                return 0
            
            # Print header
            print(f"Found {result['returned_count']} tasks "
                  f"(filtered from {result['filtered_count']} / {result['total_count']} total)\n")
            
            # Print tasks
            for task in tasks:
                print(format_task_summary(task, verbose=args.verbose))
        
        return 0
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cli_get(server: TodoManagementServer, args: argparse.Namespace) -> int:
    """Handle 'get' command"""
    try:
        result = await server.get_todo(
            task_id=args.task_id,
            context={"session_id": args.session},
        )
        
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            task = result["task"]
            dep_info = result["dependency_info"]
            
            print(f"Task: {task['task_id']}")
            print(f"Title: {task['title']}")
            print(f"Status: {task['status']} ({task['progress']}%)")
            print(f"Priority: {task['priority']}")
            
            if task.get("description"):
                print(f"Description: {task['description']}")
            
            if task.get("tags"):
                print(f"Tags: {', '.join(task['tags'])}")
            
            print(f"Created: {task['created_at']}")
            print(f"Updated: {task['updated_at']}")
            
            if task.get("started_at"):
                print(f"Started: {task['started_at']}")
            
            if task.get("completed_at"):
                print(f"Completed: {task['completed_at']}")
            
            # Dependencies
            if dep_info["blocked_by"]:
                print("\nBlocked by:")
                for dep in dep_info["blocked_by"]:
                    print(f"  - {dep['task_id']}: {dep['title']} ({dep['status']})")
            
            if dep_info["blocking"]:
                print("\nBlocking:")
                for dep in dep_info["blocking"]:
                    print(f"  - {dep['task_id']}: {dep['title']} ({dep['status']})")
            
            # Notes
            if task.get("notes"):
                print("\nNotes:")
                for note in task["notes"]:
                    print(f"  {note}")
            
            # Thinking session
            if task.get("thinking_session_id"):
                print(f"\nThinking Session: {task['thinking_session_id']}")
                if task.get("thought_number"):
                    print(f"Thought Number: {task['thought_number']}")
        
        return 0
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cli_delete(server: TodoManagementServer, args: argparse.Namespace) -> int:
    """Handle 'delete' command"""
    try:
        result = await server._delete_todo_impl(
            task_id=args.task_id,
            cascade=args.cascade,
            context={"session_id": args.session},
        )
        
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"✓ Deleted {result['task_id']}")
            
            cascade_deleted = result.get("cascade_deleted", [])
            if cascade_deleted:
                print(f"  Also deleted {len(cascade_deleted)} dependent tasks:")
                for dep_id in cascade_deleted:
                    print(f"    - {dep_id}")
        
        return 0
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cli_summary(server: TodoManagementServer, args: argparse.Namespace) -> int:
    """Handle 'summary' command"""
    try:
        result = await server.get_progress_summary(
            context={"session_id": args.session},
        )
        
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"Total Tasks: {result['total_tasks']}")
            print(f"Overall Progress: {result['overall_progress']}%")
            print(f"Completion Rate: {result['completion_rate']}\n")
            
            # By status
            print("By Status:")
            for status, count in result["by_status"].items():
                if count > 0:
                    print(f"  {status}: {count}")
            
            # By priority
            print("\nBy Priority:")
            for priority, count in result["by_priority"].items():
                if count > 0:
                    print(f"  {priority}: {count}")
            
            # Blocked
            if result["blocked_count"] > 0:
                print(f"\n⚠ {result['blocked_count']} tasks are BLOCKED")
            
            # Next unblocked
            next_tasks = result.get("next_unblocked", [])
            if next_tasks:
                print("\nNext Unblocked Tasks:")
                for task in next_tasks:
                    print(f"  - {task['task_id']}: {task['title']} ({task['priority']})")
        
        return 0
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


async def cli_clear(server: TodoManagementServer, args: argparse.Namespace) -> int:
    """Handle 'clear' command (delete all tasks)"""
    try:
        # Load session to get all task IDs
        session_id = args.session or "default"
        collection = server._load_session(session_id)
        
        task_count = len(collection.tasks)
        
        if task_count == 0:
            print("No tasks to clear.")
            return 0
        
        # Confirm unless --force
        if not args.force:
            response = input(f"Delete all {task_count} tasks? (y/N): ")
            if response.lower() != "y":
                print("Cancelled.")
                return 0
        
        # Delete all tasks
        task_ids = list(collection.tasks.keys())
        for task_id in task_ids:
            await server._delete_todo_impl(
                task_id=task_id,
                cascade=True,
                context={"session_id": session_id},
            )
        
        print(f"✓ Cleared {task_count} tasks from session {session_id}")
        return 0
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cli_main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        description="TODO Management Plugin - Task lifecycle tracking for agents"
    )
    
    # Global options
    parser.add_argument(
        "--session",
        default="default",
        help="Session ID (default: 'default')",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="Run as MCP server",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9012,
        help="MCP server port (default: 9012)",
    )
    
    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")
    
    # create
    create_parser = subparsers.add_parser("create", help="Create a new task")
    create_parser.add_argument("--title", required=True, help="Task title")
    create_parser.add_argument("--description", help="Task description")
    create_parser.add_argument(
        "--priority",
        choices=["low", "medium", "high", "critical"],
        default="medium",
        help="Priority level",
    )
    create_parser.add_argument(
        "--tags",
        nargs="*",
        help="Tags for categorization",
    )
    create_parser.add_argument(
        "--depends-on",
        nargs="*",
        help="Task IDs this task depends on",
    )
    create_parser.add_argument(
        "--thinking-session",
        help="Link to sequential_thinking session",
    )
    create_parser.add_argument(
        "--thought-number",
        type=int,
        help="Associated thought number",
    )
    
    # update
    update_parser = subparsers.add_parser("update", help="Update a task")
    update_parser.add_argument("task_id", help="Task ID to update")
    update_parser.add_argument(
        "--status",
        choices=["not-started", "in-progress", "completed", "blocked", "cancelled"],
        help="New status",
    )
    update_parser.add_argument(
        "--progress",
        type=int,
        help="Completion percentage (0-100)",
    )
    update_parser.add_argument(
        "--priority",
        choices=["low", "medium", "high", "critical"],
        help="New priority",
    )
    update_parser.add_argument(
        "--note",
        help="Add note to task history",
    )
    update_parser.add_argument(
        "--add-tags",
        nargs="*",
        help="Tags to add",
    )
    update_parser.add_argument(
        "--remove-tags",
        nargs="*",
        help="Tags to remove",
    )
    
    # list
    list_parser = subparsers.add_parser("list", help="List tasks with filters")
    list_parser.add_argument(
        "--status",
        nargs="*",
        choices=["not-started", "in-progress", "completed", "blocked", "cancelled"],
        help="Filter by status",
    )
    list_parser.add_argument(
        "--priority",
        nargs="*",
        choices=["low", "medium", "high", "critical"],
        help="Filter by priority",
    )
    list_parser.add_argument(
        "--tags",
        nargs="*",
        help="Filter by tags (OR logic)",
    )
    list_parser.add_argument(
        "--unblocked",
        action="store_true",
        help="Only show unblocked tasks",
    )
    list_parser.add_argument(
        "--thinking-session",
        help="Filter by sequential_thinking session",
    )
    list_parser.add_argument(
        "--thought-number",
        type=int,
        help="Filter by thought number",
    )
    list_parser.add_argument(
        "--sort-by",
        choices=["priority", "created_at", "updated_at", "progress"],
        default="created_at",
        help="Sort field",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        help="Max results",
    )
    list_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output (show tags, thinking session)",
    )
    
    # get
    get_parser = subparsers.add_parser("get", help="Get task details")
    get_parser.add_argument("task_id", help="Task ID")
    
    # delete
    delete_parser = subparsers.add_parser("delete", help="Delete a task")
    delete_parser.add_argument("task_id", help="Task ID")
    delete_parser.add_argument(
        "--cascade",
        action="store_true",
        help="Also delete dependent tasks",
    )
    
    # summary
    subparsers.add_parser("summary", help="Get progress summary")
    
    # clear
    clear_parser = subparsers.add_parser("clear", help="Delete all tasks")
    clear_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation",
    )
    
    args = parser.parse_args()
    
    # MCP server mode
    if args.server:
        print(f"Starting TODO Management MCP server on port {args.port}...")
        print("(MCP server implementation pending)")
        sys.exit(1)
    
    # CLI mode - execute command
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Create server instance with proper config objects
    from agent_system.config.models import AgentSystemConfig, MCPConfig
    
    system_config = AgentSystemConfig()
    mcp_config = MCPConfig(
        type="todo_management",
        enabled=True,
        storage_path="data/todos",
        max_tasks_per_session=1000,
        enable_dependencies=True,
        auto_save=True,
    )
    
    from .plugin import PLUGIN_FACTORY
    server = PLUGIN_FACTORY("todo_management", system_config, mcp_config)
    
    # Dispatch command
    handlers = {
        "create": cli_create,
        "update": cli_update,
        "list": cli_list,
        "get": cli_get,
        "delete": cli_delete,
        "summary": cli_summary,
        "clear": cli_clear,
    }
    
    handler = handlers.get(args.command)
    if not handler:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)
    
    # Run async handler
    exit_code = asyncio.run(handler(server, args))
    sys.exit(exit_code)


if __name__ == "__main__":
    cli_main()
