"""Tests for PRANA P2 memory: store, people, daily, TF-IDF recall, tools.

Memory root is redirected per-test via the PRANA_MEMORY_DIR env override,
so no test touches the real ~/.prana.
"""

import os
from pathlib import Path

import pytest

from prana.memory import daily, people, recall, store
from prana.tools import ToolRegistry
from prana.tools.base import ToolCall
from prana.tools.memory_tools import builtin_memory_tools


@pytest.fixture()
def memdir(tmp_path, monkeypatch):
    d = tmp_path / "memory"
    monkeypatch.setenv(store.ENV_OVERRIDE, str(d))
    return d


# ---- store ----

def test_read_empty_memory(memdir):
    assert store.read_memory() == ""


def test_append_fact_dated_line(memdir):
    out = store.append_fact("Prabh prefers Hinglish")
    assert out.startswith("remembered:")
    text = store.read_memory()
    assert "Prabh prefers Hinglish" in text
    assert text.startswith("# PRANA long-term memory")


def test_update_memory_replaces(memdir):
    store.append_fact("old fact")
    store.update_memory("brand new curated memory")
    text = store.read_memory()
    assert "brand new curated memory" in text
    assert "old fact" not in text


def test_credential_write_refused(memdir):
    out = store.append_fact("my api key is sk-ant-abc123XYZ789")
    assert out.startswith("refused:")
    assert store.read_memory() == ""  # nothing persisted


def test_credential_overwrite_refused(memdir):
    store.append_fact("safe fact")
    out = store.update_memory("leaked: AIzaSyD-EXAMPLEKEY123456789")
    assert out.startswith("refused:")
    assert "safe fact" in store.read_memory()


def test_empty_fact_ignored(memdir):
    assert "ignored" in store.append_fact("   ")
    assert store.read_memory() == ""


# ---- people ----

def test_people_roundtrip(memdir):
    people.save_person("Ivan", "Founder of XERJ. Emailed about Corpus Hub.")
    assert "XERJ" in people.get_person("Ivan")
    assert people.list_people() == ["Ivan"]


def test_people_missing_returns_empty(memdir):
    assert people.get_person("Nobody") == ""
    assert people.list_people() == []


def test_people_name_traversal_safe(memdir):
    people.save_person("../../etc", "evil note")
    names = people.list_people()
    assert names and all("/" not in n and n not in (".", "..") for n in names)
    # The file must live inside the people dir, nowhere else.
    files = list((memdir / "people").glob("*.md"))
    assert len(files) == 1
    assert files[0].parent == memdir / "people"
    # Original traversal-y name is still retrievable via its sanitized form.
    assert "evil note" in people.get_person("../../etc")


def test_people_bad_name_rejected(memdir):
    # ".." sanitizes to "__" — safe and stored inside the people dir.
    people.save_person("..", "dots")
    assert people.list_people() == ["__"]
    assert "dots" in people.get_person("..")
    # Truly empty names are rejected.
    with pytest.raises(ValueError):
        people.save_person("   ", "x")
    with pytest.raises(ValueError):
        people.save_person("", "x")


# ---- daily ----

def test_daily_log(memdir):
    out = daily.log_today("shipped PRANA P2")
    assert out == "logged"
    from datetime import date
    f = memdir / "daily" / f"{date.today().isoformat()}.md"
    assert f.exists()
    assert "shipped PRANA P2" in f.read_text()


def test_daily_empty_ignored(memdir):
    assert "ignored" in daily.log_today("  ")


# ---- recall ----

def test_recall_ranks_relevant_first(memdir):
    store.append_fact("Prabh's favourite fruit is the mango, eaten every summer")
    store.append_fact("The capital of France is Paris, a city of lights")
    hits = recall.recall("which fruit does Prabh like best?")
    assert hits, "expected recall hits"
    assert "mango" in hits[0].lower()


def test_recall_empty_memory(memdir):
    assert recall.recall("anything at all") == []


def test_recall_empty_query(memdir):
    store.append_fact("some fact")
    assert recall.recall("   ") == []


def test_recall_searches_people_and_daily(memdir):
    people.save_person("Ivan", "Ivan runs XERJ, an AI search engine startup")
    hits = recall.recall("who runs the AI search startup?")
    assert any("person:Ivan" in h for h in hits)


def test_recall_never_invents(memdir):
    # Unrelated query against unrelated memory → no hits, not filler.
    store.append_fact("Prabh likes cricket on Sundays")
    assert recall.recall("quantum chromodynamics lattice gauge") == []


# ---- tools via registry ----

def _mem_registry():
    reg = ToolRegistry()
    for t in builtin_memory_tools():
        reg.register(t)
    return reg


def test_remember_tool_writes(memdir):
    reg = _mem_registry()
    res = reg.dispatch(ToolCall(id="1", name="remember",
                               arguments={"fact": "Prabh is building PRANA"}))
    assert not res.is_error
    assert "remembered" in res.output
    assert "building PRANA" in store.read_memory()


def test_remember_tool_refuses_credential(memdir):
    reg = _mem_registry()
    res = reg.dispatch(ToolCall(id="1", name="remember",
                               arguments={"fact": "token sk-abc123DEF456"}))
    assert not res.is_error  # the tool answers; the STORE refused
    assert "refused" in res.output
    assert store.read_memory() == ""


def test_recall_tool_reads(memdir):
    store.append_fact("Prabh's dog is named Bruno")
    reg = _mem_registry()
    res = reg.dispatch(ToolCall(id="1", name="recall",
                               arguments={"query": "what is the dog called?"}))
    assert not res.is_error
    assert "Bruno" in res.output


def test_recall_tool_empty_store(memdir):
    reg = _mem_registry()
    res = reg.dispatch(ToolCall(id="1", name="recall",
                               arguments={"query": "anything"}))
    assert not res.is_error
    assert "no relevant memories" in res.output


def test_remember_tool_needs_no_approval(memdir):
    # Side-effect class is WRITE (auto), not IRREVERSIBLE (prompt).
    from prana.tools.base import SideEffect
    tools = {t.name: t for t in builtin_memory_tools()}
    assert tools["remember"].side_effect == SideEffect.WRITE
    assert tools["recall"].side_effect == SideEffect.READ
    # Dispatch works with a registry whose approver would explode if called.
    def boom(name, args):
        raise AssertionError("approver must not be called for remember/recall")
    reg = ToolRegistry(approver=boom)
    for t in builtin_memory_tools():
        reg.register(t)
    reg.dispatch(ToolCall(id="1", name="remember", arguments={"fact": "x"}))
    reg.dispatch(ToolCall(id="1", name="recall", arguments={"query": "x"}))
