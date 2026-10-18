"""Tests for prana.tokens — truncation, estimation, history compaction."""

from prana import tokens


def test_estimate_tokens_basic():
    assert tokens.estimate_tokens("") == 0
    assert tokens.estimate_tokens("abcd") == 1  # 4 chars -> 1
    assert tokens.estimate_tokens("a" * 400) == 100


def test_truncate_short_unchanged():
    text = "hello world"
    assert tokens.truncate_output(text) == text


def test_truncate_long_keeps_head_and_tail():
    text = "x" * 10000
    out = tokens.truncate_output(text, max_chars=1000)
    assert len(out) <= 1100
    assert "truncated" in out
    assert out.startswith("x" * 100)  # head kept
    assert out.rstrip().endswith("x" * 50) or out.endswith("x" * 10)


def test_truncate_marks_omitted_count():
    text = "y" * 5000
    out = tokens.truncate_output(text, max_chars=1000)
    # omitted = 5000 - (head+tail kept); just check the marker exists and is numeric
    import re
    m = re.search(r"truncated (\d+) chars", out)
    assert m and int(m.group(1)) > 0


def test_compact_history_under_budget_unchanged():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert tokens.compact_history(msgs, max_tokens=10000) == msgs


def test_compact_history_keeps_system_and_recent():
    big = "z" * 4000  # ~1000 tokens each
    msgs = [{"role": "system", "content": "sys"}]
    msgs += [{"role": "user", "content": f"turn{i} {big}"} for i in range(6)]
    out = tokens.compact_history(msgs, max_tokens=2500)
    assert out[0]["role"] == "system"
    # most recent turn survived
    assert any("turn5" in m["content"] for m in out)
    # a placeholder notes the compaction
    assert any("compacted" in m["content"] for m in out)
    total = sum(tokens.estimate_tokens(m["content"]) for m in out)
    assert total <= 2500 + 200  # placeholder slack


def test_scrub_secrets():
    assert tokens.scrub_secrets("bad key sk-ant-abc123 here") == "bad key [REDACTED] here"
    assert tokens.scrub_secrets("key=AIzaSyD-xyz_99") == "key=[REDACTED]"
    assert tokens.scrub_secrets("nothing secret") == "nothing secret"
    assert tokens.scrub_secrets("") == ""
