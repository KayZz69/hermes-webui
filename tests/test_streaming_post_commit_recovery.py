"""Regression coverage for errors after successful stream writeback."""

import api.streaming as streaming


def test_post_commit_failure_closes_stream_without_error_marker():
    events = []

    try:
        raise TypeError("'NoneType' object is not iterable")
    except TypeError:
        streaming._emit_post_commit_stream_recovery(
            lambda event, payload: events.append((event, payload)),
            "session-1",
            "stream-1",
        )

    assert events == [("stream_end", {"session_id": "session-1"})]
    assert not any(event == "apperror" for event, _ in events)


def test_post_commit_recovery_does_not_mask_queue_failure():
    def broken_put(event, payload):
        raise RuntimeError("queue closed")

    # A client disconnect or closed queue must not turn recovery into a second
    # exception that bypasses the streaming worker's normal teardown.
    streaming._emit_post_commit_stream_recovery(broken_put, "session-1", "stream-1")