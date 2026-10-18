"""Tests for PRANA P2 personality modes."""

from prana import events, personality
from prana.personality import MODES, assemble_system_prompt, mode_block, resolve_mode


def _prompt(**kw):
    return assemble_system_prompt(
        identity="You are PRANA.",
        tools=[],
        event=events.user_message("hi"),
        **kw,
    )


def test_each_mode_changes_prompt():
    texts = {m: _prompt(mode=m) for m in MODES}
    # Every mode's register instruction appears in its own prompt...
    for m, text in texts.items():
        assert MODES[m].split(".")[0] in text
    # ...and modes differ from each other.
    assert len(set(texts.values())) == len(MODES)


def test_default_mode_is_casual():
    assert _prompt() == _prompt(mode="casual")
    assert "sharp friend" in _prompt()


def test_mode_order_in_assembly():
    text = _prompt(mode="teacher", memory_summary="[memory] likes mangoes")
    i_ident = text.index("You are PRANA.")
    i_mode = text.index("Mode: teacher")
    i_mem = text.index("What you remember")
    i_rules = text.index("Operating rules")
    i_tools = text.index("Available tools")
    i_event = text.index("Current event")
    assert i_ident < i_mode < i_mem < i_rules < i_tools < i_event


def test_no_memory_section_when_empty():
    assert "What you remember" not in _prompt(mode="engineer")


def test_resolve_mode_cli_wins():
    assert resolve_mode("teacher", "mode: engineer\nYou are X") == "teacher"


def test_resolve_mode_from_identity_file():
    assert resolve_mode(None, "# Soul\nmode: strategist\nBe bold.") == "strategist"


def test_resolve_mode_default_and_unknown():
    assert resolve_mode(None, "no mode line here") == "casual"
    assert resolve_mode("pirate", "") == "casual"
    assert resolve_mode(None, "mode: pirate") == "casual"


def test_mode_block_unknown_falls_back():
    assert mode_block("pirate") == MODES["casual"]


def test_teacher_engineer_registers():
    teacher = _prompt(mode="teacher")
    engineer = _prompt(mode="engineer")
    assert "step" in teacher.lower()
    assert "verify" in engineer.lower()
    assert teacher != engineer
