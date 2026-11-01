import asyncio
from pathlib import Path

from app.core.voice import (
    VoiceService,
    detect_audio_format,
    normalize_for_tts,
    pop_voice_marker,
)
from app.onebot.events import Attachment

WAV_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVEfmt " + b"\x00" * 40


def test_pop_voice_marker():
    assert pop_voice_marker("[语音] 你好呀") == ("你好呀", True)
    assert pop_voice_marker("【语音】晚安") == ("晚安", True)
    assert pop_voice_marker("  [语音]\n嘿嘿") == ("嘿嘿", True)
    assert pop_voice_marker("你好呀") == ("你好呀", False)
    assert pop_voice_marker("我说 [语音] 不是标记") == ("我说 [语音] 不是标记", False)


def test_detect_audio_format():
    assert detect_audio_format(WAV_BYTES) == "wav"
    assert detect_audio_format(b"ID3\x03\x00") == "mp3"
    assert detect_audio_format(b"#!AMR\n") == "amr"
    assert detect_audio_format(b"\x02#!SILK_V3" + b"\x00" * 10) == "silk"
    assert detect_audio_format(b"") == ""
    assert detect_audio_format(b"random") == ""


def test_normalize_for_tts():
    assert normalize_for_tts("**嘿嘿**  # 好呀") == "嘿嘿 好呀"
    assert normalize_for_tts("  \n ") == ""


class FakeResponse:
    def __init__(self, payload=None, content: bytes = b"", content_type: str = "audio/wav"):
        self.status_code = 200
        self._payload = payload
        self.content = content
        self.headers = {"content-type": content_type}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeVoiceClient:
    def __init__(self, text: str = "今天天气不错") -> None:
        self.text = text
        self.posts: list = []
        self.gets: list = []

    async def post(self, url, data=None, files=None, headers=None):
        self.posts.append({"url": url, "data": data, "files": files, "headers": headers})
        return FakeResponse({"text": self.text})

    async def get(self, url):
        self.gets.append(url)
        return FakeResponse(content=WAV_BYTES)

    async def aclose(self):
        return None


def test_transcribe_downloads_and_posts(tmp_path):
    client = FakeVoiceClient()
    service = VoiceService(api_key="sk-test", data_dir=tmp_path, client=client)
    attachment = Attachment("record", "语音", "https://example.com/a.amr")
    text = asyncio.run(service.transcribe(attachment))

    assert text == "今天天气不错"
    assert client.gets == ["https://example.com/a.amr"]
    assert client.posts[0]["url"] == "/audio/transcriptions"
    assert client.posts[0]["data"] == {"model": "FunAudioLLM/SenseVoiceSmall"}
    assert client.posts[0]["headers"]["Authorization"] == "Bearer sk-test"


def test_transcribe_reads_local_file(tmp_path):
    audio = tmp_path / "voice.wav"
    audio.write_bytes(WAV_BYTES)
    service = VoiceService(api_key="sk-test", data_dir=tmp_path, client=FakeVoiceClient())
    text = asyncio.run(service.transcribe(Attachment("record", "语音", str(audio))))
    assert text == "今天天气不错"


def test_transcribe_without_key_is_skipped(tmp_path):
    client = FakeVoiceClient()
    service = VoiceService(api_key="", data_dir=tmp_path, client=client)
    assert service.asr_enabled is False
    assert asyncio.run(service.transcribe(Attachment("record", "语音", "https://example.com/a.wav"))) == ""
    assert client.posts == []


def test_transcribe_handles_bad_response(tmp_path):
    class BrokenClient(FakeVoiceClient):
        async def post(self, url, data=None, files=None, headers=None):
            return FakeResponse({"text": ""})

    service = VoiceService(api_key="sk-test", data_dir=tmp_path, client=BrokenClient())
    assert asyncio.run(service.transcribe(Attachment("record", "语音", "https://example.com/a.wav"))) == ""


def test_synthesize_writes_file(tmp_path):
    async def fake_synth(text: str) -> bytes:
        assert text == "晚安呀"
        return b"fake-mp3"

    service = VoiceService(
        api_key="", data_dir=tmp_path, synthesizer=fake_synth, tts_enabled=True
    )
    path = asyncio.run(service.synthesize("晚安呀"))
    assert path is not None
    assert Path(path).read_bytes() == b"fake-mp3"
    assert Path(path).parent.name == "voice"


def test_synthesize_failure_returns_none(tmp_path):
    async def broken(text: str) -> bytes:
        raise RuntimeError("合成挂了")

    service = VoiceService(api_key="", data_dir=tmp_path, synthesizer=broken)
    assert asyncio.run(service.synthesize("晚安呀")) is None


def test_synthesize_disabled_returns_none(tmp_path):
    async def fake_synth(text: str) -> bytes:
        return b"fake-mp3"

    service = VoiceService(
        api_key="", data_dir=tmp_path, synthesizer=fake_synth, tts_enabled=False
    )
    assert service.tts_enabled is False
    assert asyncio.run(service.synthesize("晚安呀")) is None


def test_cleanup_old_voice_files(tmp_path):
    voice_dir = tmp_path / "voice"
    voice_dir.mkdir()
    fresh = voice_dir / "fresh.mp3"
    fresh.write_bytes(b"new")
    old = voice_dir / "old.mp3"
    old.write_bytes(b"old")
    old.touch()
    import os
    import time

    os.utime(old, (time.time() - 100000, time.time() - 100000))
    VoiceService(api_key="", data_dir=tmp_path, synthesizer=None, tts_enabled=False)
    assert fresh.exists()
    assert not old.exists()
