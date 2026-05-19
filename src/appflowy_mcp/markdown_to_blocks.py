"""Parse markdown into AppFlowy block list.

Supported (minimal v1):
- # Headings (levels 1-6)
- Plain paragraphs
- - / * bulleted lists  (any depth via indent)
- 1. 2. numbered lists
- - [ ] / - [x] todo
- > quote
- ```lang ... ``` fenced code
- --- divider

Block dict shape (matches what AppFlowy stores):
    {
      "id": str,         "ty": str,
      "data": dict,      (will be JSON-serialized when written)
      "text": str,       (plain text content; None if no text)
      "children": [Block, ...]
    }
"""
import re
import uuid
from typing import Any


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLETED = re.compile(r"^(\s*)[-*]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)\d+\.\s+(.*)$")
_TODO = re.compile(r"^(\s*)-\s+\[( |x|X)\]\s*(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_DIVIDER = re.compile(r"^---+$")
_FENCE = re.compile(r"^```\s*([\w+-]*)\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?(\s*\|\s*:?-{3,}:?)*\s*\|?\s*$")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    # Split on `|` but not on `\|` (escaped pipe), then unescape.
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n and s[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def _cell_align(cell: str) -> str | None:
    cell = cell.strip()
    left = cell.startswith(":")
    right = cell.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    if left:
        return "left"
    return None


def _parse_table(lines: list[str], start: int) -> tuple[dict[str, Any], int] | None:
    """If lines[start..] looks like a markdown table, build the nested block.

    Returns (table_block, next_line_index) or None.
    """
    n = len(lines)
    if start + 1 >= n:
        return None
    if not _TABLE_ROW.match(lines[start]):
        return None
    if not _TABLE_SEP.match(lines[start + 1]):
        return None

    header_cells = _split_row(lines[start])
    sep_cells = _split_row(lines[start + 1])
    aligns = [_cell_align(c) for c in sep_cells]
    col_count = max(len(header_cells), len(sep_cells))

    data_rows: list[list[str]] = [header_cells]
    i = start + 2
    while i < n and _TABLE_ROW.match(lines[i]):
        data_rows.append(_split_row(lines[i]))
        i += 1

    # rowsLen / colsLen are required by AppFlowy UI to render the table.
    table_block = _block("simple_table", data={
        "rowsLen": len(data_rows),
        "colsLen": col_count,
    })
    for row_index, cells in enumerate(data_rows):
        row_block = _block("simple_table_row")
        for col_index in range(col_count):
            cell_text = cells[col_index] if col_index < len(cells) else ""
            cell_data: dict[str, Any] = {
                "rowPosition": row_index,
                "colPosition": col_index,
            }
            align = aligns[col_index] if col_index < len(aligns) else None
            if align:
                cell_data["align"] = align
            cell_block = _block("simple_table_cell", data=cell_data)
            # Each cell holds a paragraph with the text (matching AppFlowy importer).
            cell_block["children"].append(_block("paragraph", text=cell_text))
            row_block["children"].append(cell_block)
        table_block["children"].append(row_block)
    return table_block, i


def _block(ty: str, text: str | None = None, data: dict[str, Any] | None = None,
           children: list[dict] | None = None) -> dict[str, Any]:
    return {
        "id": _new_id("block"),
        "ty": ty,
        "data": data or {},
        "text": text,
        "children": children or [],
    }


def parse(markdown: str) -> list[dict[str, Any]]:
    """Parse markdown into a flat list of top-level blocks (with nested children)."""
    lines = markdown.splitlines()
    blocks: list[dict[str, Any]] = []
    i = 0
    n = len(lines)
    list_stacks: list[tuple[int, list[dict]]] = []  # (indent, parent_children_list)

    def flush_lists_above(indent: int) -> None:
        while list_stacks and list_stacks[-1][0] >= indent:
            list_stacks.pop()

    def append_block(b: dict, indent: int = 0) -> None:
        if indent == 0 or not list_stacks:
            blocks.append(b)
        else:
            list_stacks[-1][1].append(b)

    while i < n:
        line = lines[i]

        if line.strip() == "":
            i += 1
            continue

        # Table: needs lookahead for separator line, handle before paragraph fallback
        if _TABLE_ROW.match(line):
            parsed = _parse_table(lines, i)
            if parsed is not None:
                table_block, next_i = parsed
                flush_lists_above(0)
                append_block(table_block)
                i = next_i
                continue

        # Fenced code (multi-line) — handle before single-line patterns
        m = _FENCE.match(line)
        if m:
            lang = m.group(1) or ""
            i += 1
            buf: list[str] = []
            while i < n and not _FENCE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            if i < n:  # consume closing fence
                i += 1
            flush_lists_above(0)
            append_block(_block("code", text="\n".join(buf), data={"language": lang}))
            continue

        # Divider
        if _DIVIDER.match(line.strip()):
            flush_lists_above(0)
            append_block(_block("divider"))
            i += 1
            continue

        # Heading
        m = _HEADING.match(line)
        if m:
            flush_lists_above(0)
            level = len(m.group(1))
            append_block(_block("heading", text=m.group(2), data={"level": level}))
            i += 1
            continue

        # Todo (more specific than bulleted; check first)
        m = _TODO.match(line)
        if m:
            indent = len(m.group(1)) // 2  # 2-space indent step
            checked = m.group(2).lower() == "x"
            text = m.group(3)
            flush_lists_above(indent + 1)
            b = _block("todo_list", text=text, data={"checked": checked})
            if list_stacks and list_stacks[-1][0] == indent:
                list_stacks[-1][1].append(b)
            else:
                append_block(b, indent)
            list_stacks.append((indent + 1, b["children"]))
            i += 1
            continue

        # Bulleted list
        m = _BULLETED.match(line)
        if m:
            indent = len(m.group(1)) // 2
            text = m.group(2)
            flush_lists_above(indent + 1)
            b = _block("bulleted_list", text=text)
            if list_stacks and list_stacks[-1][0] == indent:
                list_stacks[-1][1].append(b)
            else:
                append_block(b, indent)
            list_stacks.append((indent + 1, b["children"]))
            i += 1
            continue

        # Numbered list
        m = _NUMBERED.match(line)
        if m:
            indent = len(m.group(1)) // 2
            text = m.group(2)
            flush_lists_above(indent + 1)
            b = _block("numbered_list", text=text)
            if list_stacks and list_stacks[-1][0] == indent:
                list_stacks[-1][1].append(b)
            else:
                append_block(b, indent)
            list_stacks.append((indent + 1, b["children"]))
            i += 1
            continue

        # Quote
        m = _QUOTE.match(line)
        if m:
            flush_lists_above(0)
            append_block(_block("quote", text=m.group(1)))
            i += 1
            continue

        # Plain paragraph (joins consecutive non-empty plain lines)
        flush_lists_above(0)
        buf = [line.rstrip()]
        i += 1
        while i < n:
            nxt = lines[i]
            if (nxt.strip() == ""
                    or _HEADING.match(nxt) or _BULLETED.match(nxt)
                    or _NUMBERED.match(nxt) or _TODO.match(nxt)
                    or _QUOTE.match(nxt) or _FENCE.match(nxt)
                    or _DIVIDER.match(nxt.strip())):
                break
            buf.append(nxt.rstrip())
            i += 1
        append_block(_block("paragraph", text=" ".join(buf)))

    return blocks
