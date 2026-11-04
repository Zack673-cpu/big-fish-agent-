"""FastAPI 入口：接收 NapCat 反向 WS，交给 Engine 处理。"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .config import load_settings
from .core.engine import Engine
from .core.link import LinkReader
from .core.llm import LLMClient
from .core.memory import MemoryStore
from .core.proactive import ProactiveMessenger
from .core.search import WebSearchClient
from .core.voice import VoiceService
from .core.weather import WeatherClient
from .logging_setup import setup_logging
from .onebot.client import OneBotConnection
from .onebot.events import parse_event

settings = load_settings()
setup_logging(settings.log_level)
logger = logging.getLogger(__name__)

_background_tasks: Set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    memory = MemoryStore(settings.data_dir / "bot.db")
    await memory.initialize()
    llm = LLMClient(
        settings.llm_api_key,
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_timeout,
    )
    search = None
    if settings.web_search_enabled:
        search = WebSearchClient(
            settings.llm_api_key,
            settings.llm_base_url,
            settings.web_search_count,
            settings.web_search_timeout,
        )
    weather = None
    if settings.weather_enabled:
        weather = WeatherClient(settings.weather_timeout)
    links = None
    if settings.link_parse_enabled:
        links = LinkReader(settings.link_parse_timeout, settings.link_parse_max_links)
    voice = None
    if settings.voice_asr_enabled or settings.voice_tts_enabled:
        voice = VoiceService(
            api_key=settings.voice_asr_key,
            base_url=settings.voice_asr_base_url,
            model=settings.voice_asr_model,
            asr_timeout=settings.voice_asr_timeout,
            tts_voice=settings.voice_tts_voice,
            tts_timeout=settings.voice_tts_timeout,
            data_dir=settings.data_dir,
            asr_enabled=settings.voice_asr_enabled,
            tts_enabled=settings.voice_tts_enabled,
        )
    app.state.engine = Engine(settings, memory, llm, search, links, voice, weather=weather)
    await app.state.engine.start()
    if settings.embedding_enabled and settings.embedding_api_key:
        logger.info(
            "知识库向量检索已启用（百炼 %s，%s 维）",
            settings.embedding_model,
            settings.embedding_dimensions,
        )
    elif settings.embedding_enabled:
        logger.info("知识库向量检索未启用（未配置 DASHSCOPE_API_KEY），使用字面检索")
    else:
        logger.info("知识库向量检索已关闭（EMBEDDING_ENABLED=false），使用字面检索")
    app.state.conn = None
    app.state.conn_count = 0
    app.state.conn_started = 0.0

    if not settings.llm_api_key:
        logger.error("未配置 DEEPSEEK_API_KEY，机器人暂时无法回复，请检查 .env")
    if not settings.onebot_token:
        logger.warning("ONEBOT_TOKEN 为空，WebSocket 未校验 token，建议只在 127.0.0.1 上使用")
    if search is not None:
        logger.info("联网搜索已启用（DeepSeek Search API，复用 DEEPSEEK_API_KEY）")
    else:
        logger.info("联网搜索已关闭（WEB_SEARCH_ENABLED=false）")
    if weather is not None:
        logger.info("天气查询已启用（Open-Meteo，无需 API Key）")
    else:
        logger.info("天气查询已关闭（WEATHER_ENABLED=false）")
    if links is not None:
        logger.info(
            "链接解析已启用（B站视频 + 普通网页，每条消息最多 %s 个链接）",
            settings.link_parse_max_links,
        )
    else:
        logger.info("链接解析已关闭（LINK_PARSE_ENABLED=false）")
    if voice is not None and voice.tts_enabled:
        logger.info("语音回复已启用（Edge TTS，音色 %s）", settings.voice_tts_voice)
    else:
        logger.info("语音回复已关闭（VOICE_TTS_ENABLED=false）")
    if voice is not None and voice.asr_enabled:
        logger.info("语音识别已启用（硅基流动 %s）", settings.voice_asr_model)
    elif settings.voice_asr_enabled:
        logger.warning("语音识别未配置 SILICONFLOW_API_KEY，收到的语音只会提示“[语音]”")
    else:
        logger.info("语音识别已关闭（VOICE_ASR_ENABLED=false）")
    if settings.proactive_enabled and settings.admin_qq > 0:
        messenger = ProactiveMessenger(settings, llm, memory, lambda: app.state.conn)
        app.state.engine.set_activity_listener(messenger.note_activity)
        task = asyncio.create_task(messenger.run(), name="proactive-messages")
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        logger.info(
            "主动消息已启用（每 %s–%s 分钟随机私聊 QQ %s，时段 %02d:00–%02d:00，号主互动后重新计时）",
            settings.proactive_interval_minutes,
            settings.proactive_interval_max_minutes,
            settings.admin_qq,
            settings.proactive_start_hour,
            settings.proactive_end_hour,
        )
    elif settings.proactive_enabled:
        logger.warning("主动消息已启用，但 ADMIN_QQ 未配置，不会发送")
    else:
        logger.info("主动消息已关闭（PROACTIVE_ENABLED=false）")
    logger.info(
        "大肥鱼已启动，等待 NapCat 反向 WS 连接：ws://%s:%s%s",
        settings.onebot_host,
        settings.onebot_port,
        settings.onebot_ws_path,
    )
    yield
    for task in list(_background_tasks):
        task.cancel()
    await app.state.engine.aclose()
    if search is not None:
        await search.aclose()
    if weather is not None:
        await weather.aclose()
    if links is not None:
        await links.aclose()
    if voice is not None:
        await voice.aclose()
    await llm.aclose()


app = FastAPI(title="大肥鱼 QQ Bot", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "bot": settings.bot_name}


def _token_ok(websocket: WebSocket, token: str) -> bool:
    if not token:
        return True
    header = websocket.headers.get("authorization", "")
    if header.startswith("Bearer "):
        header = header[len("Bearer "):]
    if header.strip() == token:
        return True
    return (websocket.query_params.get("access_token") or "") == token


def _log_request(payload: dict) -> None:
    """好友/入群请求只记日志，绝不自动同意，统一由号主在 QQ 客户端手动确认。"""
    request_type = payload.get("request_type")
    if request_type == "friend":
        logger.info("收到好友请求（不会自动通过）：QQ %s，请在 QQ 客户端手动确认", payload.get("user_id"))
        return
    if request_type == "group":
        action = "邀请机器人入群" if payload.get("sub_type") == "invite" else "申请加群"
        logger.info(
            "收到群请求（不会自动通过）：群 %s，QQ %s，%s，请在 QQ 客户端手动确认",
            payload.get("group_id"),
            payload.get("user_id"),
            action,
        )


def _is_poke(payload: dict) -> bool:
    notice_type = payload.get("notice_type")
    return notice_type == "poke" or (
        notice_type == "notify" and payload.get("sub_type") == "poke"
    )


async def _dispatch(engine: Engine, event, conn: OneBotConnection) -> None:
    try:
        await engine.handle(event, conn)
    except Exception:
        logger.exception("处理消息失败：%s", event.chat_key)


async def _dispatch_poke(engine: Engine, payload: dict, conn: OneBotConnection) -> None:
    try:
        await engine.handle_poke(payload, conn)
    except Exception:
        logger.exception("处理戳一戳失败")


@app.websocket(settings.onebot_ws_path)
async def onebot_ws(websocket: WebSocket) -> None:
    if not _token_ok(websocket, settings.onebot_token):
        logger.warning("拒绝了 token 不匹配的 WebSocket 连接")
        await websocket.close(code=1008)
        return
    await websocket.accept()

    started = time.monotonic()
    conn = OneBotConnection(websocket)
    previous: Optional[OneBotConnection] = app.state.conn
    if previous is not None:
        logger.warning(
            "收到新的 NapCat 连接，旧连接已持续 %.0f 秒，将其关闭",
            started - float(app.state.conn_started or started),
        )
        try:
            await previous.close()
        except Exception:
            pass
    app.state.conn = conn
    app.state.conn_started = started
    app.state.conn_count = int(app.state.conn_count or 0) + 1
    logger.info("NapCat 已连接（第 %s 次）", app.state.conn_count)

    try:
        while True:
            payload = await websocket.receive_json()
            if not isinstance(payload, dict):
                continue
            event_payload = await conn.resolve(payload)
            if event_payload is None:
                continue
            post_type = event_payload.get("post_type")
            if post_type == "request":
                _log_request(event_payload)
                continue
            if post_type == "notice":
                if _is_poke(event_payload):
                    task = asyncio.create_task(
                        _dispatch_poke(app.state.engine, event_payload, conn)
                    )
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                continue
            if post_type != "message":
                continue
            event = parse_event(event_payload)
            if event is None:
                continue
            task = asyncio.create_task(_dispatch(app.state.engine, event, conn))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
    except WebSocketDisconnect as exc:
        logger.info(
            "NapCat 连接断开（code=%s，reason=%s，持续 %.0f 秒）",
            exc.code,
            exc.reason or "-",
            time.monotonic() - started,
        )
    except Exception as exc:
        logger.warning(
            "WebSocket 处理异常（持续 %.0f 秒）：%s", time.monotonic() - started, exc
        )
    finally:
        conn.close_pending("连接结束")
        if app.state.conn is conn:
            app.state.conn = None
