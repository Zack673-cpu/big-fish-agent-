from app.config import Settings
from app.core.text import clean_text, contains_alias, remove_aliases, split_text


def test_clean_text_removes_invisible_and_spaces():
    assert clean_text("  你好 \u200b ") == "你好"
    assert clean_text("第一行\r\n\r\n\r\n第二行") == "第一行\n\n第二行"


def test_clean_text_empty():
    assert clean_text("") == ""


def test_contains_alias():
    assert contains_alias("大肥鱼你好", Settings().aliases)
    assert contains_alias("肥鱼在吗", Settings().aliases)
    assert not contains_alias("今天天气不错", Settings().aliases)


def test_remove_aliases():
    assert remove_aliases("大肥鱼 你好", Settings().aliases) == "你好"
    assert remove_aliases("你好呀大肥鱼", Settings().aliases) == "你好呀"


def test_split_text_short():
    assert split_text("你好呀~", 60, 3) == ["你好呀~"]


def test_split_text_respects_limits():
    text = "啊" * 200
    segments = split_text(text, 60, 3)
    assert len(segments) == 3
    assert all(len(segment) <= 60 for segment in segments)
    assert segments[-1].endswith("…")


def test_split_text_keeps_paragraphs():
    segments = split_text("第一段\n第二段", 60, 3)
    assert segments == ["第一段 第二段"]


def test_split_text_empty():
    assert split_text("   ", 60, 3) == []
