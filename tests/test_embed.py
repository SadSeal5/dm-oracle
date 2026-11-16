from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from helpers import add_symbol
from oracle import embed, query, store


def _fake_encode(self, texts: list[str], batch_size: int = 256) -> np.ndarray:
    """Deterministic 4-d stand-in for a model2vec model."""
    rows = []
    for text in texts:
        low = text.lower()
        rows.append(
            [
                float("explosion" in low),
                float("airlock" in low or "door" in low),
                float("projectile" in low or "bullet" in low),
                0.1,
            ]
        )
    vectors = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


@pytest.fixture(autouse=True)
def _clear_cache():
    embed._CACHE.clear()
    yield
    embed._CACHE.clear()


@pytest.fixture()
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed.Embedder, "encode", _fake_encode)


def _build(conn: sqlite3.Connection) -> dict[str, int]:
    file_id = store.insert_file(conn, "code/test.dm", 100, "abc", 0.0)
    ids: dict[str, int] = {}
    rows = [
        (
            "/proc/explosion",
            "proc",
            "explosion",
            "Detonate at a turf: explosion damage to everything nearby.",
        ),
        (
            "/obj/machinery/door/airlock/proc/open",
            "proc",
            "open",
            "Open the airlock door.",
        ),
        (
            "/obj/projectile/proc/on_hit",
            "proc",
            "on_hit",
            "A projectile or bullet hits a target.",
        ),
    ]
    for path, kind, name, body in rows:
        symbol_id = add_symbol(conn, file_id, kind, path, name=name)
        ids[path] = symbol_id
        store.insert_fts(conn, symbol_id, path, name, f"{name}()", None, body)
    conn.commit()
    return ids


def test_symbol_text_combines_path_doc_signature_and_body() -> None:
    text = embed.symbol_text("/obj/item/proc/use", "use(mob/user)", "Do the thing.", "return 1", 100)
    assert "obj item proc use" in text
    assert "Do the thing." in text
    assert "use(mob/user)" in text
    assert "return 1" in text


def test_symbol_text_truncates_body() -> None:
    text = embed.symbol_text("/a/proc/b", None, None, "x" * 50, 10)
    assert "x" * 10 in text
    assert "x" * 11 not in text


def test_build_embeddings_and_semantic_scores(
    conn: sqlite3.Connection, fake_embedder: None
) -> None:
    ids = _build(conn)
    written = embed.build_embeddings(conn, model_name="fake-model", quiet=True)
    assert written == len(ids)

    scored = embed.semantic_scores(conn, "explosion", model_name="fake-model", limit=3)
    assert scored[0][0] == ids["/proc/explosion"]

    hits = embed.semantic_hits(conn, "airlock door", model_name="fake-model", limit=1)
    assert hits[0].path == "/obj/machinery/door/airlock/proc/open"


def test_embedding_model_round_trips(conn: sqlite3.Connection) -> None:
    assert embed.embedding_model(conn) == embed.DEFAULT_MODEL
    embed.set_embedding_model(conn, "fake-model")
    assert embed.embedding_model(conn) == "fake-model"


def test_rrf_fuse_weights_lexical_above_semantic() -> None:
    def hit(path: str) -> query.SymbolHit:
        return query.SymbolHit(
            id=1,
            kind="proc",
            path=path,
            name=path.rsplit("/", 1)[-1],
            parent=None,
            signature=None,
            file="code/test.dm",
            start_line=1,
            end_line=2,
        )

    lexical = [hit("/a/proc/one"), hit("/a/proc/two")]
    semantic = [hit("/a/proc/two"), hit("/a/proc/three")]
    fused = embed.rrf_fuse(lexical, semantic, limit=3)
    assert [item.path for item in fused] == ["/a/proc/two", "/a/proc/one", "/a/proc/three"]


def test_confident_lexical_hit_skips_semantic(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build(conn)
    calls: list[str] = []

    def boom(*args, **kwargs):
        calls.append("semantic")
        return []

    monkeypatch.setattr(embed, "semantic_hits", boom)
    hits = embed.hybrid_semantic_search(conn, "explosion", limit=3)
    assert hits[0].path == "/proc/explosion"
    assert calls == []


def test_uncertain_query_consults_semantic(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build(conn)
    calls: list[str] = []

    def fake_semantic(*args, **kwargs):
        calls.append("semantic")
        return [query.get_symbol(conn, "/obj/projectile/proc/on_hit")[0]]

    monkeypatch.setattr(embed, "semantic_hits", fake_semantic)
    hits = embed.hybrid_semantic_search(conn, "how does being hit work", limit=5)
    assert calls == ["semantic"]
    assert any(hit.path == "/obj/projectile/proc/on_hit" for hit in hits)


def test_missing_numpy_is_a_clean_error(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed, "np", None)
    with pytest.raises(RuntimeError, match="pip install"):
        embed.semantic_scores(conn, "explosion")
    with pytest.raises(RuntimeError, match="pip install"):
        embed.build_embeddings(conn)
