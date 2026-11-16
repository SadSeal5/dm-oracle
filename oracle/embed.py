"""Local embeddings and semantic search.

Uses model2vec static embeddings: no GPU, no torch, 256 dimensions, fast
enough to embed a 100k-symbol index on a laptop. Vectors are stored as raw
float32 blobs in SQLite and searched with a brute-force cosine scan, which is
the right trade-off at this scale (≈110 MB, ≈10 ms per query).
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised only without the extra
    np = None  # type: ignore[assignment]
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from . import query

DEFAULT_MODEL = "minishlab/potion-base-8M"
DEFAULT_MAX_CHARS = 1200
EMBED_KINDS = ("proc", "type")

console = Console()

_CACHE: dict[str, tuple[float, "np.ndarray", "np.ndarray"]] = {}


def _require_numpy() -> None:
    if np is None:
        raise RuntimeError(
            "embeddings need numpy and model2vec; install them with "
            "`pip install -e '.[embeddings]'`"
        )


class Embedder:
    """Lazy wrapper around a model2vec StaticModel."""

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from model2vec import StaticModel
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "semantic search needs model2vec; install it with "
                    "`pip install -e '.[embeddings]'`"
                ) from exc

            self._model = StaticModel.from_pretrained(self.model_name)
        return self._model

    @property
    def dim(self) -> int:
        return int(self._load().dim)

    def encode(self, texts: list[str], batch_size: int = 256) -> np.ndarray:
        _require_numpy()
        model = self._load()
        chunks: list[np.ndarray] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            vectors = np.asarray(model.encode(batch), dtype=np.float32)
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            chunks.append(vectors / norms)
        if not chunks:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack(chunks)


def symbol_text(path: str, signature: str | None, doc: str | None, body: str | None, max_chars: int) -> str:
    head = path.replace("/", " ")
    parts = [head]
    if doc:
        parts.append(doc.strip())
    if signature:
        parts.append(signature.strip())
    if body:
        parts.append(body.strip()[:max_chars])
    return "\n".join(part for part in parts if part)


def build_embeddings(
    conn: sqlite3.Connection,
    model_name: str = DEFAULT_MODEL,
    batch_size: int = 256,
    max_chars: int = DEFAULT_MAX_CHARS,
    limit: int | None = None,
    quiet: bool = False,
) -> int:
    """Embed procs and types, replacing any previous vectors for the model."""
    _require_numpy()
    sql = (
        "SELECT s.id, s.path, s.signature, s.doc, fts.body "
        "FROM symbols s JOIN symbols_fts fts ON fts.rowid = s.id "
        "WHERE s.kind IN ('proc', 'type') ORDER BY s.id"
    )
    if limit is not None:
        sql += f" LIMIT {max(0, int(limit))}"
    rows = conn.execute(sql).fetchall()
    if not rows:
        return 0

    embedder = Embedder(model_name)
    texts = [symbol_text(r["path"], r["signature"], r["doc"], r["body"], max_chars) for r in rows]

    progress = Progress(
        TextColumn("[bold cyan]embedding[/]"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        disable=quiet,
    )
    started = time.perf_counter()
    written = 0
    with progress:
        task = progress.add_task("symbols", total=len(rows))
        for start in range(0, len(texts), batch_size):
            batch_texts = texts[start : start + batch_size]
            batch_rows = rows[start : start + batch_size]
            vectors = embedder.encode(batch_texts, batch_size=batch_size)
            payload = [
                (int(row["id"]), model_name, int(vectors.shape[1]), vectors[i].tobytes())
                for i, row in enumerate(batch_rows)
            ]
            conn.executemany(
                "INSERT INTO embeddings(symbol_id, model, dim, vector) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(symbol_id) DO UPDATE SET model=excluded.model, "
                "dim=excluded.dim, vector=excluded.vector",
                payload,
            )
            written += len(payload)
            progress.advance(task, len(payload))
    conn.commit()
    _CACHE.clear()
    elapsed = time.perf_counter() - started
    if not quiet:
        console.print(f"[green]embedded[/] {written:,} symbols in {elapsed:.1f}s ({model_name})")
    return written


def _cache_key(conn: sqlite3.Connection, model_name: str) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row["file"] if row else ""
    if not path:
        # In-memory databases all report an empty path; key them by connection
        # so two test databases cannot share a cached matrix.
        path = f"mem:{id(conn)}"
    try:
        mtime = os.path.getmtime(path) if path else 0.0
    except OSError:
        mtime = 0.0
    return f"{path}:{mtime}:{model_name}"


def _load_matrix(conn: sqlite3.Connection, model_name: str) -> tuple[np.ndarray, np.ndarray]:
    _require_numpy()
    key = _cache_key(conn, model_name)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached[1], cached[2]

    rows = conn.execute(
        "SELECT symbol_id, vector FROM embeddings WHERE model = ?", (model_name,)
    ).fetchall()
    if not rows:
        empty = np.zeros(0, dtype=np.int64), np.zeros((0, 1), dtype=np.float32)
        _CACHE[key] = (time.time(), empty[0], empty[1])
        return empty

    ids = np.fromiter((int(row["symbol_id"]) for row in rows), dtype=np.int64, count=len(rows))
    buffer = b"".join(row["vector"] for row in rows)
    matrix = np.frombuffer(buffer, dtype=np.float32).reshape(len(rows), -1).copy()
    _CACHE[key] = (time.time(), ids, matrix)
    return ids, matrix


def semantic_scores(
    conn: sqlite3.Connection,
    text: str,
    model_name: str = DEFAULT_MODEL,
    limit: int = 20,
) -> list[tuple[int, float]]:
    _require_numpy()
    ids, matrix = _load_matrix(conn, model_name)
    if ids.size == 0:
        return []
    limit = max(0, int(limit))
    if limit == 0:
        return []
    embedder = Embedder(model_name)
    vector = embedder.encode([text])[0]
    scores = matrix @ vector
    top = min(limit, scores.shape[0])
    indices = np.argpartition(-scores, top - 1)[:top]
    indices = indices[np.argsort(-scores[indices])]
    return [(int(ids[i]), float(scores[i])) for i in indices]


def semantic_hits(
    conn: sqlite3.Connection,
    text: str,
    model_name: str = DEFAULT_MODEL,
    limit: int = 20,
) -> list[query.SymbolHit]:
    scored = semantic_scores(conn, text, model_name=model_name, limit=limit)
    if not scored:
        return []
    ids = [symbol_id for symbol_id, _score in scored]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        query._SYMBOL_SELECT + f" WHERE s.id IN ({placeholders})", ids
    ).fetchall()
    by_id = {int(row["id"]): query._row_to_hit(row) for row in rows}
    return [by_id[symbol_id] for symbol_id, _score in scored if symbol_id in by_id]


def rrf_fuse(
    lexical: list[query.SymbolHit],
    semantic: list[query.SymbolHit],
    limit: int = 20,
    k: int = 60,
    weights: tuple[float, float] = (1.0, 0.5),
) -> list[query.SymbolHit]:
    """Reciprocal rank fusion of two ranked lists."""
    limit = max(0, int(limit))
    scores: dict[str, float] = {}
    hits: dict[str, query.SymbolHit] = {}
    for rank, hit in enumerate(lexical, 1):
        scores[hit.path] = scores.get(hit.path, 0.0) + weights[0] / (k + rank)
        hits.setdefault(hit.path, hit)
    for rank, hit in enumerate(semantic, 1):
        scores[hit.path] = scores.get(hit.path, 0.0) + weights[1] / (k + rank)
        hits.setdefault(hit.path, hit)
    ordered = sorted(scores.items(), key=lambda item: -item[1])
    return [hits[path] for path, _score in ordered[:limit]]


def hybrid_semantic_search(
    conn: sqlite3.Connection,
    text: str,
    limit: int = 20,
    model_name: str | None = None,
) -> list[query.SymbolHit]:
    """Lexical hybrid search fused with semantic neighbours.

    Semantic reranking only kicks in when the lexical ranker is not confident:
    a proc whose name or path segments cover the whole query is trusted as-is.
    That keeps precise symbol lookups intact while giving natural-language
    queries a second opinion.
    """
    if model_name is None:
        model_name = embedding_model(conn)
    limit = max(0, int(limit))
    if limit == 0 or not text.strip():
        return []
    lexical = query.hybrid_search(conn, text, limit=max(limit * 3, 30))
    if not lexical:
        return semantic_hits(conn, text, model_name=model_name, limit=limit)

    tokens = {token.lower() for token in query._content_tokens(text)}
    joined = "_".join(sorted(tokens))
    top = lexical[0]
    segments = {part for part in re.split(r"[/_]", top.path.lower()) if part}
    confident = top.kind == "proc" and (
        top.name.lower() in {text.strip().lower(), joined}
        or (tokens and tokens <= segments)
    )
    if confident:
        return lexical[:limit]

    semantic = semantic_hits(conn, text, model_name=model_name, limit=max(limit * 3, 30))
    if not semantic:
        return lexical[:limit]
    return rrf_fuse(lexical, semantic, limit=limit)


def embedding_model(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'embedding_model'").fetchone()
    return row["value"] if row else DEFAULT_MODEL


def has_embeddings(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row["n"])


def set_embedding_model(conn: sqlite3.Connection, model_name: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('embedding_model', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (model_name,),
    )
    conn.commit()
