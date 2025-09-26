# Weather Plugin

The Weather plugin provides comprehensive weather information for any location worldwide. It supports multiple weather data sources and offers detailed forecasts, current conditions, and various data formats.

## Overview

This plugin fetches weather data from multiple reliable sources including:
- **met.no** (Norwegian Meteorological Institute) - Default, high accuracy
- **wttr.in** - Simple, text-based weather service
- **weather.gov** - US National Weather Service
- **marine.weather.gov** - Marine weather data

## Features

### Core Operations
- **Forecast**: Multi-day weather forecasts (1-7 days)
- **Current Conditions**: Real-time weather data
- **Location Search**: Find weather for cities, coordinates, airports
- **Multiple Sources**: Automatic fallback between weather services
- **Format Options**: Detailed, daily summary, or hourly data
- **Marine Weather**: Specialized marine forecasts
- **Solar Data**: UV index and solar radiation information

### Data Sources
- **Primary**: met.no (Norwegian Meteorological Institute)
- **Backup**: wttr.in, weather.gov, marine.weather.gov
- **Coverage**: Worldwide locations
- **Update Frequency**: Real-time to hourly updates

## Configuration

Configure the Weather plugin in `config/mcp.yaml`:

```yaml
mcp:
  enabled_servers:
  - weather

servers:
  weather:
    type: weather
    # Optional configuration can be added here
    # default_units: "metric"
    # cache_duration: 300
```

### Environment Variables
- `WEATHER_DEFAULT_SOURCE`: Default weather data source
- `WEATHER_API_KEY`: API key if required by source

## Usage Examples

### Basic Weather Query
```python
# Get 3-day forecast for a city
{
  "action": "forecast",
  "location": "New York, NY",
  "days": 3,
  "units": "metric"
}

# Current weather conditions
{
  "action": "get",
  "location": "London, UK",
  "source": "met.no"
}
```

### Advanced Queries
```python
# Detailed forecast with specific source
{
  "action": "forecast",
  "location": "Tokyo, Japan",
  "source": "met.no",
  "days": 7,
  "units": "metric",
  "summary_format": "detailed",
  "include_radiation": true
}

# Marine weather
{
  "action": "forecast",
  "location": "40.7,-74.0",  # Coordinates
  "source": "marine.weather.gov",
  "include_marine": true,
  "days": 5
}
```

### Quick Lookups
```python
# Simple current conditions
{
  "action": "check",
  "location": "Berlin"
}

# Search for location weather
{
  "action": "search",
  "location": "Paris, France",
  "units": "imperial"
}
```

## API Reference

### Parameters

- **action** (required): Weather operation
  - `forecast`, `search`, `query`, `get`, `check`, `lookup`

- **location** (required): Location name, city, or coordinates
  - Examples: "New York", "London, UK", "40.7,-74.0"

- **source**: Weather data source
  - `met.no` (default), `wttr.in`, `weather.gov`, `marine.weather.gov`

- **days**: Forecast days (1-7, default: 3)

- **units**: Temperature and measurement units
  - `metric` (default), `imperial`

- **include_marine**: Include marine weather data (boolean)

- **summary_format**: Output format
  - `detailed` (default), `daily_summary`, `hourly`

- **include_radiation**: Include UV/solar data (boolean)

### Response Format

```json
{
  "location": "Location Name",
  "source": "met.no",
  "current": {
    "temperature": 22,
    "condition": "partly cloudy",
    "humidity": 65,
    "wind_speed": 12,
    "wind_direction": "SW"
  },
  "forecast": [
    {
      "date": "2025-01-15",
      "high": 25,
      "low": 18,
      "condition": "sunny",
      "precipitation": 0
    }
  ],
  "units": "metric"
}
```

## Location Formats

Supported location formats:
- **City Names**: "New York", "London", "Tokyo"
- **City, Country**: "Paris, France", "Sydney, Australia"  
- **Coordinates**: "40.7128,-74.0060" (latitude,longitude)
- **Airport Codes**: "JFK", "LHR", "NRT"
- **Postal Codes**: "10001", "SW1A 1AA"

## Data Sources Details

### met.no (Default)
- **Coverage**: Worldwide
- **Accuracy**: High (official meteorological service)
- **Update**: Hourly
- **Features**: Full forecast data, marine weather

### wttr.in  
- **Coverage**: Worldwide
- **Accuracy**: Good
- **Update**: Frequent
- **Features**: Simple format, ASCII art weather

### weather.gov
- **Coverage**: United States only
- **Accuracy**: Very high (official NWS)
- **Update**: Real-time
- **Features**: Detailed US forecasts, alerts

### marine.weather.gov
- **Coverage**: US coastal and marine areas
- **Accuracy**: High (specialized marine data)
- **Update**: Regular
- **Features**: Marine conditions, wave height, tide data

## Troubleshooting

### Common Issues

1. **Location Not Found**
   - Try different format: "City, Country" instead of just "City"
   - Use coordinates for precise locations
   - Check spelling of location name

2. **No Data Returned**
   - Try different weather source
   - Check internet connectivity
   - Verify location exists in selected source's coverage

3. **Marine Data Unavailable**
   - Use marine.weather.gov source for US coastal areas
   - Ensure location is near water body
   - Try coordinates instead of city name