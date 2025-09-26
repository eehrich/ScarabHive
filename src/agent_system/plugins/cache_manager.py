"""
Cache management utility for AgentSystem plugins.

Usage:
    python -m agent_system.plugins.cache_manager --help
    python -m agent_system.plugins.cache_manager info
    python -m agent_system.plugins.cache_manager clean --plugin web_scraper
    python -m agent_system.plugins.cache_manager clear --all
"""
import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional
from agent_system.plugins.cache import PluginCache


def find_cache_root() -> Path:
    """Find the project cache root directory."""
    # Look for project root by finding pyproject.toml
    current_path = Path.cwd()
    while current_path.parent != current_path:
        if (current_path / "pyproject.toml").exists():
            return current_path / ".cache"
        current_path = current_path.parent
    
    # Fallback to current directory
    return Path.cwd() / ".cache"


async def show_cache_info(plugin_name: Optional[str] = None):
    """Show cache information for all plugins or a specific plugin."""
    cache_root = find_cache_root()
    
    if not cache_root.exists():
        print("No cache directory found.")
        return
    
    if plugin_name:
        # Show info for specific plugin
        if not (cache_root / plugin_name).exists():
            print(f"No cache found for plugin: {plugin_name}")
            return
        
        cache = PluginCache(plugin_name, cache_root.parent)
        info = cache.get_cache_info()
        print_cache_info(info)
    else:
        # Show info for all plugins
        total_files = 0
        total_size = 0
        
        print("AgentSystem Plugin Cache Information")
        print("=" * 50)
        
        for plugin_dir in cache_root.iterdir():
            if plugin_dir.is_dir():
                cache = PluginCache(plugin_dir.name, cache_root.parent)
                info = cache.get_cache_info()
                
                total_files += info.get('total_files', 0)
                total_size += info.get('total_size_mb', 0)
                
                print(f"\n{plugin_dir.name}:")
                print_cache_info(info, indent="  ")
        
        print(f"\nTOTAL: {total_files} files, {total_size:.2f} MB")


def print_cache_info(info: dict, indent: str = ""):
    """Print formatted cache information."""
    print(f"{indent}Files: {info.get('total_files', 0)} total, "
          f"{info.get('valid_files', 0)} valid, "
          f"{info.get('expired_files', 0)} expired")
    print(f"{indent}Size: {info.get('total_size_mb', 0):.2f} MB")
    print(f"{indent}TTL: {info.get('default_ttl', 0)} seconds")
    print(f"{indent}Path: {info.get('cache_dir', 'Unknown')}")
    
    if 'error' in info:
        print(f"{indent}Error: {info['error']}")


async def cleanup_cache(plugin_name: str):
    """Clean up expired entries for a specific plugin."""
    cache_root = find_cache_root()
    
    if not (cache_root / plugin_name).exists():
        print(f"No cache found for plugin: {plugin_name}")
        return
    
    cache = PluginCache(plugin_name, cache_root.parent)
    deleted_count = await cache.cleanup_expired()
    
    print(f"Cleaned up {deleted_count} expired cache entries for {plugin_name}")


async def clear_cache(plugin_name: Optional[str] = None, all_plugins: bool = False):
    """Clear cache for a specific plugin or all plugins."""
    cache_root = find_cache_root()
    
    if not cache_root.exists():
        print("No cache directory found.")
        return
    
    if all_plugins:
        # Clear all plugin caches
        total_deleted = 0
        
        for plugin_dir in cache_root.iterdir():
            if plugin_dir.is_dir():
                cache = PluginCache(plugin_dir.name, cache_root.parent)
                deleted_count = await cache.clear()
                total_deleted += deleted_count
                print(f"Cleared {deleted_count} cache files for {plugin_dir.name}")
        
        print(f"\nTotal: Cleared {total_deleted} cache files")
        
    elif plugin_name:
        # Clear specific plugin cache
        if not (cache_root / plugin_name).exists():
            print(f"No cache found for plugin: {plugin_name}")
            return
        
        cache = PluginCache(plugin_name, cache_root.parent)
        deleted_count = await cache.clear()
        
        print(f"Cleared {deleted_count} cache files for {plugin_name}")
    else:
        print("Please specify --plugin <name> or --all")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="AgentSystem Plugin Cache Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s info                           # Show info for all plugins
  %(prog)s info --plugin web_scraper      # Show info for specific plugin
  %(prog)s clean --plugin duckduckgo_search  # Clean expired entries
  %(prog)s clear --plugin web_scraper     # Clear all entries for plugin
  %(prog)s clear --all                    # Clear all caches
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Info command
    info_parser = subparsers.add_parser('info', help='Show cache information')
    info_parser.add_argument('--plugin', help='Show info for specific plugin')
    
    # Clean command
    clean_parser = subparsers.add_parser('clean', help='Clean expired cache entries')
    clean_parser.add_argument('--plugin', required=True, help='Plugin name to clean')
    
    # Clear command
    clear_parser = subparsers.add_parser('clear', help='Clear cache entries')
    clear_group = clear_parser.add_mutually_exclusive_group(required=True)
    clear_group.add_argument('--plugin', help='Plugin name to clear')
    clear_group.add_argument('--all', action='store_true', help='Clear all plugin caches')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # Run appropriate command
    try:
        if args.command == 'info':
            asyncio.run(show_cache_info(args.plugin))
        elif args.command == 'clean':
            asyncio.run(cleanup_cache(args.plugin))
        elif args.command == 'clear':
            asyncio.run(clear_cache(args.plugin, args.all))
        else:
            parser.print_help()
    except KeyboardInterrupt:
        print("\nOperation cancelled.")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()