"""CommonMark-ish → Telegram MarkdownV2 conversion.

Telegram's MarkdownV2 is *not* a superset of CommonMark — every literal
`_ * [ ] ( ) ~ ` > # + - = | { } . !` outside of formatting markup must
be backslash-escaped, or the API rejects the message with `Bad Request:
can't parse entities`. The agent's synthesis prompt asks for plain
markdown, so naively forwarding its output as MarkdownV2 breaks any
message with a paragraph-ending period or a parenthetical.

This module converts the common subset the agent actually produces:

    `code`            → `code`
    ```block```       → ```block```
    **bold**          → *bold*
    *italic* (single) → _italic_
    _italic_          → _italic_
    [text](url)       → [escaped text](escaped url)

Everything else is treated as literal text and escaped. Patterns we
intentionally do NOT support: nested bold/italic, lists (rendered as
plain lines with escaped `-`), headings (escaped `#`), tables, images,
strikethrough beyond `~text~`. The agent's output is short-form prose,
not a formatted document; for richer rendering switch to HTML mode.
"""

from __future__ import annotations

import re

# Per the Bot API docs:
# https://core.telegram.org/bots/api#markdownv2-style
_RESERVED = r"_*[]()~`>#+-=|{}.!"


def _escape(text: str) -> str:
    return "".join(("\\" + ch) if ch in _RESERVED else ch for ch in text)


# Order matters: greedy multi-char patterns first so a `**` doesn't get
# eaten by the single-`*` italic rule.
_TOKEN_PATTERNS = [
    # ```optional lang\ncode``` — triple-backtick block, possibly multiline.
    ("pre", re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)),
    # ```inline``` — single-line triple backtick (rare but appears).
    ("pre_inline", re.compile(r"```(.*?)```", re.DOTALL)),
    # `code` — single backticks. Non-greedy to avoid swallowing siblings.
    ("code", re.compile(r"`([^`\n]+)`")),
    # [text](url) — links. Both halves get escaped.
    ("link", re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")),
    # **bold** — must run before single-`*` so `**x**` isn't read as `*` + `*x*` + `*`.
    ("bold_star", re.compile(r"\*\*([^*\n]+)\*\*")),
    # *italic* — single stars. Bold patterns above already consumed `**…**`.
    ("italic_star", re.compile(r"\*([^*\n]+)\*")),
    # _italic_ — already MarkdownV2-shaped, just need to escape inside.
    ("italic_under", re.compile(r"_([^_\n]+)_")),
]


def _render_token(kind: str, m: re.Match) -> str:
    if kind == "pre":
        # Inside ``` blocks, only ` and \ need escaping per the spec.
        lang = m.group(1)
        body = m.group(2).replace("\\", "\\\\").replace("`", "\\`")
        return f"```{lang}\n{body}```"
    if kind == "pre_inline":
        body = m.group(1).replace("\\", "\\\\").replace("`", "\\`")
        return f"```{body}```"
    if kind == "code":
        body = m.group(1).replace("\\", "\\\\").replace("`", "\\`")
        return f"`{body}`"
    if kind == "link":
        text = _escape(m.group(1))
        # In links, ) and \ inside the URL need backslash-escaping; we
        # already escape \ above, so just handle ).
        url = m.group(2).replace("\\", "\\\\").replace(")", "\\)")
        return f"[{text}]({url})"
    if kind in ("bold_star", "italic_star", "italic_under"):
        body = _escape(m.group(1))
        marker = "*" if kind == "bold_star" else "_"
        return f"{marker}{body}{marker}"
    raise AssertionError(f"unknown token kind: {kind}")


def to_markdown_v2(text: str) -> str:
    """Convert agent-flavored markdown into Telegram MarkdownV2.

    The conversion is single-pass: each pattern is searched in priority
    order, the earliest match wins, and its substitution skips past the
    consumed span so siblings further along the string remain available.
    Falls back to character-level escaping for any text the patterns
    don't claim. Safe to call on plain text (returns escaped plain
    text)."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        best: tuple[int, str, re.Match] | None = None
        for kind, rx in _TOKEN_PATTERNS:
            m = rx.search(text, i)
            if m is None:
                continue
            if best is None or m.start() < best[0]:
                best = (m.start(), kind, m)
        if best is None:
            out.append(_escape(text[i:]))
            break
        start, kind, m = best
        if start > i:
            out.append(_escape(text[i:start]))
        out.append(_render_token(kind, m))
        i = m.end()
    return "".join(out)
