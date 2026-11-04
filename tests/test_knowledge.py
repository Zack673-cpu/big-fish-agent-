"""人格语料检索（RAG）的单元测试。"""

import asyncio

from app.core.embeddings import EmbeddingError
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


def make_kb(tmp_path, **kwargs) -> KnowledgeBase:
    path = tmp_path / "kb.md"
    path.write_text(KB_TEXT, encoding="utf-8")
    return KnowledgeBase(path, index_dir=tmp_path / "index", **kwargs)


def start(kb: KnowledgeBase) -> None:
    asyncio.run(kb.start(wait=True))


def retrieve(kb: KnowledgeBase, query: str, **kwargs):
    return asyncio.run(kb.retrieve(query, **kwargs))


class FakeEmbedder:
    """按关键词返回固定向量，避免单元测试真的请求百炼。"""

    model = "fake-embedding"
    dimensions = 3

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.batch_sizes = []
        self._fail = fail

    async def embed(self, texts):
        self.calls += 1
        self.batch_sizes.append(len(texts))
        if self._fail:
            raise EmbeddingError("模拟接口失败")
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text):
        if "小伊卡" in text:
            return [1.0, 0.0, 0.0]
        if "观鸟" in text:
            return [0.0, 1.0, 0.0]
        if "新增" in text:
            return [0.0, 0.0, 1.0]
        return [0.1, 0.1, 0.1]

    async def aclose(self) -> None:
        return None


def test_retrieve_hits_matching_chunk(tmp_path):
    kb = make_kb(tmp_path)
    start(kb)
    results = retrieve(kb, "小伊卡是谁", top_k=1)
    assert len(results) == 1
    assert "小伊卡" in results[0]
    assert "观鸟" not in results[0]


def test_retrieve_skips_usage_and_source_sections(tmp_path):
    kb = make_kb(tmp_path)
    start(kb)
    results = retrieve(kb, "使用说明 资料出处", top_k=5)
    assert all("不应该" not in item for item in results)


def test_retrieve_returns_empty_for_unrelated_query(tmp_path):
    kb = make_kb(tmp_path)
    start(kb)
    assert retrieve(kb, "今天天气怎么样") == []


def test_single_common_bigram_does_not_match(tmp_path):
    kb = make_kb(tmp_path)
    start(kb)
    assert retrieve(kb, "今天天气不错") == []


def test_retrieve_short_phrase_still_matches(tmp_path):
    kb = make_kb(tmp_path)
    start(kb)
    results = retrieve(kb, "观鸟", top_k=1)
    assert len(results) == 1
    assert "观鸟" in results[0]


def test_retrieve_missing_file_returns_empty(tmp_path):
    kb = KnowledgeBase(tmp_path / "missing.md", index_dir=tmp_path / "missing_index")
    start(kb)
    assert retrieve(kb, "小伊卡") == []


def test_embedding_retrieve_and_cache(tmp_path):
    embedder = FakeEmbedder()
    kb = make_kb(tmp_path, embedder=embedder, embedding_min_score=0.5)

    start(kb)
    assert embedder.calls == 1
    assert (tmp_path / "index" / "index.faiss").is_file()

    results = retrieve(kb, "小伊卡是谁", top_k=1)
    assert len(results) == 1
    assert "小伊卡" in results[0]
    # 建索引 1 次 + 查询 1 次
    assert embedder.calls == 2


def test_embedding_cache_reused_without_reindex(tmp_path):
    first = FakeEmbedder()
    kb = make_kb(tmp_path, embedder=first, embedding_min_score=0.5)
    start(kb)
    assert first.calls == 1

    second = FakeEmbedder()
    kb2 = make_kb(tmp_path, embedder=second, embedding_min_score=0.5)
    start(kb2)
    assert second.calls == 0

    results = retrieve(kb2, "小伊卡是谁", top_k=1)
    assert "小伊卡" in results[0]
    assert second.calls == 1


def test_embedding_failure_falls_back_to_bigram(tmp_path):
    kb = make_kb(tmp_path, embedder=FakeEmbedder(fail=True))
    start(kb)

    results = retrieve(kb, "小伊卡是谁", top_k=1)

    assert len(results) == 1
    assert "小伊卡" in results[0]


def test_directory_index_and_incremental_update(tmp_path):
    corpus = tmp_path / "knowledge"
    corpus.mkdir()
    index_dir = tmp_path / "index"
    (corpus / "a.md").write_text(
        "## 小伊卡\n天马小伊卡是风堇的伙伴。\n", encoding="utf-8"
    )

    first = FakeEmbedder()
    kb = KnowledgeBase(
        corpus, embedder=first, index_dir=index_dir, embedding_min_score=0.4
    )
    start(kb)
    assert first.calls == 1

    second = FakeEmbedder()
    kb2 = KnowledgeBase(
        corpus, embedder=second, index_dir=index_dir, embedding_min_score=0.4
    )
    start(kb2)
    assert second.calls == 0
    assert retrieve(kb2, "小伊卡是谁", top_k=1)

    # 修改一个文件、新增一个文件，只应重算受影响的块
    (corpus / "a.md").write_text(
        "## 小伊卡\n天马小伊卡是风堇的伙伴，又长大了一点。\n", encoding="utf-8"
    )
    (corpus / "b.md").write_text("## 新增\n这是新增的语料内容。\n", encoding="utf-8")
    third = FakeEmbedder()
    kb3 = KnowledgeBase(
        corpus, embedder=third, index_dir=index_dir, embedding_min_score=0.4
    )
    start(kb3)
    assert third.calls == 1

    results = retrieve(kb3, "新增", top_k=1)
    assert len(results) == 1
    assert "新增" in results[0]

    # 删除文件后旧片段必须从索引里移除
    (corpus / "b.md").unlink()
    fourth = FakeEmbedder()
    kb4 = KnowledgeBase(
        corpus, embedder=fourth, index_dir=index_dir, embedding_min_score=0.4
    )
    start(kb4)
    assert fourth.calls == 0
    assert retrieve(kb4, "新增", top_k=1) == []


def test_large_corpus_is_embedded_in_groups(tmp_path):
    corpus = tmp_path / "knowledge"
    corpus.mkdir()
    for file_index in range(25):
        sections = []
        for section_index in range(10):
            sections.append(
                f"## 章节{file_index}-{section_index}\n"
                f"这是第 {file_index} 个文件的第 {section_index} 段测试语料。\n"
            )
        (corpus / f"file{file_index}.md").write_text(
            "\n".join(sections), encoding="utf-8"
        )

    embedder = FakeEmbedder()
    kb = KnowledgeBase(
        corpus, embedder=embedder, index_dir=tmp_path / "index"
    )
    start(kb)

    assert embedder.calls == 2
    assert embedder.batch_sizes == [200, 50]
    assert kb._index.ntotal == 250


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
