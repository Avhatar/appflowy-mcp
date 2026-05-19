"""Parse a single line of markdown into a list of formatted text runs.

Output: list[(text_chunk, attrs_dict)] where attrs_dict is empty for plain text
or maps to AppFlowy-known attrs: bold, italic, strikethrough, code, href.

Limitations (v1):
- No nested formatting (e.g. **bold *italic*** comes out as one bold span only).
- Earliest match across patterns wins; ties broken by pattern priority order.
"""
import re

# (pattern, attrs_builder). attrs_builder takes the match, returns (text, attrs).
_INLINE_PATTERNS: list[tuple[re.Pattern, callable]] = [
    # `code` — content not interpreted further
    (re.compile(r"`([^`\n]+)`"),
     lambda m: (m.group(1), {"code": True})),
    # [text](url)
    (re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)"),
     lambda m: (m.group(1), {"href": m.group(2)})),
    # **bold**
    (re.compile(r"\*\*([^*\n]+)\*\*"),
     lambda m: (m.group(1), {"bold": True})),
    # __bold__
    (re.compile(r"__([^_\n]+)__"),
     lambda m: (m.group(1), {"bold": True})),
    # *italic* — but not part of bold (handled above by ordering)
    (re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])"),
     lambda m: (m.group(1), {"italic": True})),
    # _italic_ — guard against inside-word underscores
    (re.compile(r"(?<![_\w])_([^_\n]+)_(?![_\w])"),
     lambda m: (m.group(1), {"italic": True})),
    # ~~strikethrough~~
    (re.compile(r"~~([^~\n]+)~~"),
     lambda m: (m.group(1), {"strikethrough": True})),
]


def parse_inline(text: str) -> list[tuple[str, dict]]:
    if not text:
        return []

    out: list[tuple[str, dict]] = []
    pos = 0
    n = len(text)

    while pos < n:
        # Find earliest match across all patterns
        best: tuple[int, int, str, dict] | None = None
        for pat, builder in _INLINE_PATTERNS:
            m = pat.search(text, pos)
            if m and (best is None or m.start() < best[0]):
                chunk, attrs = builder(m)
                best = (m.start(), m.end(), chunk, attrs)

        if best is None:
            out.append((text[pos:], {}))
            break

        start, end, chunk, attrs = best
        if start > pos:
            out.append((text[pos:start], {}))
        out.append((chunk, attrs))
        pos = end

    return out


def has_formatting(runs: list[tuple[str, dict]]) -> bool:
    return any(attrs for _, attrs in runs)
