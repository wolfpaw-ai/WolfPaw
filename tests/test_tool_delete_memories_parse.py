"""Pure-logic tests for the `delete_memories` helpers — selection parsing,
clustering, yes-detection. No DB, always run."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from wolfpaw.memory.conversational import Message
from wolfpaw.toolbox.tools.delete_memories import (
    _cluster,
    _episode_label,
    _is_yes,
    _parse_selection,
)


# --- _parse_selection ------------------------------------------------------


def test_parse_plain_numbers():
    assert _parse_selection("1,3", 5) == [1, 3]
    assert _parse_selection("delete 2 and 4", 5) == [2, 4]


def test_parse_all():
    assert _parse_selection("all", 4) == [1, 2, 3, 4]
    assert _parse_selection("delete everything", 3) == [1, 2, 3]


def test_parse_all_except():
    assert _parse_selection("all except 2", 4) == [1, 3, 4]
    assert _parse_selection("all but 8 and 9", 10) == [1, 2, 3, 4, 5, 6, 7, 10]


def test_parse_none_is_explicit_cancel():
    assert _parse_selection("none", 5) == []
    assert _parse_selection("cancel", 5) == []
    assert _parse_selection("no thanks", 5) == []


def test_parse_ambiguous_returns_none():
    # Empty, keep/only-semantics, or unreadable → re-ask (None), never a guess.
    assert _parse_selection("", 5) is None
    assert _parse_selection("keep 8 and 9", 10) is None
    assert _parse_selection("only 1 and 2", 5) is None
    assert _parse_selection("hmm not sure", 5) is None


def test_parse_out_of_range_numbers_dropped():
    # 9 is out of range for a 3-item list → ignored; 2 kept.
    assert _parse_selection("2, 9", 3) == [2]
    # Only out-of-range digits and nothing else parseable → ambiguous.
    assert _parse_selection("42", 3) is None


# --- _is_yes ---------------------------------------------------------------


def test_is_yes():
    for w in ("yes", "Y", "yeah", "confirm", "do it", "ok"):
        assert _is_yes(w)
    for w in ("no", "nope", "maybe", "", "cancel"):
        assert not _is_yes(w)


# --- _cluster / _episode_label ---------------------------------------------


_TID = uuid4()


def _msg(content: str, ts: datetime, tid=_TID) -> Message:
    return Message(
        id=uuid4(), thread_id=tid, role="user",
        content=content, metadata={}, created_at=ts,
    )


def test_cluster_splits_on_time_gap():
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    msgs = [
        _msg("a", base),
        _msg("b", base + timedelta(minutes=5)),        # same episode
        _msg("c", base + timedelta(hours=6)),          # new episode (>2h gap)
        _msg("d", base + timedelta(hours=6, minutes=1)),  # same as c
    ]
    episodes = _cluster(msgs, timedelta(hours=2))
    assert len(episodes) == 2
    assert [len(e["messages"]) for e in episodes] == [2, 2]
    assert episodes[0]["message_ids"] == [msgs[0].id, msgs[1].id]


def test_cluster_splits_on_thread_change():
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    other = uuid4()
    msgs = [
        _msg("a", base),                                  # thread _TID
        _msg("b", base + timedelta(minutes=1)),           # thread _TID
        _msg("c", base + timedelta(minutes=2), tid=other),  # different thread
    ]
    episodes = _cluster(msgs, timedelta(hours=2))
    # Same timeframe, but the thread change forces a new episode.
    assert len(episodes) == 2
    assert [len(e["messages"]) for e in episodes] == [2, 1]


def test_episode_label_has_date_count_snippet():
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    ep = _cluster([_msg("Let's plan the Portugal trip in detail", base)],
                  timedelta(hours=2))[0]
    label = _episode_label(ep)
    assert "May 01, 2026" in label
    assert "1 message" in label
    assert "Portugal" in label
