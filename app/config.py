"""配置加载：从 .env 和环境变量读取，集中成 Settings 对象。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """机器人运行配置。所有字段都有默认值，方便测试直接构造。"""

    bot_name: str = "大肥鱼"
    aliases: Tuple[str, ...] = ("大肥鱼", "肥鱼")
    admin_qq: int = 0

    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_timeout: float = 60.0

    web_search_enabled: bool = True
    web_search_count: int = 3
    web_search_timeout: float = 15.0

    weather_enabled: bool = True
    weather_timeout: float = 10.0

    link_parse_enabled: bool = True
    link_parse_timeout: float = 10.0
    link_parse_max_links: int = 2

    voice_asr_enabled: bool = True
    voice_asr_key: str = ""
    voice_asr_base_url: str = "https://api.siliconflow.cn/v1"
    voice_asr_model: str = "FunAudioLLM/SenseVoiceSmall"
    voice_asr_timeout: float = 30.0
    voice_tts_enabled: bool = True
    voice_tts_voice: str = "zh-CN-XiaoyiNeural"
    voice_tts_timeout: float = 15.0
    voice_tts_max_chars: int = 80

    red_packet_enabled: bool = True
    red_packet_delay_min: float = 1.0
    red_packet_delay_max: float = 5.0
    red_packet_cooldown: float = 30.0

    proactive_enabled: bool = True
    proactive_interval_minutes: int = 20
    proactive_interval_max_minutes: int = 120
    proactive_start_hour: int = 8
    proactive_end_hour: int = 23
    proactive_utc_offset: float = 8.0

    onebot_host: str = "127.0.0.1"
    onebot_port: int = 8080
    onebot_path: str = "/onebot/v11/ws"
    onebot_token: str = ""

    max_reply_chars: int = 60
    max_reply_segments: int = 3
    segment_delay_min: float = 3.0
    segment_delay_max: float = 5.0
    context_messages: int = 20
    summary_trigger: int = 40

    rate_min_interval: float = 0.0
    rate_per_minute: int = 60
    group_reply_probability: float = 1.0
    group_cooldown: float = 0.0

    knowledge_path: Path = BASE_DIR / "knowledge"
    knowledge_index_dir: Path = BASE_DIR / "data" / "knowledge_index"
    knowledge_top_k: int = 2
    embedding_enabled: bool = True
    embedding_api_key: str = ""
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    embedding_timeout: float = 20.0
    embedding_min_score: float = 0.40

    quote_reply: bool = True
    data_dir: Path = BASE_DIR / "data"
    log_level: str = "INFO"

    @property
    def onebot_ws_path(self) -> str:
        """返回以 / 开头的 WebSocket 路径。"""
        path = self.onebot_path or "/onebot/v11/ws"
        return path if path.startswith("/") else "/" + path


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数，当前值：{raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是数字，当前值：{raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"环境变量 {name} 必须是 true/false，当前值：{raw!r}")


def _env_aliases(default: Tuple[str, ...]) -> Tuple[str, ...]:
    raw = _env("BOT_ALIASES")
    if not raw:
        return default
    items = tuple(part.strip() for part in raw.replace("，", ",").split(",") if part.strip())
    return items or default


def _env_path(name: str, default: Path) -> Path:
    raw = _env(name)
    path = Path(raw) if raw else default
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def load_settings() -> Settings:
    """读取 .env（不覆盖已有环境变量），返回配置对象。"""
    load_dotenv(BASE_DIR / ".env", override=False)
    defaults = Settings()
    data_dir = _env_path("DATA_DIR", defaults.data_dir)
    return Settings(
        bot_name=_env("BOT_NAME") or defaults.bot_name,
        aliases=_env_aliases(defaults.aliases),
        admin_qq=_env_int("ADMIN_QQ", defaults.admin_qq),
        llm_api_key=_env("DEEPSEEK_API_KEY"),
        llm_base_url=_env("LLM_BASE_URL") or defaults.llm_base_url,
        llm_model=_env("LLM_MODEL") or defaults.llm_model,
        llm_timeout=_env_float("LLM_TIMEOUT", defaults.llm_timeout),
        web_search_enabled=_env_bool("WEB_SEARCH_ENABLED", defaults.web_search_enabled),
        web_search_count=_env_int("WEB_SEARCH_COUNT", defaults.web_search_count),
        web_search_timeout=_env_float("WEB_SEARCH_TIMEOUT", defaults.web_search_timeout),
        weather_enabled=_env_bool("WEATHER_ENABLED", defaults.weather_enabled),
        weather_timeout=_env_float("WEATHER_TIMEOUT", defaults.weather_timeout),
        link_parse_enabled=_env_bool("LINK_PARSE_ENABLED", defaults.link_parse_enabled),
        link_parse_timeout=_env_float("LINK_PARSE_TIMEOUT", defaults.link_parse_timeout),
        link_parse_max_links=_env_int("LINK_PARSE_MAX_LINKS", defaults.link_parse_max_links),
        voice_asr_enabled=_env_bool("VOICE_ASR_ENABLED", defaults.voice_asr_enabled),
        voice_asr_key=_env("SILICONFLOW_API_KEY"),
        voice_asr_base_url=_env("VOICE_ASR_BASE_URL") or defaults.voice_asr_base_url,
        voice_asr_model=_env("VOICE_ASR_MODEL") or defaults.voice_asr_model,
        voice_asr_timeout=_env_float("VOICE_ASR_TIMEOUT", defaults.voice_asr_timeout),
        voice_tts_enabled=_env_bool("VOICE_TTS_ENABLED", defaults.voice_tts_enabled),
        voice_tts_voice=_env("VOICE_TTS_VOICE") or defaults.voice_tts_voice,
        voice_tts_timeout=_env_float("VOICE_TTS_TIMEOUT", defaults.voice_tts_timeout),
        voice_tts_max_chars=_env_int("VOICE_TTS_MAX_CHARS", defaults.voice_tts_max_chars),
        red_packet_enabled=_env_bool("RED_PACKET_ENABLED", defaults.red_packet_enabled),
        red_packet_delay_min=_env_float("RED_PACKET_DELAY_MIN", defaults.red_packet_delay_min),
        red_packet_delay_max=_env_float("RED_PACKET_DELAY_MAX", defaults.red_packet_delay_max),
        red_packet_cooldown=_env_float("RED_PACKET_COOLDOWN", defaults.red_packet_cooldown),
        proactive_enabled=_env_bool("PROACTIVE_ENABLED", defaults.proactive_enabled),
        proactive_interval_minutes=_env_int(
            "PROACTIVE_INTERVAL_MINUTES", defaults.proactive_interval_minutes
        ),
        proactive_interval_max_minutes=_env_int(
            "PROACTIVE_INTERVAL_MAX_MINUTES", defaults.proactive_interval_max_minutes
        ),
        proactive_start_hour=_env_int("PROACTIVE_START_HOUR", defaults.proactive_start_hour),
        proactive_end_hour=_env_int("PROACTIVE_END_HOUR", defaults.proactive_end_hour),
        proactive_utc_offset=_env_float("PROACTIVE_UTC_OFFSET", defaults.proactive_utc_offset),
        onebot_host=_env("ONEBOT_HOST") or defaults.onebot_host,
        onebot_port=_env_int("ONEBOT_PORT", defaults.onebot_port),
        onebot_path=_env("ONEBOT_PATH") or defaults.onebot_path,
        onebot_token=_env("ONEBOT_TOKEN"),
        max_reply_chars=_env_int("MAX_REPLY_CHARS", defaults.max_reply_chars),
        max_reply_segments=_env_int("MAX_REPLY_SEGMENTS", defaults.max_reply_segments),
        segment_delay_min=_env_float("SEGMENT_DELAY_MIN", defaults.segment_delay_min),
        segment_delay_max=_env_float("SEGMENT_DELAY_MAX", defaults.segment_delay_max),
        context_messages=_env_int("CONTEXT_MESSAGES", defaults.context_messages),
        summary_trigger=_env_int("SUMMARY_TRIGGER", defaults.summary_trigger),
        rate_min_interval=_env_float("RATE_MIN_INTERVAL", defaults.rate_min_interval),
        rate_per_minute=_env_int("RATE_PER_MINUTE", defaults.rate_per_minute),
        group_reply_probability=_env_float("GROUP_REPLY_PROBABILITY", defaults.group_reply_probability),
        group_cooldown=_env_float("GROUP_COOLDOWN", defaults.group_cooldown),
        knowledge_path=_env_path(
            "KNOWLEDGE_PATH",
            _env_path("KNOWLEDGE_FILE", defaults.knowledge_path),
        ),
        knowledge_index_dir=_env_path(
            "KNOWLEDGE_INDEX_DIR",
            data_dir / "knowledge_index",
        ),
        knowledge_top_k=_env_int("KNOWLEDGE_TOP_K", defaults.knowledge_top_k),
        embedding_enabled=_env_bool("EMBEDDING_ENABLED", defaults.embedding_enabled),
        embedding_api_key=_env("DASHSCOPE_API_KEY"),
        embedding_base_url=_env("EMBEDDING_BASE_URL") or defaults.embedding_base_url,
        embedding_model=_env("EMBEDDING_MODEL") or defaults.embedding_model,
        embedding_dimensions=_env_int("EMBEDDING_DIMENSIONS", defaults.embedding_dimensions),
        embedding_timeout=_env_float("EMBEDDING_TIMEOUT", defaults.embedding_timeout),
        embedding_min_score=_env_float("EMBEDDING_MIN_SCORE", defaults.embedding_min_score),
        quote_reply=_env_bool("QUOTE_REPLY", defaults.quote_reply),
        data_dir=data_dir,
        log_level=(_env("LOG_LEVEL") or defaults.log_level).upper(),
    )
