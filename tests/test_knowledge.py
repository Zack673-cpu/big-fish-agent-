"""人格语料检索（RAG）的单元测试。"""

from app.core.knowledge import KnowledgeBase
from app.core.persona import build_system_prompt

KB_TEXT = """# 测试语料

<!-- 注释内容不参与检索 -->

## 小伊卡

天马小伊卡是风堇的翼兽伙伴，小小一团，总是没睡醒的样子。它洒落的虹光曾救活过一整座城邦的人。

## 观鸟

风堇喜欢用留影石机记录翁法罗斯的鸟儿，相册已经厚厚一本。

## 对话示例

用户说「今天好累啊」时，先陪对方慢慢说，不着急。

## 使用说明

这段是使用说明，不应该出现在检索结果里。

## 资料出处

这里是来源链接，不应该出现在检索结果里。
"""


def make_kb(tmp_path) -> KnowledgeBase:
    path = tmp_path / "kb.md"
    path.write_text(KB_TEXT, encoding="utf-8")
    return KnowledgeBase(path)


def test_retrieve_hits_matching_chunk(tmp_path):
    kb = make_kb(tmp_path)
    results = kb.retrieve("小伊卡是谁", top_k=1)
    assert len(results) == 1
    assert "小伊卡" in results[0]
    assert "观鸟" not in results[0]


def test_retrieve_skips_usage_and_source_sections(tmp_path):
    kb = make_kb(tmp_path)
    results = kb.retrieve("使用说明 资料出处", top_k=5)
    assert all("不应该" not in item for item in results)


def test_retrieve_returns_empty_for_unrelated_query(tmp_path):
    kb = make_kb(tmp_path)
    assert kb.retrieve("今天天气怎么样") == []


def test_single_common_bigram_does_not_match(tmp_path):
    kb = make_kb(tmp_path)
    assert kb.retrieve("今天天气不错") == []


def test_retrieve_short_phrase_still_matches(tmp_path):
    kb = make_kb(tmp_path)
    results = kb.retrieve("观鸟", top_k=1)
    assert len(results) == 1
    assert "观鸟" in results[0]


def test_retrieve_missing_file_returns_empty(tmp_path):
    kb = KnowledgeBase(tmp_path / "missing.md")
    assert kb.retrieve("小伊卡") == []


def test_system_prompt_includes_knowledge_and_identity_guard():
    prompt = build_system_prompt(
        bot_name="大肥鱼",
        max_chars=60,
        is_group=False,
        summary="",
        memories=[],
        knowledge=["【小伊卡】\n天马小伊卡是风堇的伙伴。"],
    )
    assert "【风堇语料参考" in prompt
    assert "不是风堇本人" in prompt
    assert "天马小伊卡" in prompt
