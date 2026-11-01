from app.onebot.events import parse_event


def test_group_array_at_me():
    payload = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 1,
        "user_id": 10001,
        "group_id": 20001,
        "self_id": 99999,
        "sender": {"card": "小明", "nickname": "小明"},
        "message": [
            {"type": "at", "data": {"qq": "99999"}},
            {"type": "text", "data": {"text": " 你好"}},
        ],
    }
    event = parse_event(payload)
    assert event is not None
    assert event.is_group
    assert event.at_me
    assert event.text.strip() == "你好"
    assert event.sender_name == "小明"
    assert event.chat_key == "group:20001"


def test_group_array_at_someone_else():
    payload = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 2,
        "user_id": 10001,
        "group_id": 20001,
        "self_id": 99999,
        "message": [
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": " 晚上好"}},
        ],
    }
    event = parse_event(payload)
    assert event is not None
    assert not event.at_me


def test_private_cq_string_with_image():
    payload = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 3,
        "user_id": 10002,
        "self_id": 99999,
        "message": "你好[CQ:image,file=1.jpg]",
    }
    event = parse_event(payload)
    assert event is not None
    assert event.is_private
    assert event.has_media
    assert event.text == "你好"
    assert event.chat_key == "private:10002"


def test_ignore_self_message():
    payload = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 4,
        "user_id": 99999,
        "group_id": 20001,
        "self_id": 99999,
        "message": "我在呀",
    }
    assert parse_event(payload) is None


def test_ignore_non_message_event():
    payload = {"post_type": "meta_event", "meta_event_type": "heartbeat", "self_id": 99999}
    assert parse_event(payload) is None


def test_group_without_group_id():
    payload = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 5,
        "user_id": 10001,
        "self_id": 99999,
        "message": "你好",
    }
    assert parse_event(payload) is None


def test_group_array_face_only_marks_media():
    payload = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 6,
        "user_id": 10001,
        "group_id": 20001,
        "self_id": 99999,
        "message": [
            {"type": "face", "data": {"id": "1", "summary": "/菜汪"}}
        ],
    }
    event = parse_event(payload)
    assert event is not None
    assert event.text == ""
    assert event.has_media
    assert not event.at_me
    assert event.attachments[0].kind == "face"
    assert event.attachments[0].label == "/菜汪"


def test_array_image_keeps_url():
    payload = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 9,
        "user_id": 10005,
        "self_id": 99999,
        "message": [
            {
                "type": "image",
                "data": {"sub_type": "1", "url": "https://example.com/a.jpg"},
            }
        ],
    }
    event = parse_event(payload)
    assert event is not None
    assert event.has_media
    assert event.attachments[0].kind == "image"
    assert event.attachments[0].label == "表情包"
    assert event.attachments[0].url == "https://example.com/a.jpg"


def test_private_cq_face_marks_media():
    payload = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 7,
        "user_id": 10003,
        "self_id": 99999,
        "message": "[CQ:face,id=1]",
    }
    event = parse_event(payload)
    assert event is not None
    assert event.text == ""
    assert event.has_media


def test_reply_segment_is_not_media():
    payload = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 8,
        "user_id": 10004,
        "self_id": 99999,
        "message": "[CQ:reply,id=1]你好",
    }
    event = parse_event(payload)
    assert event is not None
    assert not event.has_media
    assert event.text == "你好"
