"""语音能力：语音转文字（硅基流动 SenseVoice）与文字转语音（Edge TTS）。"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_MARKER_RE = re.compile(r"^(?:\s*(?:\[语音\]|【语音】))+\s*")
_MAX_AUDIO_BYTES = 10 * 1024 * 1024
_FFMPEG_TIMEOUT = 20.0
_TTS_CLEAN_RE = re.compile(r"[*_`#>|]")
_MIME = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "flac": "audio/flac",
}


class VoiceError(Exception):
    """语音处理失败。"""


def pop_voice_marker(text: str) -> Tuple[str, bool]:
    """取出回复开头的 [语音] 标记，返回（去掉标记的正文, 是否要发语音）。"""
    match = _MARKER_RE.match(text or "")
    if not match:
        return text, False
    return text[match.end():].strip(), True


def detect_audio_format(raw: bytes) -> str:
    """按文件头猜音频格式；认不出来返回空串。"""
    if not raw:
        return ""
    if raw.startswith(b"RIFF"):
        return "wav"
    if raw.startswith(b"ID3") or raw[:2] in {b"\xff\xf3", b"\xff\xf2", b"\xff\xfb"}:
        return "mp3"
    if raw.startswith(b"OggS"):
        return "ogg"
    if raw.startswith(b"fLaC"):
        return "flac"
    if raw.startswith(b"#!AMR"):
        return "amr"
    if b"#!SILK" in raw[:16]:
        return "silk"
    if b"ftyp" in raw[:16]:
        return "m4a"
    return ""


async def _ffmpeg_to_wav(raw: bytes) -> bytes:
    """用系统 ffmpeg 把任意音频转成 16k 单声道 wav。"""
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise VoiceError("服务器没有安装 ffmpeg") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(raw), timeout=_FFMPEG_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        raise VoiceError("ffmpeg 转码超时") from exc
    if process.returncode != 0 or not stdout:
        detail = (stderr or b"").decode("utf-8", "ignore")[:120]
        raise VoiceError(f"ffmpeg 转码失败：{detail}")
    return stdout


def _silk_to_wav_sync(raw: bytes) -> bytes:
    """用纯 Python 的 pilk 解码 QQ 的 silk 语音。"""
    try:
        import pilk
    except ImportError as exc:
        raise VoiceError("没有安装 pilk，无法解析 silk 语音") from exc
    with tempfile.TemporaryDirectory() as tmp:
        silk_path = Path(tmp) / "voice.silk"
        wav_path = Path(tmp) / "voice.wav"
        silk_path.write_bytes(raw)
        pilk.silk_to_wav(str(silk_path), str(wav_path), rate=16000)
        return wav_path.read_bytes()


async def _silk_to_wav(raw: bytes) -> bytes:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _silk_to_wav_sync, raw)


async def prepare_audio(raw: bytes) -> Tuple[bytes, str]:
    """把收到的语音整理成接口能接受的格式，返回（音频字节, 文件名后缀）。"""
    if not raw:
        raise VoiceError("语音内容为空")
    if len(raw) > _MAX_AUDIO_BYTES:
        raise VoiceError("语音文件超过 10MB")
    fmt = detect_audio_format(raw)
    if fmt == "wav":
        return raw, "wav"
    if fmt in {"mp3", "ogg", "m4a", "flac"}:
        return raw, fmt
    if fmt == "silk":
        return await _silk_to_wav(raw), "wav"
    # amr 以及认不出来的格式都交给 ffmpeg
    return await _ffmpeg_to_wav(raw), "wav"


def normalize_for_tts(text: str) -> str:
    """去掉标记和排版符号，避免念出奇怪的东西。"""
    text = _TTS_CLEAN_RE.sub("", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class VoiceService:
    """语音识别与语音合成。任何失败都只记日志，由上层退回文字。"""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = "FunAudioLLM/SenseVoiceSmall",
        asr_timeout: float = 30.0,
        tts_voice: str = "zh-CN-XiaoyiNeural",
        tts_timeout: float = 15.0,
        data_dir: Path | str = "data",
        asr_enabled: bool = True,
        tts_enabled: bool = True,
        client: Optional[httpx.AsyncClient] = None,
        synthesizer: Optional[Callable[[str], Awaitable[bytes]]] = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._model = model
        self._asr_timeout = max(5.0, asr_timeout)
        self._tts_voice = tts_voice
        self._tts_timeout = max(5.0, tts_timeout)
        self._audio_dir = Path(data_dir) / "voice"
        self._asr_enabled = asr_enabled and bool(self._api_key)
        self._tts_enabled = tts_enabled
        self._synthesizer = synthesizer
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=max(self._asr_timeout, self._tts_timeout),
        )
        self._cleanup_old_files()

    @property
    def asr_enabled(self) -> bool:
        return self._asr_enabled

    @property
    def tts_enabled(self) -> bool:
        return self._tts_enabled

    async def transcribe(self, attachment) -> str:
        """把一条语音附件转成文字；未启用或失败时返回空串。"""
        if not self._asr_enabled:
            return ""
        raw = await self._load_audio(str(getattr(attachment, "url", "") or ""))
        if not raw:
            return ""
        audio, fmt = await prepare_audio(raw)
        files = {"file": (f"voice.{fmt}", audio, _MIME.get(fmt, "application/octet-stream"))}
        try:
            response = await asyncio.wait_for(
                self._client.post(
                    "/audio/transcriptions",
                    data={"model": self._model},
                    files=files,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                ),
                timeout=self._asr_timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as exc:
            logger.warning("语音识别失败：%s", exc)
            return ""
        if not isinstance(payload, dict):
            return ""
        text = str(payload.get("text") or payload.get("result") or "").strip()
        return text[:500]

    async def synthesize(self, text: str) -> Optional[str]:
        """把一段文字合成为 mp3，返回文件绝对路径；失败返回 None。"""
        if not self._tts_enabled:
            return None
        clean = normalize_for_tts(text)
        if not clean:
            return None
        try:
            audio = await asyncio.wait_for(self._synthesize(clean), timeout=self._tts_timeout)
        except (VoiceError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("语音合成失败：%s", exc)
            return None
        except Exception as exc:  # 第三方库的异常类型不固定
            logger.warning("语音合成异常：%s", exc)
            return None
        if not audio:
            return None
        try:
            self._audio_dir.mkdir(parents=True, exist_ok=True)
            path = self._audio_dir / f"{uuid.uuid4().hex}.mp3"
            path.write_bytes(audio)
        except OSError as exc:
            logger.warning("语音文件写入失败：%s", exc)
            return None
        return str(path)

    async def _synthesize(self, text: str) -> bytes:
        if self._synthesizer is not None:
            return await self._synthesizer(text)
        try:
            import edge_tts
        except ImportError as exc:
            raise VoiceError("没有安装 edge-tts") from exc
        chunks: list[bytes] = []
        communicate = edge_tts.Communicate(text, self._tts_voice)
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                chunks.append(chunk["data"])
        return b"".join(chunks)

    async def _load_audio(self, target: str) -> bytes:
        """语音可能是 http 地址，也可能是 NapCat 传过来的本地路径。"""
        if not target:
            return b""
        try:
            if target.startswith(("http://", "https://")):
                response = await self._client.get(target)
                response.raise_for_status()
                if len(response.content) > _MAX_AUDIO_BYTES:
                    return b""
                return response.content
            path = Path(target)
            if not path.is_absolute() or not path.is_file():
                return b""
            if path.stat().st_size > _MAX_AUDIO_BYTES:
                return b""
            return path.read_bytes()
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("语音文件读取失败：%s", exc)
            return b""

    def _cleanup_old_files(self) -> None:
        """清掉一天前的语音文件，避免 data 目录越积越大。"""
        try:
            if not self._audio_dir.is_dir():
                return
            deadline = time.time() - 86400
            for item in self._audio_dir.glob("*.mp3"):
                if item.stat().st_mtime < deadline:
                    item.unlink(missing_ok=True)
        except OSError:
            return

    async def aclose(self) -> None:
        await self._client.aclose()
