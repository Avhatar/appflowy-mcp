"""Build an AppFlowy Y.Doc binary from a list of blocks (the inverse of markdown.py).

Schema (verified against AppFlowy-Collab e59260e):
    Doc.data (Map):
      "document" (Map):
        "page_id" → str
        "blocks" (Map):
          <block_id> (Map) { id, ty, parent, children, data(JSON str), external_id, external_type }
        "meta" (Map):
          "children_map" (Map) { <key> → Array<block_id> }
          "text_map"     (Map) { <key> → Text(plain str or with deltas) }

Output bytes are bincode-serialized `EncodedCollab { state_vector, doc_state, version=V1 }`
ready to be sent as `encoded_collab_v1` to `PUT /api/workspace/{ws}/collab/{object_id}`.
"""
import json
import struct
import uuid
from typing import Any

from pycrdt import Array, Doc, Map, Text

from .inline import has_formatting, parse_inline


def _new_key(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _encode_encoded_collab(state_vector: bytes, doc_state: bytes, version: int = 0) -> bytes:
    """bincode 1.x default config: 8-byte LE length prefix, then bytes, then 1-byte enum tag."""
    out = struct.pack("<Q", len(state_vector)) + state_vector
    out += struct.pack("<Q", len(doc_state)) + doc_state
    out += struct.pack("<B", version)
    return out


def _add_block(
    blocks_map: Map,
    children_map: Map,
    text_map: Map,
    parent_id: str,
    block: dict[str, Any],
) -> str:
    """Insert one block (and its sub-tree) into the Y.Doc maps. Returns its id."""
    block_id = block["id"]
    children_key = _new_key("ch")
    has_text = block.get("text") is not None
    text_key = _new_key("txt") if has_text else ""

    # Build the block Map. Only include external_id/external_type for blocks
    # that actually carry text. AppFlowy treats empty-string external_id as
    # "has text" and tries to render it.
    block_fields: dict[str, Any] = {
        "id": block_id,
        "ty": block["ty"],
        "parent": parent_id,
        "children": children_key,
        "data": json.dumps(block.get("data") or {}),
    }
    if has_text:
        block_fields["external_id"] = text_key
        block_fields["external_type"] = "text"
    blocks_map[block_id] = Map(block_fields)

    # Children ordering: build first, then insert into children_map
    child_ids: list[str] = []
    for child in block.get("children") or []:
        cid = _add_block(blocks_map, children_map, text_map, block_id, child)
        child_ids.append(cid)
    children_map[children_key] = Array(child_ids)

    if text_key:
        raw_text = block.get("text") or ""
        runs = parse_inline(raw_text)
        if has_formatting(runs):
            # Insert plain text first, then format ranges. Insert-with-attrs
            # would inherit formatting onto neighboring runs in Yrs semantics.
            # IMPORTANT: pycrdt Text.format uses UTF-8 BYTE offsets, not chars.
            # For Cyrillic / emoji this matters (1 char ≠ 1 byte).
            text_map[text_key] = Text("")
            t = text_map[text_key]
            plain = "".join(chunk for chunk, _ in runs)
            t.insert(0, plain)
            offset = 0
            for chunk, attrs in runs:
                chunk_bytes = len(chunk.encode("utf-8"))
                if attrs:
                    t.format(offset, offset + chunk_bytes, attrs)
                offset += chunk_bytes
        else:
            text_map[text_key] = Text(raw_text)

    return block_id


def _populate_document(document_map: Map, blocks: list[dict[str, Any]]) -> None:
    """Fill an empty `document` Y.Map with AppFlowy document structure."""
    page_id = _new_key("page")
    page_children_key = _new_key("ch")
    document_map["page_id"] = page_id

    blocks_map = Map({})
    document_map["blocks"] = blocks_map
    # Root `page` block — no text, so omit external_id/external_type.
    blocks_map[page_id] = Map({
        "id": page_id,
        "ty": "page",
        "parent": "",
        "children": page_children_key,
        "data": "{}",
    })

    meta = Map({})
    document_map["meta"] = meta
    children_map = Map({})
    meta["children_map"] = children_map
    text_map = Map({})
    meta["text_map"] = text_map

    top_ids: list[str] = []
    for b in blocks:
        bid = _add_block(blocks_map, children_map, text_map, page_id, b)
        top_ids.append(bid)
    children_map[page_children_key] = Array(top_ids)


def build_document(blocks: list[dict[str, Any]]) -> bytes:
    """Build a complete AppFlowy document Y.Doc and serialize to bincode bytes
    (for `PUT /api/workspace/{ws}/collab/{obj}` — background DB upsert)."""
    doc = Doc()
    data_map = Map({})
    doc["data"] = data_map
    document = Map({})
    data_map["document"] = document
    _populate_document(document, blocks)

    doc_state = doc.get_update()
    state_vector = doc.get_state()
    return _encode_encoded_collab(state_vector, doc_state, version=0)


def _block_plain_text(
    block_id: str, blocks_map: Map, text_map: Map
) -> str:
    """Plain-text contents of a block (formatting stripped).

    Used for heading matching. Reads the block's `external_id` → corresponding
    Y.Text in text_map → concat of all insert chunks (regardless of attrs).
    """
    if block_id not in blocks_map:
        return ""
    block = blocks_map[block_id]
    if "external_id" not in list(block.keys()):
        return ""
    ext_id = block["external_id"]
    if not ext_id or ext_id not in text_map:
        return ""
    runs = text_map[ext_id].diff()
    return "".join(chunk for chunk, _attrs in runs)


def _block_data_dict(block: Map) -> dict[str, Any]:
    if "data" not in list(block.keys()):
        return {}
    raw = block["data"]
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_heading(s: str) -> str:
    return " ".join(s.split()).lower()


def _find_root_headings(
    blocks_map: Map, text_map: Map, root_children: Array, target: str
) -> list[tuple[int, str, int]]:
    """Return all root-level heading blocks matching `target` (case-insensitive,
    whitespace-normalized). Each entry: (index_in_root_children, block_id, level).
    """
    target_norm = _normalize_heading(target)
    out: list[tuple[int, str, int]] = []
    for i, bid in enumerate(list(root_children)):
        if bid not in blocks_map:
            continue
        block = blocks_map[bid]
        if block["ty"] != "heading":
            continue
        text = _block_plain_text(bid, blocks_map, text_map)
        if _normalize_heading(text) == target_norm:
            level = int(_block_data_dict(block).get("level", 1))
            out.append((i, bid, level))
    return out


def _section_end_index(
    blocks_map: Map, root_children: Array, start_index: int, heading_level: int
) -> int:
    """Index of the next root block that ends the section: a heading at level
    ≤ `heading_level`. Returns len(root_children) if no such block.
    """
    children = list(root_children)
    n = len(children)
    for i in range(start_index + 1, n):
        bid = children[i]
        if bid not in blocks_map:
            continue
        block = blocks_map[bid]
        if block["ty"] != "heading":
            continue
        level = int(_block_data_dict(block).get("level", 1))
        if level <= heading_level:
            return i
    return n


def _delete_block_tree(
    block_id: str, blocks_map: Map, children_map: Map, text_map: Map
) -> None:
    """Recursively remove a block and its descendants from all three maps."""
    if block_id not in blocks_map:
        return
    block = blocks_map[block_id]
    keys = list(block.keys())
    if "children" in keys:
        children_key = block["children"]
        if children_key and children_key in list(children_map.keys()):
            for cid in list(children_map[children_key]):
                _delete_block_tree(cid, blocks_map, children_map, text_map)
            del children_map[children_key]
    if "external_id" in keys:
        ext_id = block["external_id"]
        if ext_id and ext_id in list(text_map.keys()):
            del text_map[ext_id]
    del blocks_map[block_id]


def _resolve_match(
    matches: list[tuple[int, str, int]],
    heading: str,
    match_index: int | None,
) -> tuple[tuple[int, str, int] | None, str | None]:
    if not matches:
        return None, f"heading not found: {heading!r}"
    if match_index is None:
        if len(matches) > 1:
            return None, (
                f"multiple matches ({len(matches)}) for heading {heading!r}; "
                "specify match_index (0-based)"
            )
        return matches[0], None
    if match_index < 0 or match_index >= len(matches):
        return None, (
            f"match_index={match_index} out of range; {len(matches)} matches "
            f"found for heading {heading!r}"
        )
    return matches[match_index], None


def replace_section_in_document(
    existing_encoded_collab: bytes,
    heading: str,
    new_blocks: list[dict[str, Any]],
    match_index: int | None = None,
) -> tuple[bytes | None, str | None]:
    """Replace one root-level section (heading + everything until the next
    same-or-higher heading) with new blocks. Returns (encoded_v1, error).

    Matching is case-insensitive and whitespace-normalized. If multiple
    root-level headings match the same text, `match_index` must be specified
    or the call returns an error without writing.

    `new_blocks` is the parsed markdown for the replacement — if it starts
    with a heading at the section level you're replacing, that becomes the
    new section header; if not, the heading is gone. Passing `new_blocks=[]`
    deletes the section entirely.
    """
    doc = Doc()
    doc["data"] = Map({})
    doc.apply_update(existing_encoded_collab)

    document = doc["data"]["document"]
    page_id = document["page_id"]
    blocks_map = document["blocks"]
    meta = document["meta"]
    children_map = meta["children_map"]
    text_map = meta["text_map"]

    page_block = blocks_map[page_id]
    root_children_key = page_block["children"]
    root_children = children_map[root_children_key]

    matches = _find_root_headings(blocks_map, text_map, root_children, heading)
    match, err = _resolve_match(matches, heading, match_index)
    if err is not None or match is None:
        return None, err

    start_index, _heading_id, level = match
    end_index = _section_end_index(blocks_map, root_children, start_index, level)

    # Capture IDs to delete before we mutate the array.
    to_delete = list(root_children)[start_index:end_index]

    # Remove the slot range from the Y.Array — repeatedly delete the leftmost
    # element of the range, since indices shift after each deletion. pycrdt
    # supports __delitem__ on a single index.
    for _ in range(end_index - start_index):
        del root_children[start_index]

    # Free the orphaned block / text / children entries.
    for bid in to_delete:
        _delete_block_tree(bid, blocks_map, children_map, text_map)

    # Insert the new blocks at the same position.
    new_ids: list[str] = []
    for b in new_blocks:
        nid = _add_block(blocks_map, children_map, text_map, page_id, b)
        new_ids.append(nid)
    for offset, nid in enumerate(new_ids):
        root_children.insert(start_index + offset, nid)

    doc_state = doc.get_update()
    state_vector = doc.get_state()
    return _encode_encoded_collab(state_vector, doc_state, version=0), None


def insert_after_heading_in_document(
    existing_encoded_collab: bytes,
    heading: str,
    new_blocks: list[dict[str, Any]],
    match_index: int | None = None,
) -> tuple[bytes | None, str | None]:
    """Insert new blocks immediately after a root-level heading (i.e. at the
    very top of that section). Returns (encoded_v1, error).

    Same matching/ambiguity rules as `replace_section_in_document`.
    """
    doc = Doc()
    doc["data"] = Map({})
    doc.apply_update(existing_encoded_collab)

    document = doc["data"]["document"]
    page_id = document["page_id"]
    blocks_map = document["blocks"]
    meta = document["meta"]
    children_map = meta["children_map"]
    text_map = meta["text_map"]

    page_block = blocks_map[page_id]
    root_children_key = page_block["children"]
    root_children = children_map[root_children_key]

    matches = _find_root_headings(blocks_map, text_map, root_children, heading)
    match, err = _resolve_match(matches, heading, match_index)
    if err is not None or match is None:
        return None, err

    start_index, _heading_id, _level = match
    insert_at = start_index + 1

    new_ids: list[str] = []
    for b in new_blocks:
        nid = _add_block(blocks_map, children_map, text_map, page_id, b)
        new_ids.append(nid)
    for offset, nid in enumerate(new_ids):
        root_children.insert(insert_at + offset, nid)

    doc_state = doc.get_update()
    state_vector = doc.get_state()
    return _encode_encoded_collab(state_vector, doc_state, version=0), None


def append_blocks_to_document(
    existing_encoded_collab: bytes, blocks: list[dict[str, Any]]
) -> bytes:
    """Load an existing document, append `blocks` to the end of the root page,
    and return the re-encoded bincode bytes for `PUT /collab/{obj}`.

    Unlike `build_document`, this preserves the existing Y.Doc state — including
    its block tree, text deltas, and CRDT clocks — and just mutates it by
    inserting new top-level blocks. The wire format on the way out is still a
    full-state `encoded_collab_v1`, because that's what the PUT endpoint
    expects.

    Same WS-conflict caveat as the rest of the write path: if a live editor
    session is open on this page, its next sync can overwrite our upsert with
    its local state. Mitigate by closing all editor sessions before calling.
    """
    doc = Doc()
    doc["data"] = Map({})
    doc.apply_update(existing_encoded_collab)

    document = doc["data"]["document"]
    page_id = document["page_id"]
    blocks_map = document["blocks"]
    meta = document["meta"]
    children_map = meta["children_map"]
    text_map = meta["text_map"]

    page_block = blocks_map[page_id]
    page_children_key = page_block["children"]

    new_ids: list[str] = []
    for b in blocks:
        bid = _add_block(blocks_map, children_map, text_map, page_id, b)
        new_ids.append(bid)

    # Mutate the existing Y.Array of root children — do NOT reassign the slot
    # in children_map (that would replace the array and lose CRDT history).
    root_children = children_map[page_children_key]
    for bid in new_ids:
        root_children.append(bid)

    doc_state = doc.get_update()
    state_vector = doc.get_state()
    return _encode_encoded_collab(state_vector, doc_state, version=0)


def build_replacement_update(
    existing_encoded_collab: bytes, blocks: list[dict[str, Any]]
) -> bytes:
    """Return a Yrs v1 update that — applied to the live AppFlowy realtime
    Y.Doc — replaces its content with `blocks`.

    For `POST /api/workspace/v1/{ws}/collab/{obj}/web-update`.

    Implementation: load existing state, delete the `document` key from `data`,
    then insert a fresh one, then return the FULL doc state (not a sv-delta).
    Sending a full state lets the realtime server merge using Yrs Y.Map
    last-writer semantics regardless of what state it currently holds — this
    survives drift between our snapshot and the live realtime state better
    than a precise delta.
    """
    doc = Doc()
    doc["data"] = Map({})
    if existing_encoded_collab:
        doc.apply_update(existing_encoded_collab)

    data_map = doc["data"]
    if "document" in list(data_map.keys()):
        del data_map["document"]

    document = Map({})
    data_map["document"] = document
    _populate_document(document, blocks)

    # Full state v1 update — pycrdt's get_update() with no state_vector arg
    # returns encode_state_as_update_v1(StateVector::default()).
    return doc.get_update()
