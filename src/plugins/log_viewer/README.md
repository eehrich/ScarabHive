# Log Viewer Plugin

The Log Viewer plugin provides comprehensive log file management and viewing capabilities with real-time monitoring, search functionality, and web-based interface. It enables efficient log analysis and debugging workflows.

## Overview

This plugin offers both MCP tools and web UI capabilities for log file management. It provides safe, polling-based log viewing that avoids infinite logging loops while offering powerful search and monitoring features.

## Features

### Core Operations
- **List Log Files**: Discover and list available log files with metadata
- **Tail Logs**: Get recent lines from log files (configurable count)
- **Search Logs**: Pattern-based searching with regex support
- **Real-time Polling**: Web UI with automatic log updates
- **Multi-file Support**: Handle multiple log files simultaneously

### Web Interface
- **Real-time Updates**: Polling-based updates (prevents infinite loops)
- **Search Interface**: Interactive pattern search across logs
- **File Selection**: Easy switching between different log files
- **Responsive Design**: Works on desktop and mobile devices
- **Export Functions**: Download log segments and search results

## Configuration

Configure the Log Viewer plugin in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
  - log_viewer

servers:
  log_viewer:
    type: log_viewer
    # Optional configuration
    # log_directories:
    #   - "logs/"
    #   - "/var/log/application/"
    # Search configuration
    search:
      max_results: 1000
      regex_enabled: true
      case_sensitive: false
```

### Environment Variables
- `LOG_VIEWER_DIRS`: Colon-separated list of log directories
- `LOG_VIEWER_PORT`: Web UI port (default: 8080)
- `LOG_VIEWER_POLL_INTERVAL`: Update interval in milliseconds

## Usage Examples

### MCP Tool Usage

#### List Available Log Files
```python
{
  "action": "list_files"
}
```

#### Get Recent Log Lines
```python
# Get last 50 lines (default)
{
  "action": "get_tail",
  "log_file": "logs/api.log"
}

# Get specific number of lines
{
  "action": "get_tail",
  "log_file": "logs/cli.log", 
  "lines": 100
}
```

#### Search Log Files
```python
# Search specific log file
{
  "action": "search",
  "log_file": "logs/api.log",
  "pattern": "ERROR",
  "max_results": 50
}

# Search all log files
{
  "action": "search",
  "pattern": "timeout|connection.*failed",
  "max_results": 100
}

# Case-sensitive regex search
{
  "action": "search",
  "log_file": "logs/debug.log",
  "pattern": "^\\[ERROR\\].*database",
  "max_results": 25
}
```

## API Reference

### Tools

#### list_files
Lists all available log files with metadata.

**Parameters:** None

**Response:**
```json
{
  "log_files": [
    {
      "name": "api.log",
      "path": "logs/api.log",
      "size": 1048576,
      "modified": "2025-01-15T14:30:00Z",
      "lines": 5420,
      "readable": true
    },
    {
      "name": "cli.log", 
      "path": "logs/cli.log",
      "size": 524288,
      "modified": "2025-01-15T14:25:00Z", 
      "lines": 2810,
      "readable": true
    }
  ],
  "total_files": 2,
  "total_size": 1572864
}
```

#### get_tail
Retrieves the last N lines from a log file.

**Parameters:**
- **log_file** (required): Path to the log file
- **lines**: Number of lines to retrieve (default: 50)

**Response:**
```json
{
  "log_file": "logs/api.log",
  "lines_requested": 50,
  "lines_returned": 50,
  "content": [
    "2025-01-15 14:30:15 [INFO] Server started on port 8000",
    "2025-01-15 14:30:16 [INFO] Plugin loaded: weather",
    "2025-01-15 14:30:17 [ERROR] Database connection failed"
  ],
  "file_size": 1048576,
  "last_modified": "2025-01-15T14:30:00Z"
}
```

#### search
Searches for patterns in log files using regex.

**Parameters:**
- **pattern** (required): Search pattern (regex supported)
- **log_file**: Specific log file (optional, searches all if not specified)
- **max_results**: Maximum results to return (default: 100)

**Response:**
```json
{
  "pattern": "ERROR",
  "files_searched": ["logs/api.log", "logs/cli.log"],
  "total_matches": 15,
  "results": [
    {
      "file": "logs/api.log",
      "line_number": 142,
      "timestamp": "2025-01-15 14:25:30",
      "content": "2025-01-15 14:25:30 [ERROR] Database connection timeout",
      "context_before": "2025-01-15 14:25:29 [INFO] Attempting database connection",
      "context_after": "2025-01-15 14:25:31 [INFO] Retrying connection"
    }
  ],
  "search_time": 0.234
}
```

## Web Interface

### Features

#### Real-time Log Viewer
- **Live Updates**: Automatic polling for new log entries
- **Configurable Refresh**: Adjustable poll intervals
- **Multi-file Tabs**: Switch between different log files
- **Responsive Layout**: Adapts to different screen sizes

#### Search Interface  
- **Pattern Search**: Real-time pattern matching
- **Regex Support**: Full regular expression capabilities
- **Highlight Matches**: Visual highlighting of search results
- **Export Results**: Download search results as text/CSV

#### File Management
- **File Browser**: Navigate available log files
- **File Information**: Size, modification time, line count
- **Download Logs**: Export complete log files
- **Filtered Views**: Show only specific file types

### Access URLs

- **Main Panel**: `http://localhost:8080/plugins/log_viewer/panel`
- **File List API**: `http://localhost:8080/plugins/log_viewer/logs/list`
- **Content API**: `http://localhost:8080/plugins/log_viewer/logs/content/{log_name}`
- **Search API**: `http://localhost:8080/plugins/log_viewer/logs/search`

## Search Patterns

### Basic Patterns
- **Text**: `ERROR` - Simple text matching
- **Case Insensitive**: `(?i)error` - Case-insensitive matching
- **Word Boundaries**: `\bERROR\b` - Exact word matching

### Advanced Regex
- **Line Start**: `^ERROR` - Lines starting with ERROR
- **Line End**: `timeout$` - Lines ending with timeout
- **Multiple Patterns**: `ERROR|WARN|FATAL` - Any of these patterns
- **Date Patterns**: `\d{4}-\d{2}-\d{2}` - Date matching

### Common Use Cases
```regex
# Find all errors in the last hour
^2025-01-15 1[4-5]:.*ERROR

# Database-related issues
(?i)(database|db|sql).*(?:error|fail|timeout)

# HTTP error codes  
\b[4-5]\d{2}\b

# Memory or performance issues
(?i)(memory|cpu|performance).*(?:high|low|exceeded)
```

## Performance Features

### Infinite Loop Prevention
- **Polling-based Updates**: Avoids continuous streaming that can cause log loops
- **Configurable Intervals**: Adjustable poll frequency to balance freshness and performance
- **Resource Limits**: Maximum file sizes and result counts to prevent overload

### Optimization Strategies
- **Incremental Reading**: Only reads new content since last poll
- **Efficient Parsing**: Optimized log line parsing and regex matching  
- **Caching**: Temporary caching of search results and file metadata
- **Lazy Loading**: On-demand loading of large log files

## Security Considerations

### Access Control
- **File Path Validation**: Prevents directory traversal attacks
- **Readable File Checks**: Ensures only accessible files are shown
- **Sanitized Output**: HTML escaping in web interface
- **Rate Limiting**: Built-in request rate limiting

### Privacy Protection
- **No Log Storage**: Doesn't store log content permanently
- **Session Isolation**: Each session has isolated file access
- **Configurable Directories**: Restrict access to specific log directories

## Troubleshooting

### Common Issues

#### No Log Files Found
- **Check Directories**: Verify log_directories configuration
- **File Permissions**: Ensure read access to log files
- **Path Resolution**: Check that paths exist and are accessible
- **Directory Structure**: Verify directory structure matches configuration

#### Search Not Working
- **Regex Syntax**: Validate regular expression syntax
- **File Size**: Large files may take longer to search
- **Pattern Complexity**: Simplify complex regex patterns
- **Encoding Issues**: Ensure log files use supported encoding

#### Web UI Issues  
- **Port Conflicts**: Check if configured port is available
- **Browser Cache**: Clear browser cache for UI updates
- **JavaScript Errors**: Check browser console for errors
- **CORS Issues**: Verify allowed_origins configuration

### Performance Issues

#### Slow Search
- **Large Files**: Consider splitting large log files
- **Complex Regex**: Simplify search patterns
- **Multiple Files**: Search specific files instead of all files
- **Resource Limits**: Increase max_results gradually

#### High Memory Usage
- **File Size Limits**: Configure max_file_size appropriately  
- **Result Limits**: Reduce max_results for searches
- **Poll Frequency**: Increase poll_interval to reduce updates
- **Cache Cleanup**: Regular cleanup of temporary cache

## Integration Examples

### CI/CD Integration
```bash
# Search for errors in deployment logs
curl "http://localhost:8080/plugins/log_viewer/logs/search" \
  -d "pattern=ERROR|FAIL" \
  -d "log_file=logs/deployment.log"
```

### Monitoring Integration
```python
# Automated error detection
import requests

response = requests.get(
    "http://localhost:8080/plugins/log_viewer/logs/search",
    params={
        "pattern": "CRITICAL|FATAL|ERROR", 
        "max_results": 10
    }
)

if response.json()["total_matches"] > 0:
    # Send alert
    pass
```

### Log Analysis Workflows
```python
# Multi-step log analysis
# 1. List available logs
# 2. Search for specific patterns  
# 3. Get context around matches
# 4. Export results for further analysis
```