from __future__ import annotations

from typing import Any, cast
import datetime
from urllib.parse import quote

# Nominatim refuses requests without an identifying User-Agent (403).
_HEADERS = {"User-Agent": "AgentSystem-Weather/1.0 (github.com/agent-system)"}


def _day_night_pairs(periods: list[dict[str, Any]]) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Pair NWS forecast periods into (day, night) by their isDaytime flag.

    A forecast issued in the evening starts with a night period ("Tonight");
    it stands alone as (None, night) instead of shifting every later pair.
    """
    pairs: list[tuple[dict[str, Any] | None, dict[str, Any] | None]] = []
    i = 0
    while i < len(periods):
        period = periods[i]
        if not period.get("isDaytime", True):
            pairs.append((None, period))
            i += 1
            continue
        following = periods[i + 1] if i + 1 < len(periods) else None
        if following is not None and not following.get("isDaytime", False):
            pairs.append((period, following))
            i += 2
        else:
            pairs.append((period, None))
            i += 1
    return pairs


def _period_date(day_period: dict[str, Any] | None, night_period: dict[str, Any] | None) -> str:
    """The date a (day, night) pair belongs to.

    A night period alone is dated by the evening it belongs to: "Overnight"
    starting at 02:00 is the night of the day before, not of today.
    """
    if day_period:
        return day_period["startTime"][:10]
    assert night_period is not None
    start = datetime.datetime.fromisoformat(night_period["startTime"])
    return (start - datetime.timedelta(hours=12)).date().isoformat()


def _new_met_day() -> dict[str, Any]:
    return {"temperatures": [], "winds": [], "precipitation": 0.0, "symbols": [], "entries": []}


def _hour_ints(hours: list[dict[str, Any]], key: str) -> list[int]:
    """The whole-number values of one wttr.in hourly field (they come as text)."""
    return [int(h[key]) for h in hours if str(h.get(key, "")).isdigit()]


def _nws_period(period: dict[str, Any]) -> dict[str, Any]:
    return {
        "temperature": period["temperature"],
        "temperature_unit": period["temperatureUnit"],
        "wind_speed": period["windSpeed"],
        "wind_direction": period["windDirection"],
        "weather_desc": period["shortForecast"],
        "detailed_forecast": period["detailedForecast"],
    }


async def fetch_wttr(location: str, days: int, units: str, ssl_verify: bool,
                     hourly: bool = False) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx package required for weather server")

    days = min(days, 3)

    unit_param = "M" if units == "metric" else "u" if units == "imperial" else "M"
    url = f"https://wttr.in/{quote(location)}?format=j1&{unit_param}"

    async with httpx.AsyncClient(verify=ssl_verify, timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()

        current = data.get("current_condition", [{}])[0]
        weather_data = cast(list[dict[str, Any]], data.get("weather", [])[:days])

        result: dict[str, Any] = {
            "location": location,
            "source": "wttr.in",
            "units": "metric" if unit_param == "M" else "imperial",
            "current": {
                "temperature": current.get("temp_C" if unit_param == "M" else "temp_F"),
                "feels_like": current.get("FeelsLikeC" if unit_param == "M" else "FeelsLikeF"),
                "humidity": current.get("humidity"),
                "wind_speed": current.get("windspeedKmph" if unit_param == "M" else "windspeedMiles"),
                "wind_direction": current.get("winddir16Point"),
                "pressure": current.get("pressure"),
                "visibility": current.get("visibility"),
                "weather_desc": current.get("weatherDesc", [{}])[0].get("value"),
                "observation_time": current.get("observation_time"),
            },
            "forecast": [],
        }

        for day_data in weather_data:
            hours = cast(list[dict[str, Any]], day_data.get("hourly", []))
            # The day level has no wind, humidity, rain or condition: they
            # are taken from the day's three-hour steps.
            winds = _hour_ints(hours, "windspeedKmph" if unit_param == "M" else "windspeedMiles")
            humidities = _hour_ints(hours, "humidity")
            rain_chances = _hour_ints(hours, "chanceofrain")
            descriptions: list[str] = []
            for hour_data in hours:
                text = ((hour_data.get("weatherDesc") or [{}])[0].get("value") or "").strip()
                if text and text not in descriptions:
                    descriptions.append(text)
            day_info: dict[str, Any] = {
                "date": day_data.get("date"),
                "max_temp": day_data.get("maxtempC" if unit_param == "M" else "maxtempF"),
                "min_temp": day_data.get("mintempC" if unit_param == "M" else "mintempF"),
                "avg_temp": day_data.get("avgtempC" if unit_param == "M" else "avgtempF"),
                "wind_speed": max(winds) if winds else None,
                "humidity": round(sum(humidities) / len(humidities)) if humidities else None,
                "chance_of_rain": max(rain_chances) if rain_chances else None,
                "weather_desc": descriptions,
            }

            # The three-hour steps cost tokens: only with summary_format hourly.
            hourly_list = hours if hourly else []
            if hourly:
                day_info["hours"] = []
            for hour_data in hourly_list:
                hour_info = {
                    "time": hour_data.get("time"),
                    "temperature": hour_data.get("tempC" if unit_param == "M" else "tempF"),
                    "feels_like": hour_data.get("FeelsLikeC" if unit_param == "M" else "FeelsLikeF"),
                    "wind_speed": hour_data.get("windspeedKmph" if unit_param == "M" else "windspeedMiles"),
                    "humidity": hour_data.get("humidity"),
                    "pressure": hour_data.get("pressure"),
                    "weather_desc": hour_data.get("weatherDesc", [{}])[0].get("value"),
                    "chance_of_rain": hour_data.get("chanceofrain"),
                }
                day_info["hours"].append(hour_info)

            result["forecast"].append(day_info)

        return result


async def fetch_weather_gov(location: str, days: int, units: str, ssl_verify: bool) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx package required for weather server")

    geocode_url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    geocode_params: dict[str, str] = {"address": location, "benchmark": "2020", "format": "json"}

    # follow_redirects: api.weather.gov answers 301 for coordinates with more
    # than four decimals, which the geocoder always returns.
    async with httpx.AsyncClient(verify=ssl_verify, timeout=30.0, follow_redirects=True) as client:
        geocode_response = await client.get(geocode_url, params=geocode_params)
        geocode_response.raise_for_status()
        geocode_data = geocode_response.json()

        matches = geocode_data.get("result", {}).get("addressMatches", [])
        if not matches:
            raise ValueError(f"Could not find coordinates for US location: {location}")

        coordinates = matches[0]["coordinates"]
        lat, lon = coordinates["y"], coordinates["x"]

        weather_url = f"https://api.weather.gov/points/{lat},{lon}"
        weather_response = await client.get(weather_url)
        weather_response.raise_for_status()
        weather_data = weather_response.json()

        forecast_url = weather_data["properties"]["forecast"]
        forecast_response = await client.get(forecast_url)
        forecast_response.raise_for_status()
        forecast_data = forecast_response.json()

        periods = forecast_data["properties"]["periods"][: days * 2]

        result: dict[str, Any] = {
            "location": location,
            "source": "weather.gov",
            "units": "imperial",
            "coordinates": {"lat": lat, "lon": lon},
            "forecast": [],
        }

        day_periods: list[dict[str, Any]] = []
        for day_period, night_period in _day_night_pairs(periods):
            day_info: dict[str, Any] = {"date": _period_date(day_period, night_period)}
            if day_period:
                day_info["day"] = _nws_period(day_period)
            if night_period:
                day_info["night"] = _nws_period(night_period)
            day_periods.append(day_info)

        result["forecast"] = day_periods
        return result


async def fetch_marine_weather_gov(location: str, days: int, units: str, ssl_verify: bool, include_marine: bool = True) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx package required for weather server")

    geocode_url = "https://nominatim.openstreetmap.org/search"
    geocode_params: dict[str, str | int] = {"q": location, "format": "json", "limit": 1}

    # follow_redirects: api.weather.gov answers 301 for coordinates with more
    # than four decimals, which the geocoder always returns.
    async with httpx.AsyncClient(verify=ssl_verify, timeout=30.0, follow_redirects=True, headers=_HEADERS) as client:
        geocode_response = await client.get(geocode_url, params=geocode_params)
        geocode_response.raise_for_status()
        geocode_data = geocode_response.json()

        if not geocode_data:
            raise ValueError(f"Could not find coordinates for location: {location}")

        lat = float(geocode_data[0]["lat"])
        lon = float(geocode_data[0]["lon"])

        result: dict[str, Any] = {
            "location": location,
            "source": "marine.weather.gov",
            # The forecast temperatures are NOAA's, always Fahrenheit.
            "units": "imperial",
            "coordinates": {"lat": lat, "lon": lon},
            "current": {},
            "forecast": [],
            "marine_data": {
                "sea_surface_temperatures": [],
                "wave_heights": [],
                "wind_waves": [],
            }
            if include_marine
            else None,
        }

        try:
            weather_url = f"https://api.weather.gov/points/{lat},{lon}"
            weather_response = await client.get(weather_url)
            # Outside the US api.weather.gov answers 404; raising records it
            # in atmospheric_data_error instead of an empty forecast.
            weather_response.raise_for_status()
            weather_data = weather_response.json()
            forecast_url = weather_data["properties"]["forecast"]
            forecast_response = await client.get(forecast_url)
            forecast_response.raise_for_status()
            forecast_data = forecast_response.json()
            periods = forecast_data["properties"]["periods"][: days * 2]

            for day_period, night_period in _day_night_pairs(periods):
                first = day_period or night_period
                assert first is not None
                high = first["temperature"]
                low = (night_period or first)["temperature"]
                result["forecast"].append({
                    "date": _period_date(day_period, night_period),
                    "max_temp": high,
                    "min_temp": low,
                    "avg_temp": (high + low) / 2,
                    "wind_speed": first["windSpeed"],
                    "wind_direction": first["windDirection"],
                    "weather_desc": first["shortForecast"],
                    "detailed_forecast": first["detailedForecast"],
                })
        except Exception as e:
            result["atmospheric_data_error"] = f"NOAA weather API failed: {str(e)}"

        if include_marine:
            try:
                current_date = datetime.date.today()
                # Narrow marine_data to a concrete dict so mypy knows it's not None
                marine_data = cast(dict[str, Any], result["marine_data"])
                for day in range(days):
                    forecast_date = current_date + datetime.timedelta(days=day)
                    seasonal_adjustment = 2 * (datetime.datetime.now().month - 6) / 6
                    latitude_adjustment = (90 - abs(lat)) / 3
                    estimated_sst = 15 + latitude_adjustment + seasonal_adjustment

                    marine_data["sea_surface_temperatures"].append(
                        {
                            "date": forecast_date.isoformat(),
                            "temperature": round(estimated_sst, 1),
                            "units": "Celsius",
                            "source": "estimated",
                            "note": "Estimated SST - upgrade to real OISST/Copernicus API for production",
                        }
                    )

                    estimated_wave_height = max(0.5, min(4.0, abs(lat) / 20 + 0.5))
                    marine_data["wave_heights"].append(
                        {
                            "date": forecast_date.isoformat(),
                            "significant_wave_height": round(estimated_wave_height, 1),
                            "units": "meters",
                            "source": "estimated",
                        }
                    )
            except Exception as e:
                result["marine_data"]["error"] = f"Marine data fetch failed: {str(e)}"

        return result


async def fetch_met_no(location: str, days: int, units: str, ssl_verify: bool,
                       hourly: bool = False) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx package required for weather server")

    geocode_url = "https://nominatim.openstreetmap.org/search"
    geocode_params: dict[str, str | int] = {"q": location, "format": "json", "limit": 1}

    async with httpx.AsyncClient(verify=ssl_verify, timeout=30.0, headers=_HEADERS) as client:
        geocode_response = await client.get(geocode_url, params=geocode_params)
        geocode_response.raise_for_status()
        geocode_data = geocode_response.json()

        if not geocode_data:
            raise ValueError(f"Could not find coordinates for location: {location}")

        lat = float(geocode_data[0]["lat"])
        lon = float(geocode_data[0]["lon"])

        # complete, not compact: only it has each six-hour slot's minimum and
        # maximum temperature; the six-hourly days have just four instants.
        weather_url = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
        weather_params: dict[str, float] = {"lat": lat, "lon": lon}

        weather_response = await client.get(weather_url, params=weather_params)
        weather_response.raise_for_status()
        weather_data = weather_response.json()

        timeseries = cast(list[dict[str, Any]], weather_data["properties"]["timeseries"])

        result: dict[str, Any] = {
            "location": location,
            "source": "met.no",
            "units": "metric",
            "coordinates": {"lat": lat, "lon": lon},
            "current": None,
            "forecast": [],
        }

        if timeseries:
            current_data = timeseries[0]
            instant_data = current_data["data"]["instant"]["details"]
            result["current"] = {
                "time": current_data["time"],
                "temperature": instant_data.get("air_temperature"),
                "humidity": instant_data.get("relative_humidity"),
                "pressure": instant_data.get("air_pressure_at_sea_level"),
                "wind_speed": instant_data.get("wind_speed"),
                "wind_direction": instant_data.get("wind_from_direction"),
            }

        # The series is hourly for about two and a half days, six-hourly after
        # that: count dates, not entries. Dates are local, approximated from
        # the longitude, so a forecast made near midnight UTC does not start
        # with a day of one entry.
        offset = datetime.timedelta(hours=round(lon / 15))
        daily_forecasts: dict[str, dict[str, Any]] = {}
        aligned_seen = False
        for entry in timeseries:
            time = datetime.datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
            date = (time + offset).date().isoformat()
            aligned = time.hour % 6 == 0 and time.minute == 0
            aligned_seen = aligned_seen or aligned
            if date not in daily_forecasts:
                if len(daily_forecasts) >= days:
                    break
                daily_forecasts[date] = _new_met_day()
            day = daily_forecasts[date]
            data = entry["data"]

            instant = data["instant"]["details"]
            entry_data: dict[str, Any] = {
                "time": entry["time"],
                "temperature": instant.get("air_temperature"),
                "humidity": instant.get("relative_humidity"),
                "pressure": instant.get("air_pressure_at_sea_level"),
                "wind_speed": instant.get("wind_speed"),
                "wind_direction": instant.get("wind_from_direction"),
            }

            next_6_hours = data.get("next_6_hours")
            if next_6_hours:
                amount = next_6_hours["details"].get("precipitation_amount")
                symbol = next_6_hours["summary"].get("symbol_code")
                entry_data["precipitation"] = amount
                entry_data["weather_symbol"] = symbol
                # The six-hour windows overlap between hourly steps; the ones
                # starting at 00/06/12/18Z tile the day.
                # A slot is booked to the local day of its midpoint: by its
                # start it could run up to five hours into the next day.
                slot_date = (time + offset + datetime.timedelta(hours=3)).date().isoformat()
                if aligned and slot_date not in daily_forecasts and len(daily_forecasts) < days:
                    daily_forecasts[slot_date] = _new_met_day()
                slot_day = daily_forecasts.get(slot_date)
                if aligned and slot_day is not None:
                    slot_day["precipitation"] += amount or 0.0
                    if symbol:
                        slot_day["symbols"].append(symbol)
                    for key in ("air_temperature_min", "air_temperature_max"):
                        extreme = next_6_hours["details"].get(key)
                        if extreme is not None:
                            slot_day["temperatures"].append(extreme)
            if not aligned_seen:
                # Before the first aligned slot each six-hour window reaches
                # into that slot: count these hours one by one instead.
                next_1_hours = data.get("next_1_hours")
                if next_1_hours:
                    day["precipitation"] += next_1_hours["details"].get("precipitation_amount") or 0.0

            if hourly:
                day["entries"].append(entry_data)
            if instant.get("air_temperature") is not None:
                day["temperatures"].append(instant["air_temperature"])
            if instant.get("wind_speed") is not None:
                day["winds"].append(instant["wind_speed"])

        for date, day in daily_forecasts.items():
            temps = day["temperatures"]
            if temps:
                forecast_day: dict[str, Any] = {
                    "date": date,
                    "min_temp": min(temps),
                    "max_temp": max(temps),
                    "precipitation": round(day["precipitation"], 1),
                    "wind_max": max(day["winds"]) if day["winds"] else None,
                    "symbols": day["symbols"],
                }
                if hourly:
                    forecast_day["hourly"] = day["entries"]
                result["forecast"].append(forecast_day)

        return result
