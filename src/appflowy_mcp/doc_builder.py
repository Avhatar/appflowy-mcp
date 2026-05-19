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
