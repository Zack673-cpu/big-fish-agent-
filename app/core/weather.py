"""天气查询：Open-Meteo 免费接口，无需 API Key，返回未来几天结构化预报。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO 天气代码，Open-Meteo 用这一套编码
_WEATHER_CODES: Dict[int, str] = {
    0: "晴",
    1: "大部晴朗",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴冰雹",
    99: "强雷暴伴冰雹",
}

_DATE_LABELS = {0: "今天", 1: "明天", 2: "后天"}


def weather_text(code: int) -> str:
    """把 WMO 天气代码转成中文描述。"""
    return _WEATHER_CODES.get(code, "未知天气")


@dataclass(frozen=True)
class DailyWeather:
    day: date
    code: int
    temp_max: float
    temp_min: float
    rain_probability: int | None

    @property
    def description(self) -> str:
        return weather_text(self.code)


@dataclass(frozen=True)
class WeatherReport:
    city: str
    province: str
    days: List[DailyWeather]

    @property
    def location(self) -> str:
        province = self.province.strip().rstrip("省")
        return f"{self.city}（{province}）" if province else self.city

    def format_context(self, today: date | None = None) -> str:
        """转成给模型看的天气上下文，日期和数值都以这里为准。"""
        if today is None:
            today = datetime.now(timezone(timedelta(hours=8))).date()
        lines = [f"【实时天气数据（Open-Meteo 城市级预报，{self.location}，北京时间）】"]
        for item in self.days:
            label = _DATE_LABELS.get((item.day - today).days, "")
            prefix = f"{label}（{item.day.isoformat()}）" if label else item.day.isoformat()
            rain = (
                f"，降水概率 {item.rain_probability}%"
                if item.rain_probability is not None
                else ""
            )
            lines.append(
                f"- {prefix}：{item.description}，"
                f"{round(item.temp_min)}~{round(item.temp_max)}℃{rain}"
            )
        lines.append(
            "说明：以上是城市级数值天气预报，回答时日期和数值以此为准，"
            "不要用网页搜索的旧预报覆盖；也不要把城市数据说成某个区、湖泊或景点的精确天气。"
        )
        return "\n".join(lines)


def parse_forecast(
    data: Dict[str, Any], city: str, province: str, days: int = 3
) -> WeatherReport | None:
    """解析 Open-Meteo daily 数据；字段缺失时返回 None。"""
    daily = data.get("daily") if isinstance(data, dict) else None
    if not isinstance(daily, dict):
        return None
    times = daily.get("time") or []
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rains = daily.get("precipitation_probability_max") or []
    result: List[DailyWeather] = []
    for index in range(min(len(times), days)):
        try:
            day = date.fromisoformat(str(times[index]))
            high = float(highs[index])
            low = float(lows[index])
            code = int(codes[index])
        except (IndexError, TypeError, ValueError):
            continue
        rain = None
        try:
            if index < len(rains) and rains[index] is not None:
                rain = int(rains[index])
        except (TypeError, ValueError):
            rain = None
        result.append(DailyWeather(day, code, high, low, rain))
    if not result:
        return None
    return WeatherReport(city=city, province=province, days=result)


class WeatherClient:
    """调用 Open-Meteo：先地理编码，再取未来 3 天预报。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "bigfish-bot/1.0"},
        )

    async def forecast(self, city: str, days: int = 3) -> WeatherReport | None:
        city = (city or "").strip()[:40]
        if not city:
            return None
        try:
            place = await self._geocode(city)
            if place is None:
                logger.info("天气查询：没有找到城市 %s", city)
                return None
            name = str(place.get("name") or city)
            province = str(place.get("admin1") or "")
            latitude = place.get("latitude")
            longitude = place.get("longitude")
            if latitude is None or longitude is None:
                logger.warning("天气查询：城市 %s 缺少经纬度", city)
                return None
            params = {
                "latitude": latitude,
                "longitude": longitude,
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max"
                ),
                "timezone": "Asia/Shanghai",
                "forecast_days": max(1, min(7, days)),
            }
            response = await self._client.get(_FORECAST_URL, params=params)
            response.raise_for_status()
            return parse_forecast(response.json(), name, province, days)
        except httpx.HTTPError as exc:
            logger.warning("天气查询失败（%s）：%s", city, exc)
            return None
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("天气接口返回格式异常（%s）：%s", city, exc)
            return None

    async def _geocode(self, city: str) -> Dict[str, Any] | None:
        params = {"name": city, "count": 1, "language": "zh", "format": "json"}
        response = await self._client.get(_GEOCODING_URL, params=params)
        response.raise_for_status()
        data = response.json()
        results = data.get("results") if isinstance(data, dict) else None
        if not results or not isinstance(results[0], dict):
            return None
        return results[0]

    async def aclose(self) -> None:
        await self._client.aclose()
