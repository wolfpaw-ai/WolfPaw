"""Pure-unit tests for the prompt-context block formatters in
`memory.conversational`. No DB / model client needed."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from wolfpaw.memory import conversational as conv


def test_format_summaries_block_renders_levels_distinctly():
    summaries = [
        conv.ThreadSummary(
            id=uuid4(), thread_id=uuid4(), level=2,
            summary_md="older fold",
            range_start_message_id=None, range_end_message_id=None,
            created_at=datetime.now(timezone.utc),
        ),
        conv.ThreadSummary(
            id=uuid4(), thread_id=uuid4(), level=1,
            summary_md="newer window",
            range_start_message_id=None, range_end_message_id=None,
            created_at=datetime.now(timezone.utc),
        ),
    ]
    block = conv.format_summaries_block(summaries)
    assert "Older summary" in block
    assert "older fold" in block
    assert "Earlier window" in block
    assert "newer window" in block


def test_format_summaries_block_empty_returns_empty_string():
    assert conv.format_summaries_block([]) == ""


def test_format_vector_recall_block_truncates_long_content():
    long_msg = conv.Message(
        id=uuid4(), thread_id=uuid4(), role="user",
        content="x" * 1000, metadata={},
        created_at=datetime.now(timezone.utc),
    )
    block = conv.format_vector_recall_block([long_msg])
    assert "Possibly-relevant" in block
    assert "..." in block


def test_format_vector_recall_block_empty_returns_empty_string():
    assert conv.format_vector_recall_block([]) == ""
