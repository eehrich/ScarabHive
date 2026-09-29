# HTTP Server Plugin

The HTTP Server plugin provides HTTP adapter capabilities for tool servers, exposing wrapped tool servers via REST API endpoints. It enables HTTP-based access to tool functionality for web applications and external integrations.

## Overview

This plugin acts as an HTTP bridge for the tool servers, allowing web applications, mobile apps, and other HTTP clients to interact with their tools through standard REST API endpoints.

## Features

### Core Operations
- **Health Checks**: Monitor tool server status and connectivity
- **Tool Invocation**: Call tools via HTTP POST requests
- **REST API Interface**: Standard HTTP methods and response formats
- **Error Handling**: Comprehensive error responses and status codes
- **Request Validation**: Input parameter validation and sanitization

### HTTP Integration
- **Standard Methods**: GET, POST, PUT, DELETE support
- **JSON Payloads**: JSON request and response bodies
- **Status Codes**: Proper HTTP status code usage
- **CORS Support**: Cross-origin request handling
- **Authentication**: Configurable authentication methods

## Configuration

Configure the HTTP Server plugin in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
  - http_server

servers:
  http_server:
    type: http_server
    host: 127.0.0.1
    port: 9000
    # Additional optional settings:
    timeout: 30
    ssl_verify: true
```

### Environment Variables
- `HTTP_SERVER_HOST`: Server bind address (default: 127.0.0.1)
- `HTTP_SERVER_PORT`: Server port (default: 8080)
- `HTTP_SERVER_AUTH_KEY`: API key for authentication
- `HTTP_SERVER_TIMEOUT`: Request timeout in seconds

## Usage Examples

### Health Check
```python
# Check server health
{
  "action": "health"
}
```

```bash
# HTTP request
curl http://localhost:8080/health
```

### Tool Invocation
```python
# Call tool via HTTP adapter
{
  "action": "call",
  "tool": "weather_forecast",
  "params": {
    "location": "New York",
    "days": 3
  }
}
```

```bash
# HTTP request  
curl -X POST http://localhost:8080/tools/weather_forecast \
  -H "Content-Type: application/json" \
  -d '{"location": "New York", "days": 3}'
```

## API Reference

### Tools

#### health
Performs health check on the tool server and HTTP adapter.

**Parameters:** None

**Response:**
```json
{
  "status": "healthy",
  "server": "http_server",
  "version": "1.0.0",
  "uptime": 3600,
  "mcp_server_status": "connected",
  "memory_usage": "45MB",
  "active_connections": 5
}
```

#### call
Invokes a tool through the HTTP adapter.

**Parameters:**
- **tool** (required): tool name to invoke
- **params**: Parameters object to pass to the tool

**Response:**
```json
{
  "success": true,
  "tool": "weather_forecast",
  "result": {
    "location": "New York",
    "temperature": 22,
    "condition": "sunny",
    "forecast": [...]
  },
  "execution_time": 1.23,
  "request_id": "req_abc123"
}
```

## HTTP Endpoints

### Core Endpoints

#### GET /health
Server health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2025-01-15T14:30:00Z",
  "version": "1.0.0"
}
```

#### GET /tools
List available tools.

**Response:**
```json
{
  "tools": [
    {
      "name": "weather_forecast",
      "description": "Get weather forecast",
      "parameters": {...}
    }
  ],
  "total_tools": 5
}
```

#### POST /tools/{tool_name}
Invoke specific tool.

**Request Body:**
```json
{
  "location": "Paris",
  "units": "metric"
}
```

**Response:**
```json
{
  "success": true,
  "result": {...},
  "execution_time": 0.85
}
```

### Resource Endpoints

#### GET /resources
List available resources.

#### GET /resources/{resource_id}
Get specific resource content.

### Utility Endpoints

#### GET /status
Detailed server status information.

#### POST /validate
Validate tool parameters without execution.

## Request/Response Format

### Standard Request
```json
{
  "tool": "tool_name",
  "params": {
    "param1": "value1",
    "param2": "value2"
  },
  "request_id": "optional_id",
  "timeout": 30
}
```

### Standard Response
```json
{
  "success": true,
  "result": {...},
  "metadata": {
    "tool": "tool_name",
    "execution_time": 1.23,
    "request_id": "req_123",
    "timestamp": "2025-01-15T14:30:00Z"
  }
}
```

### Error Response
```json
{
  "success": false,
  "error": {
    "code": "INVALID_PARAMETERS",
    "message": "Missing required parameter: location",
    "details": {
      "missing_params": ["location"],
      "provided_params": ["units"]
    }
  },
  "request_id": "req_123"
}
```

## HTTP Status Codes

### Success Codes
- **200 OK**: Successful tool execution
- **201 Created**: Resource created successfully
- **202 Accepted**: Request accepted for async processing

### Client Error Codes
- **400 Bad Request**: Invalid request parameters
- **401 Unauthorized**: Authentication required
- **403 Forbidden**: Insufficient permissions  
- **404 Not Found**: Tool or resource not found
- **422 Unprocessable Entity**: Invalid parameter values

### Server Error Codes
- **500 Internal Server Error**: tool server error
- **502 Bad Gateway**: tool server unavailable
- **503 Service Unavailable**: Server overloaded
- **504 Gateway Timeout**: tool execution timeout

## Authentication

> **Implementation status (2026-06-14):** The server implements **API-key auth on `POST /call`** — send the key via `X-API-Key: <key>` or `Authorization: Bearer <key>`. The key is read from the `HTTP_SERVER_AUTH_KEY` env var (or `server_config.auth_key`). `GET /health` is intentionally unauthenticated (liveness). When **no** key is configured the server **refuses to bind a non-loopback host** (e.g. `0.0.0.0`) and only serves on `127.0.0.1`. The Basic-auth, CORS, rate-limiting, `/validate`, `/tools/*`, `/metrics`, and WebSocket sections below are **not yet implemented** (roadmap, not current behavior); the only real endpoints are `/health` and `/call`.

### API Key Authentication
```bash
# Include API key in header
curl -H "X-API-Key: your-api-key" \
  http://localhost:8080/tools/weather_forecast
```

### Bearer Token Authentication  
```bash
# Include bearer token
curl -H "Authorization: Bearer your-token" \
  http://localhost:8080/tools/weather_forecast
```

### Basic Authentication
```bash
# Basic auth credentials
curl -u username:password \
  http://localhost:8080/tools/weather_forecast
```

## CORS Configuration

### Enable CORS
```yaml
cors_enabled: true
allowed_origins: 
  - "https://myapp.com"
  - "https://localhost:3000"
allowed_methods: ["GET", "POST", "PUT", "DELETE"]
allowed_headers: ["Content-Type", "Authorization"]
```

### Development Mode
```yaml
cors_enabled: true
allowed_origins: ["*"]  # Allow all origins (development only)
```

## Rate Limiting

### Configuration
```yaml
rate_limit:
  enabled: true
  requests_per_minute: 60
  burst_limit: 10
  per_ip: true
```

### Rate Limit Headers
```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 45
X-RateLimit-Reset: 1642694400
```

## WebSocket Support

### Real-time Updates
```javascript
// WebSocket connection for real-time tool results
const ws = new WebSocket('ws://localhost:8080/ws');

ws.send(JSON.stringify({
  action: 'subscribe',
  tool: 'weather_forecast',
  params: { location: 'London' }
}));

ws.onmessage = (event) => {
  const result = JSON.parse(event.data);
  console.log('Real-time result:', result);
};
```

### Event Streaming
- **Tool Results**: Real-time tool execution results
- **Status Updates**: Server status changes
- **Error Notifications**: Real-time error reporting

## Integration Examples

### JavaScript/Node.js
```javascript
const axios = require('axios');

async function callWeatherTool(location) {
  try {
    const response = await axios.post(
      'http://localhost:8080/tools/weather_forecast',
      { location: location, days: 5 },
      { headers: { 'X-API-Key': 'your-api-key' } }
    );
    
    return response.data.result;
  } catch (error) {
    console.error('Error:', error.response.data);
    throw error;
  }
}
```

### Python
```python
import requests

def call_weather_tool(location):
    url = 'http://localhost:8080/tools/weather_forecast'
    headers = {'X-API-Key': 'your-api-key'}
    data = {'location': location, 'days': 5}
    
    response = requests.post(url, json=data, headers=headers)
    
    if response.status_code == 200:
        return response.json()['result']
    else:
        raise Exception(f"API Error: {response.json()}")
```

### curl Examples
```bash
# List available tools
curl http://localhost:8080/tools

# Call weather tool
curl -X POST http://localhost:8080/tools/weather_forecast \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{"location": "Tokyo", "days": 3}'

# Check server health
curl http://localhost:8080/health
```

## Monitoring and Logging

### Request Logging
- **Access Logs**: HTTP request/response logging
- **Error Logs**: Detailed error information
- **Performance Metrics**: Response times and throughput
- **Security Events**: Authentication and authorization events

### Metrics Endpoints
```bash
# Prometheus metrics
curl http://localhost:8080/metrics

# Health check with details
curl http://localhost:8080/health?detailed=true
```

## Security Best Practices

### Production Configuration
- **Enable Authentication**: Require API keys or tokens
- **Restrict CORS**: Limit allowed origins in production
- **Use HTTPS**: Enable TLS encryption
- **Rate Limiting**: Implement appropriate rate limits
- **Input Validation**: Validate all incoming parameters

### Network Security
- **Firewall Rules**: Restrict network access
- **Reverse Proxy**: Use nginx/Apache for SSL termination
- **Load Balancing**: Distribute traffic across instances
- **DDoS Protection**: Implement DDoS mitigation

## Troubleshooting

### Common Issues

#### Connection Errors
- **Port Already in Use**: Change port or stop conflicting service
- **Firewall Blocking**: Check firewall rules and open required ports
- **Network Connectivity**: Verify network configuration and routing

#### Authentication Problems
- **Invalid API Key**: Verify API key configuration and format
- **CORS Issues**: Check CORS settings and allowed origins
- **Missing Headers**: Ensure required headers are included

#### Performance Issues
- **High Latency**: Check tool server performance and network
- **Rate Limiting**: Verify rate limits and adjust if necessary
- **Memory Usage**: Monitor memory consumption and limits

#### Tool integration Issues
- **Tool Not Found**: Verify tool is properly registered
- **Parameter Errors**: Check parameter names and types
- **Timeout Errors**: Adjust timeout settings for slow tools