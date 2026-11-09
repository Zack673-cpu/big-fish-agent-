"""检索质量评测（黑盒）：固定设定问题集 → 真实 RAG 检索 → 命中率报告。

用法（在项目根目录执行）：
    python scripts/eval_retrieval.py --label baseline
    python scripts/eval_retrieval.py --top-k 10 --label after_factcard
    python scripts/eval_retrieval.py --no-embedding --label literal

说明：
- 只调用向量接口和本地索引，不调用聊天模型，成本低、结果可重复。
- 命中判定：返回片段里出现任一期望关键词，即视为召回成功；报告同时给出首中位次。
- 报告写到 data/eval/（已在 .gitignore 中），不影响线上服务。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.config import load_settings  # noqa: E402
from app.core.embeddings import BailianEmbeddings  # noqa: E402
from app.core.knowledge import KnowledgeBase  # noqa: E402

OUT_DIR = BASE_DIR / "data" / "eval"

# 设定类问题集：每题给若干期望关键词，命中任一即算召回成功。
# 关键词刻意选“定义型”词，避免只要出现人名就算命中。
QUESTIONS = [
    {"cat": "身份", "query": "风堇的真名是什么", "expect": ["雅辛忒丝"]},
    {"cat": "身份", "query": "风堇的称号是什么", "expect": ["摇光的医师"]},
    {"cat": "身份", "query": "风堇是不是天空半神", "expect": ["半神"]},
    {"cat": "身份", "query": "风堇在哪座城市重建了庭院", "expect": ["奥赫玛"]},
    {"cat": "设定", "query": "如我所书是什么", "expect": ["昔涟"]},
    {"cat": "设定", "query": "小伊卡是什么", "expect": ["翼兽", "虹色", "嘟嘟"]},
    {"cat": "设定", "query": "小伊卡为什么不会说话", "expect": ["不会说话", "不再说话"]},
    {"cat": "设定", "query": "昏光庭院是什么地方", "expect": ["疗愈", "弥合"]},
    {"cat": "设定", "query": "艾格勒是谁", "expect": ["晨昏之眼", "天空之泰坦"]},
    {"cat": "设定", "query": "阳雷骑士塞涅俄丝是谁", "expect": ["塞涅俄丝"]},
    {"cat": "设定", "query": "黎明机器是什么", "expect": ["黎明机器"]},
    {"cat": "设定", "query": "创世涡心是什么", "expect": ["火种", "归还"]},
    {"cat": "设定", "query": "神悟树庭是什么地方", "expect": ["求学", "学派"]},
    {"cat": "关系", "query": "风堇的父亲去哪了", "expect": ["远行", "没有回来"]},
    {"cat": "关系", "query": "风堇的祖母是谁", "expect": ["雅辛缇亚"]},
    {"cat": "关系", "query": "那刻夏和风堇是什么关系", "expect": ["助手"]},
    {"cat": "关系", "query": "风堇怎么称呼亲近的开拓者", "expect": ["灰宝"]},
    {"cat": "关系", "query": "缇宝怎么称呼风堇", "expect": ["小风堇"]},
    {"cat": "关系", "query": "风堇和遐蝶是什么关系", "expect": ["观鸟", "观星"]},
    {"cat": "剧情", "query": "3.1 常有别离时是什么任务", "expect": ["家属", "常有别离时"]},
    {"cat": "剧情", "query": "3.2 走过安眠地的花丛里风堇做了什么", "expect": ["实验室", "根石之间"]},
    {"cat": "剧情", "query": "3.3 风堇的结局是什么", "expect": ["天象画壁"]},
    {"cat": "剧情", "query": "3.4 美梦与圣杯活动里风堇在做什么", "expect": ["奇美拉"]},
    {"cat": "剧情", "query": "3.5 黄金迷境大饭店里风堇做了什么", "expect": ["迷其林", "大饭店"]},
    {"cat": "剧情", "query": "4.2 银河信号漫游里风堇在做什么", "expect": ["助教", "直播"]},
    {"cat": "剧情", "query": "小伊卡暴食的番外讲了什么", "expect": ["暴食", "番外", "饭量"]},
    {"cat": "日常", "query": "风堇有什么爱好", "expect": ["观鸟", "留影石"]},
    {"cat": "日常", "query": "风堇睡前喝什么", "expect": ["羊奶花茶"]},
    {"cat": "性格", "query": "风堇为什么不觉得自己是英雄", "expect": ["平凡"]},
    {"cat": "性格", "query": "风堇对自己的血脉在意吗", "expect": ["血脉", "相去甚远"]},
]


def _display_file_map(index_dir: Path) -> dict[str, str]:
    """从索引缓存里还原“片段 → 来源文件”，只用于报告定位，读不到就返回空。"""
    path = index_dir / "chunks.jsonl"
    if not path.is_file():
        return {}
    mapping: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                display = str(item.get("display") or "")
                filename = str(item.get("file") or "")
                if display and filename:
                    mapping.setdefault(display, filename)
    except (OSError, ValueError):
        return {}
    return mapping


def _first_hit(results: list[str], expect: list[str]) -> int:
    """返回第一个命中期望关键词的位次（从 1 开始）；没命中返回 0。"""
    for rank, text in enumerate(results, start=1):
        if any(word in text for word in expect):
            return rank
    return 0


def _heading_of(text: str) -> str:
    first = text.split("\n", 1)[0].strip()
    return first.strip("【】") if first else "（无标题）"


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    embedder = None
    if not args.no_embedding and settings.embedding_enabled and settings.embedding_api_key:
        embedder = BailianEmbeddings(
            settings.embedding_api_key,
            settings.embedding_base_url,
            settings.embedding_model,
            settings.embedding_dimensions,
            settings.embedding_timeout,
        )

    knowledge = KnowledgeBase(
        settings.knowledge_path,
        embedder=embedder,
        index_dir=settings.knowledge_index_dir,
        embedding_min_score=settings.embedding_min_score,
    )
    started = time.monotonic()
    try:
        await knowledge.start(wait=True)
        index_dir = Path(settings.knowledge_index_dir)
        file_map = _display_file_map(index_dir)
        mode = "向量检索" if embedder is not None else "字面检索（未启用向量）"
        chunk_total = len(knowledge._records)  # 只读，用于报告语料规模
        print(f"语料模式：{mode}；语料块：{chunk_total}；top_k：{args.top_k}")

        rows = []
        hit_at_2 = hit_at_5 = hit_at_all = 0
        rank_sum = 0
        for item in QUESTIONS:
            try:
                results = await knowledge.retrieve(item["query"], args.top_k)
            except Exception as exc:  # 单题失败不影响整份评测
                results = []
                print(f"检索异常：{item['query']} -> {exc}")
            rank = _first_hit(results, item["expect"])
            top_file = file_map.get(results[0], "（未知）") if results else "（无结果）"
            if rank:
                hit_at_all += 1
                rank_sum += rank
                if rank <= 2:
                    hit_at_2 += 1
                if rank <= 5:
                    hit_at_5 += 1
            rows.append(
                {
                    "cat": item["cat"],
                    "query": item["query"],
                    "expect": "/".join(item["expect"]),
                    "rank": rank,
                    "top1": _heading_of(results[0]) if results else "（无结果）",
                    "top_file": top_file,
                    "count": len(results),
                }
            )

        total = len(QUESTIONS)
        average_rank = (rank_sum / hit_at_all) if hit_at_all else 0.0
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = OUT_DIR / f"{stamp}_retrieval_{args.label}.md"

        lines = [
            f"# 检索评测报告 {args.label}",
            "",
            f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
            f"- 模式：{mode}；top_k：{args.top_k}；语料块：{chunk_total}",
            f"- 题量：{total}；命中：{hit_at_all}；命中率：{hit_at_all / total:.1%}",
            f"- 命中@2：{hit_at_2}/{total}（{hit_at_2 / total:.1%}）",
            f"- 命中@5：{hit_at_5}/{total}（{hit_at_5 / total:.1%}）",
            f"- 首中平均位次：{average_rank:.2f}",
            "",
            "| 类别 | 问题 | 期望关键词 | 首中位次 | top1 来源文件 | top1 小节 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in rows:
            lines.append(
                "| {cat} | {query} | {expect} | {rank} | {top_file} | {top1} |".format(
                    cat=row["cat"],
                    query=row["query"],
                    expect=row["expect"],
                    rank=row["rank"] or "未命中",
                    top_file=row["top_file"],
                    top1=row["top1"],
                )
            )
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        elapsed = time.monotonic() - started
        print(
            f"命中 {hit_at_all}/{total}（{hit_at_all / total:.1%}），"
            f"命中@2 {hit_at_2}/{total}，命中@5 {hit_at_5}/{total}，"
            f"首中平均位次 {average_rank:.2f}，耗时 {elapsed:.1f} 秒"
        )
        print(f"报告：{report_path}")
        misses = [row for row in rows if not row["rank"]]
        if misses:
            print("未命中题目：")
            for row in misses:
                print(f"  - [{row['cat']}] {row['query']}（top1：{row['top1']} / {row['top_file']}）")
        return 0 if not misses else 2
    finally:
        await knowledge.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description="检索质量评测")
    parser.add_argument("--label", default="run", help="报告标签，写进文件名")
    parser.add_argument("--top-k", type=int, default=10, help="每题取回片段数，默认 10")
    parser.add_argument(
        "--no-embedding",
        action="store_true",
        help="强制只测字面检索，便于在无向量密钥时对比",
    )
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k 必须大于 0")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
