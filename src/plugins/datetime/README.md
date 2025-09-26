# DateTime Plugin

The DateTime plugin provides comprehensive date and time operations for the AgentSystem. It supports current time retrieval, date formatting, calculations, timezone conversions, and calendar operations.

## Overview

This plugin offers a single tool with multiple actions for handling various date/time scenarios:
- Get current date/time in any timezone
- Format dates with custom patterns
- Parse date strings with specified formats
- Perform date arithmetic (add/subtract time periods)
- Convert between timezones
- Calculate business days, weekdays, and date differences

## Features

### Core Operations
- **Current**: Get current date/time
- **Format**: Format dates with custom patterns
- **Parse**: Parse date strings with specified formats
- **Add/Subtract**: Date arithmetic operations
- **Convert Timezone**: Convert between different timezones
- **Timestamp**: Unix timestamp operations
- **Calendar Info**: Get calendar information
- **Business Days**: Calculate business days
- **Day of Week**: Get day of the week
- **Days Until**: Calculate days until a specific date

## Configuration

Configure the DateTime plugin in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
  - datetime

servers:
  datetime:
    type: datetime
    # Optional configuration
    # default_timezone: "UTC"
```

## Usage Examples

### Get Current Time
```python
# Get current time in UTC
{
  "action": "current",
  "timezone": "UTC",
  "format": "iso"
}

# Get current time in specific timezone
{
  "action": "current", 
  "timezone": "America/New_York",
  "format": "Y-m-d H:i:s"
}
```

### Format Dates
```python
# Format a date string
{
  "action": "format",
  "datetime": "2025-01-15 14:30:00",
  "format": "F j, Y g:i A"
}
```

### Date Arithmetic
```python
# Add 30 days to current date
{
  "action": "add",
  "days": 30,
  "format": "Y-m-d"
}

# Subtract 2 weeks from a specific date
{
  "action": "subtract",
  "datetime": "2025-02-01",
  "weeks": 2
}
```

### Timezone Conversion
```python
# Convert timezone
{
  "action": "convert_timezone",
  "datetime": "2025-01-15 14:30:00",
  "timezone": "America/New_York"
}
```

## API Reference

### Parameters

- **action** (required): Operation to perform
  - `current`, `format`, `parse`, `add`, `subtract`, `convert_timezone`, `timestamp`, `calendar_info`, `business_days`, `day_of_week`, `days_until`

- **timezone**: Target timezone (e.g., 'UTC', 'America/New_York')
- **format**: Output format string
- **datetime**: Input date/time string
- **custom_format**: Custom format pattern
- **input_format**: Format of input date string
- **years/months/weeks/days/hours/minutes/seconds**: Time units for arithmetic operations

### Response Format

Returns JSON with formatted date/time results and any additional requested information.

## Common Format Strings

- ISO format: `Y-m-d\TH:i:s`
- US format: `m/d/Y g:i A`
- European format: `d.m.Y H:i`
- Long format: `F j, Y g:i A`

## Supported Timezones

Supports all standard timezone names including:
- UTC
- America/New_York, America/Los_Angeles, America/Chicago
- Europe/London, Europe/Paris, Europe/Berlin
- Asia/Tokyo, Asia/Shanghai, Asia/Kolkata
- And many more...
