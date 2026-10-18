"""Stdlib-only recall: TF-IDF vectors + cosine similarity over stored memory.

Searches MEMORY.md lines, all people notes, and recent daily notes.
Pure Python, no dependencies. Empty memory → empty results, never
hallucinated "I remember..." filler — the agent must never invent memories.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Tuple

from . import daily as _daily
from . import people as _people
from . import store as _store

#: Max chars of recalled context injected into the system prompt.
MAX_RECALL_CHARS = 1500

#: How many recent daily files to include in the corpus.
RECENT_DAILY_FILES = 7

_WORD = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _WORD.findall(text.lower())


def _documents() -> List[Tuple[str, str]]:
    """(source label, text) pairs across the memory tree."""
    docs: List[Tuple[str, str]] = []
    mem = _store.read_memory()
    for line in mem.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            docs.append(("memory", line))
    for name in _people.list_people():
        text = _people.get_person(name)
        if text:
            docs.append((f"person:{name}", text))
    try:
        files = sorted(_daily.daily_dir().glob("*.md"), reverse=True)[:RECENT_DAILY_FILES]
    except OSError:
        files = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            docs.append((f"daily:{f.stem}", text))
    return docs


def _tfidf(docs: List[List[str]]) -> Tuple[List[Dict[str, float]], Dict[str, float]]:
    """Return (doc vectors, idf). Vectors are {term: tf*idf}."""
    n = len(docs)
    df: Dict[str, int] = {}
    tfs: List[Dict[str, float]] = []
    for toks in docs:
        counts: Dict[str, int] = {}
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
        total = len(toks) or 1
        tfs.append({t: c / total for t, c in counts.items()})
        for t in counts:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((1 + n) / (1 + c)) + 1.0 for t, c in df.items()}
    vecs = [{t: tf * idf[t] for t, tf in d.items()} for d in tfs]
    return vecs, idf


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def recall(query: str, k: int = 5) -> List[str]:
    """Top-k relevant memory snippets for a query, most relevant first.

    Each item is labeled by source, e.g. "[memory] - 2026-10-18: ...".
    Returns [] for empty queries or empty memory — never invented content.
    """
    query = (query or "").strip()
    if not query:
        return []
    docs = _documents()
    if not docs:
        return []
    k = max(1, k)
    tok_docs = [_tokenize(text) for _, text in docs]
    q_toks = _tokenize(query)
    if not q_toks:
        return []
    vecs, idf = _tfidf(tok_docs)
    q_counts: Dict[str, int] = {}
    for t in q_toks:
        q_counts[t] = q_counts.get(t, 0) + 1
    q_total = len(q_toks)
    q_vec = {t: (c / q_total) * idf.get(t, 1.0) for t, c in q_counts.items()}
    scored = sorted(
        ((_cosine(q_vec, v), i) for i, v in enumerate(vecs)),
        key=lambda s: s[0],
        reverse=True,
    )
    out = []
    for score, i in scored[:k]:
        if score <= 0.0:
            break
        label, text = docs[i]
        out.append(f"[{label}] {text[:400]}")
    return out
