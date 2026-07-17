"""Open-Meteo adapter — forecast backbone (and NWS fallback for forecasts)."""

import httpx

from app.adapters.base import Adapter, AdapterManifest
from app.adapters.weather_derive import derive_fields

HOURLY_VARS = ("temperature_2m,apparent_temperature,precipitation_probability,"
               "precipitation,snowfall,weather_code,cloud_cover,dew_point_2m,"
               "wind_speed_10m,wind_gusts_10m,uv_index,wind_direction_10m")
DAILY_VARS = ("temperature_2m_max,temperature_2m_min,snowfall_sum,"
              "precipitation_sum,precipitation_probability_max,weather_code")


class OpenMeteoAdapter(Adapter):
    manifest = AdapterManifest(
        name="open_meteo",
        version="1.0",
        entity_kind="location",
        fields=[  # published contract = derive_fields() output keys
            "wind_chill_min_morning", "heat_index_max", "freezing_precip_expected",
            "temp_min_today", "snow_24h_in", "rain_within_4h", "rain_start_label",
            "raining_now", "rain_ends_within_6h", "rain_end_label", "storms_expected",
            "storm_start_label", "pop_commute_max", "pop_today_max", "all_day_soaker",
            "is_weekday", "high_today", "low_tonight", "temp_swing_today",
            "high_delta_vs_yesterday", "temp_7am", "temp_evening", "temp_evening_min",
            "temp_evening_max", "dew_max_today", "dew_max_yesterday", "dew_evening_max",
            "dew_drop_vs_yesterday", "max_gust_today", "wind_overnight_max",
            "wind_evening_max", "cloud_overnight_max", "cloud_midday_min",
            "uv_index_max", "pop_evening_max", "freeze_snap_onset", "hot_streak_days",
            "consecutive_precip_days", "first_freeze_of_season", "first_warm_day_of_season",
        ],
        poll_seconds_fresh=1800,
        stale_after_seconds=7200,
        api_key_required=False,
        card_templates=["weather_meaning", "forecast_timeline"],
        registry_record={
            "source_name": "open_meteo",
            "source_url": "https://open-meteo.com",
            "license_type": "CC-BY 4.0 (data); free API for non-commercial use",
            "commercial_use_allowed": 0,   # AIDEV-CAUTION: requires paid plan for
                                           # commercial units — review before V1.2
                                           # (plan §5 source-registry action item)
            "redistribution_allowed": 1,
            "bulk_storage_allowed": 1,
            "cache_allowed": 1,
            "attribution_required": 1,
            "attribution_text": "Weather data by Open-Meteo.com",
            "api_key_required": 0,
            "rate_limit": "10,000 calls/day (free tier)",
            "terms_url": "https://open-meteo.com/en/terms",
            "last_reviewed_date": "2026-07-13",
            "notes": "Forecast fallback per plan §10; commercial licensing open item.",
        },
    )

    async def fetch(self, entity) -> dict:
        params = {
            "latitude": entity["latitude"], "longitude": entity["longitude"],
            "hourly": HOURLY_VARS, "daily": DAILY_VARS,
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "precipitation_unit": "inch", "timezone": "auto",
            "forecast_days": 8, "past_days": 7,   # 8 = today + 7-day outlook
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
            r.raise_for_status()
            return r.json()

    def normalize(self, raw: dict, day_iso: str = None, now_h: int = None,
                  season_state: dict = None) -> dict:
        from datetime import date, datetime
        return derive_fields(
            raw["hourly"], raw["daily"],
            day_iso or date.today().isoformat(),
            datetime.now().hour if now_h is None else now_h,
            season_state if season_state is not None else {},
        )
