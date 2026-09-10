"""Settled tool-card previews freeze their artifacts (per-call media snapshots).

The generated-image tool card renders the artifact path from the persisted
``session.tool_calls`` snippet. Without a per-call snapshot, a same-path
rewrite of the underlying file (agent regenerates/overwrites) silently
rewrites the historical card's preview. This suite pins the settle-time
annotation contract: tool calls carrying local ``MEDIA:`` refs gain a
``_media_snapshots`` {path: digest} map from the same content-addressed
store the message annotator uses, with identical idempotency and
deny-parity guarantees.
"""

from __future__ import annotations

import json

import pytest

from api.media_snapshots import (
    annotate_tool_call_snapshots,
    get_snapshot_dir,
    is_valid_digest,
)
from api.streaming import (
    _extract_tool_calls_from_messages,
    _tool_result_snippet,
)

IMAGE_PATH = "/root/.hermes/cache/images/snap-fixture.png"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def snap_dir(tmp_path, monkeypatch):
    store = tmp_path / "media_snapshots"
    monkeypatch.setenv("HERMES_WEBUI_MEDIA_SNAPSHOT_DIR", str(store))
    return store


def _image_tool_call(path: str = IMAGE_PATH, *, snippet: str | None = None) -> dict:
    payload = {"success": True, "image": path, "provider": "openai-codex"}
    return {
        "name": "image_generate",
        "snippet": snippet
        if snippet is not None
        else _tool_result_snippet(json.dumps(payload)),
        "tid": "call-snap-1",
        "assistant_msg_idx": 0,
        "args": {"prompt": "fixture"},
    }


def _allow_all(path):
    return True


def test_settled_tool_call_gains_media_snapshot_map(snap_dir, tmp_path):
    source = tmp_path / "card.png"
    source.write_bytes(PNG_BYTES)
    raw = str(source)
    tc = _image_tool_call(raw)

    captured = annotate_tool_call_snapshots([tc], allowed_predicate=_allow_all)

    assert captured == 1
    snaps = tc["_media_snapshots"]
    # The snippet's MEDIA token for a plain absolute path IS the path text, so
    # the map is keyed by that exact string (the frontend's path= value).
    assert is_valid_digest(snaps[raw])
    stored = get_snapshot_dir() / f"{snaps[raw]}.snap"
    assert stored.read_bytes() == PNG_BYTES


def test_annotation_is_idempotent_across_settles(snap_dir, tmp_path):
    source = tmp_path / "card.png"
    source.write_bytes(PNG_BYTES)
    tc = _image_tool_call(str(source))

    assert annotate_tool_call_snapshots([tc], allowed_predicate=_allow_all) == 1
    first = tc["_media_snapshots"]
    assert annotate_tool_call_snapshots([tc], allowed_predicate=_allow_all) == 0
    assert tc["_media_snapshots"] == first
    assert len(list(snap_dir.glob("*.snap"))) == 1


def test_pinned_digest_survives_inplace_overwrite(snap_dir, tmp_path):
    """THE regression: regenerate/overwrite the same path and the historical
    card keeps the original bytes via the frozen digest."""
    source = tmp_path / "card.png"
    source.write_bytes(PNG_BYTES)
    tc = _image_tool_call(str(source))
    annotate_tool_call_snapshots([tc], allowed_predicate=_allow_all)
    digest = tc["_media_snapshots"][str(source)]

    source.write_bytes(b"v2-completely-different")
    from api.media_snapshots import snapshot_path_for_digest

    stored = snapshot_path_for_digest(digest)
    assert stored is not None
    assert stored.read_bytes() == PNG_BYTES


def test_calls_without_media_refs_are_untouched(snap_dir):
    tc = {"name": "terminal", "snippet": "plain text output", "tid": "t1"}
    assert annotate_tool_call_snapshots([tc], allowed_predicate=_allow_all) == 0
    assert "_media_snapshots" not in tc


def test_non_list_input_is_rejected_cleanly(snap_dir):
    assert annotate_tool_call_snapshots(None) == 0
    assert annotate_tool_call_snapshots("nope") == 0


def test_denied_paths_are_never_captured(snap_dir, tmp_path):
    source = tmp_path / "secret.png"
    source.write_bytes(PNG_BYTES)
    tc = _image_tool_call(str(source))

    def deny(_path):
        return False

    assert annotate_tool_call_snapshots([tc], allowed_predicate=deny) == 0
    assert "_media_snapshots" not in tc


def test_persisted_digest_survives_list_rebuild_across_settles(snap_dir, tmp_path):
    """THE settle-path regression: every turn rebuilds s.tool_calls from scratch,
    discarding the annotated dicts. Without carry-forward by tid, a later re-settle
    re-captures the CURRENT live bytes and rebinds a historical digest — silently
    rewriting history. The previous persisted list must seed FINAL digests.
    """
    source = tmp_path / "card.png"
    source.write_bytes(PNG_BYTES)
    # Turn 1: capture original bytes.
    original_calls = [_image_tool_call(str(source))]
    assert annotate_tool_call_snapshots(original_calls, allowed_predicate=_allow_all) == 1
    original_digest = original_calls[0]["_media_snapshots"][str(source)]

    # The file changes in place (agent regenerated into the same path).
    source.write_bytes(b"v2-new-bytes")

    # Turn 2: production rebuilds summaries from scratch (no stamps), then annotates
    # with the PREVIOUS persisted list passed through.
    rebuilt_calls = [_image_tool_call(str(source))]
    annotate_tool_call_snapshots(
        rebuilt_calls, previous_tool_calls=original_calls, allowed_predicate=_allow_all
    )

    # The rebuilt entry inherits the ORIGINAL digest — history not rebound.
    assert rebuilt_calls[0]["_media_snapshots"][str(source)] == original_digest
    from api.media_snapshots import snapshot_path_for_digest

    assert snapshot_path_for_digest(original_digest).read_bytes() == PNG_BYTES


def test_end_to_end_extract_then_annotate(snap_dir, tmp_path):
    """The settle path: extraction output feeds the annotator directly."""
    source = tmp_path / "card.png"
    source.write_bytes(PNG_BYTES)
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "tid-1", "function": {"name": "image_generate",
             "arguments": json.dumps({"prompt": "p"})}}]},
        {"role": "tool", "tool_call_id": "tid-1",
         "content": json.dumps({"success": True, "image": str(source)})},
    ]
    calls = _extract_tool_calls_from_messages(messages)
    assert "MEDIA:" in calls[0]["snippet"]

    annotate_tool_call_snapshots(calls, allowed_predicate=_allow_all)

    assert str(source) in calls[0]["_media_snapshots"]