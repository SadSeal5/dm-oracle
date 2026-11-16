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
    """Load the gold set, turning malformed input into a readable error."""
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg} at line {exc.lineno})") from exc
    if not isinstance(data, list):
        raise ValueError("expected a JSON array of questions")
    questions: list[Question] = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict) or "query" not in item or "expect" not in item:
            raise ValueError(f"question {index} needs 'query' and 'expect'")
        if not isinstance(item["expect"], list):
            raise ValueError(f"question {index}: 'expect' must be a list")
        mode = str(item.get("mode", "text"))
        if mode not in {"text", "symbol"}:
            # A typo like "symbo" would silently run as a text query and make
            # the recall number meaningless.
            raise ValueError(
                f"question {index}: 'mode' must be 'text' or 'symbol' (got {mode!r})"
            )
        questions.append(
            Question(
                query=str(item["query"]),
                expect=[str(path) for path in item["expect"]],
                mode=mode,
            )
        )
    return questions


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
