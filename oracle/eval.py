"""Retrieval evaluation harness.

A small gold set of questions with expected symbol paths. It is intentionally
cheap to extend: add a JSON file with ``{"query", "expect", "mode"}`` entries
and point ``oracle eval`` at it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import embed, query


@dataclass
class Question:
    query: str
    expect: list[str]
    mode: str = "text"


@dataclass
class Result:
    question: Question
    ranked: list[str]
    rank: int | None

    @property
    def hit(self) -> bool:
        return self.rank is not None


@dataclass
class EvalReport:
    results: list[Result] = field(default_factory=list)
    k: int = 5

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def hits(self) -> int:
        return sum(1 for result in self.results if result.hit)

    @property
    def recall_at_k(self) -> float:
        return self.hits / self.total if self.total else 0.0

    @property
    def mrr(self) -> float:
        if not self.total:
            return 0.0
        return sum(1.0 / result.rank for result in self.results if result.rank) / self.total

    @property
    def top1(self) -> float:
        if not self.total:
            return 0.0
        return sum(1 for result in self.results if result.rank == 1) / self.total


def load_questions(path: str | Path) -> list[Question]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Question(
            query=item["query"],
            expect=list(item["expect"]),
            mode=item.get("mode", "text"),
        )
        for item in data
    ]


def run_eval(
    conn: sqlite3.Connection,
    questions: list[Question],
    k: int = 5,
    semantic: bool = False,
) -> EvalReport:
    report = EvalReport(k=k)
    for question in questions:
        if question.mode == "symbol":
            ranked = [hit.path for hit in query.find(conn, question.query, limit=k * 4)]
        elif semantic:
            ranked = [
                hit.path
                for hit in embed.hybrid_semantic_search(conn, question.query, limit=k * 4)
            ]
        else:
            ranked = [hit.path for hit in query.hybrid_search(conn, question.query, limit=k * 4)]

        rank = None
        for index, path in enumerate(ranked, 1):
            if path in question.expect:
                rank = index
                break
        report.results.append(Result(question=question, ranked=ranked[:k], rank=rank))
    return report
