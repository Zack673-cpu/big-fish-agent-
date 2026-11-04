"""天气模块：解析、格式化和工具分发。"""

import asyncio
from datetime import date

from app.config import Settings
from app.core.engine import Engine
from app.core.persona import build_system_prompt
from app.core.weather import parse_forecast, weather_text


def _payload() -> dict:
    return {
        "daily": {
            "time": ["2026-11-04", "2026-11-05", "2026-11-06"],
            "weather_code": [61, 3, 0],
            "temperature_2m_max": [18.2, 22.6, 25.1],
            "temperature_2m_min": [13.9, 15.2, 16.8],
            "precipitation_probability_max": [100, 20, 0],
        }
    }


def test_weather_text() -> None:
    assert weather_text(61) == "小雨"
    assert weather_text(0) == "晴"
    assert weather_text(999) == "未知天气"


def test_parse_forecast_and_format() -> None:
    report = parse_forecast(_payload(), "武汉", "湖北省")
    assert report is not None
    assert report.location == "武汉（湖北）"
    text = report.format_context(date(2026, 11, 4))
    assert "今天（2026-11-04）：小雨，14~18℃，降水概率 100%" in text
    assert "明天（2026-11-05）：阴，15~23℃，降水概率 20%" in text
    assert "后天（2026-11-06）：晴，17~25℃，降水概率 0%" in text


def test_parse_forecast_rejects_broken_payload() -> None:
    assert parse_forecast({}, "武汉", "湖北省") is None
    broken = {"daily": {"time": ["2026-11-04"], "weather_code": [1]}}
    assert parse_forecast(broken, "武汉", "湖北省") is None


class _FakeWeather:
    def __init__(self, report: object) -> None:
        self._report = report
        self.cities: list[str] = []

    async def forecast(self, city: str):
        self.cities.append(city)
        return self._report


def test_system_prompt_has_weather_guide_and_time() -> None:
    prompt = build_system_prompt(
        bot_name="大肥鱼",
        max_chars=200,
        is_group=False,
        summary="",
        memories=[],
        weather_tool=True,
    )
    assert "get_weather" in prompt
    assert "当前时间：" in prompt
    assert "城市级" in prompt


def test_engine_dispatches_weather_tool() -> None:
    report = parse_forecast(_payload(), "武汉", "湖北省")
    weather = _FakeWeather(report)
    engine = Engine(Settings(), memory=None, llm=None, weather=weather)
    call = {
        "id": "call_weather",
        "function": {"name": "get_weather", "arguments": '{"city": "武汉"}'},
    }
    content = asyncio.run(engine._run_tool(call, 0))
    assert weather.cities == ["武汉"]
    assert "武汉（湖北）" in content
    assert "明天（2026-11-05）：阴，15~23℃" in content

    missing = _FakeWeather(None)
    engine = Engine(Settings(), memory=None, llm=None, weather=missing)
    content = asyncio.run(engine._run_tool(call, 0))
    assert "没有查到「武汉」的天气数据" in content
