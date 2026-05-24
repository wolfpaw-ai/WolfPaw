"""Unit tests for the MarkdownV2 converter used by the Telegram channel."""

from __future__ import annotations

from wolfpaw.channels.telegram_markdown import to_markdown_v2


def test_plain_text_escapes_all_reserved_chars():
    """Bare text with no formatting markup must have every reserved
    character backslash-escaped so the Bot API accepts it."""
    out = to_markdown_v2("Hello (world). How are you!")
    assert out == "Hello \\(world\\)\\. How are you\\!"


def test_bold_double_star_becomes_single_star():
    """Agent's `**bold**` is CommonMark; MarkdownV2 wants single `*`."""
    out = to_markdown_v2("This is **bold** text.")
    assert out == "This is *bold* text\\."


def test_inline_code_preserved_with_internal_backtick_escape():
    out = to_markdown_v2("Run `ls -la` to list.")
    # Backslash escapes `-` inside the code span per the spec.
    assert "`ls -la`" in out
    assert out.endswith("list\\.")


def test_triple_backtick_block_with_language_preserved():
    src = "Some code:\n```python\nx = 1 + 2\n```\nDone."
    out = to_markdown_v2(src)
    assert "```python\nx = 1 + 2\n```" in out
    assert out.endswith("Done\\.")


def test_link_text_and_url_escaped():
    out = to_markdown_v2("See [the docs](https://example.com/help).")
    # Inside (url), only `)` and `\` need escaping per the spec —
    # `.` inside the URL stays literal. Text outside the link still
    # gets the `.` escaped.
    assert "[the docs](https://example.com/help)" in out
    assert out.endswith("\\.")


def test_italic_with_underscores_preserved():
    out = to_markdown_v2("This is _italic_ for you.")
    assert "_italic_" in out
    assert out.endswith("for you\\.")


def test_italic_with_single_star_converted_to_underscore():
    out = to_markdown_v2("This is *italic* text.")
    assert "_italic_" in out


def test_mixed_formatting_in_one_line():
    """A realistic agent reply: bold + code + a parenthetical."""
    src = "The answer is **42** (computed via `sum([1, 41])`)."
    out = to_markdown_v2(src)
    assert "*42*" in out
    assert "`sum([1, 41])`" in out
    # Parens around the explanation are escaped since they're outside markup.
    assert "\\(computed via" in out
    assert out.endswith("\\)\\.")


def test_empty_string_stays_empty():
    assert to_markdown_v2("") == ""


def test_does_not_explode_on_unbalanced_markers():
    """Half-open `**bold` (no closing) → treated as literal `*` chars."""
    out = to_markdown_v2("not really **bold")
    # All `*` are escaped because no matching closer was found.
    assert "\\*\\*bold" in out
