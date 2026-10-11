import pytest

from agent.state import HistoryEntry, State, load_state, save_state


def test_missing_file_gives_empty_state(tmp_path):
    state = load_state(tmp_path / "nope.json")
    assert state.next_topic_index == 0 and state.history == []


def test_round_trip(tmp_path):
    path = tmp_path / "state.json"
    state = State(next_topic_index=3)
    state.add(HistoryEntry(date="2026-09-17", slot="09:00", topic="AI agents", status="posted"))
    save_state(state, path)

    loaded = load_state(path)
    assert loaded.next_topic_index == 3
    assert loaded.history[0].topic == "AI agents"


def test_already_handled():
    state = State()
    state.add(HistoryEntry(date="2026-09-17", slot="09:00", topic="x", status="failed"))
    assert not state.already_handled("2026-09-17", "09:00")  # failed -> retry allowed
    state.add(HistoryEntry(date="2026-09-17", slot="09:00", topic="x", status="posted"))
    assert state.already_handled("2026-09-17", "09:00")


def test_invalid_status():
    with pytest.raises(ValueError):
        State().add(HistoryEntry(date="d", slot="s", topic="t", status="oops"))


# --- writing must never leave a half-written file ----------------------------

def test_a_crash_mid_write_leaves_the_old_file_intact(tmp_path, monkeypatch):
    """The whole point of writing to a temp file first."""
    import json as _json

    from agent import jsonio

    target = tmp_path / "state.json"
    target.write_text('{"keep": "me"}', encoding="utf-8")

    def die(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(jsonio.os, "replace", die)
    with pytest.raises(OSError):
        jsonio.write_json(target, {"new": "data"})

    assert _json.loads(target.read_text()) == {"keep": "me"}
    assert list(tmp_path.glob(".*tmp")) == []          # and no litter left behind


def test_a_locked_destination_is_retried(tmp_path, monkeypatch):
    """Windows anti-virus can hold a file open for a moment; that is not a
    reason to lose the write."""
    from agent import jsonio

    target = tmp_path / "state.json"
    attempts = []
    real = jsonio.os.replace

    def flaky(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("being scanned")
        real(src, dst)

    monkeypatch.setattr(jsonio.os, "replace", flaky)
    monkeypatch.setattr(jsonio.time, "sleep", lambda _s: None)

    jsonio.write_json(target, {"ok": True})

    assert len(attempts) == 3
    assert target.exists()
