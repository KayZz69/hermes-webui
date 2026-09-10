"""Generated-image tool results keep a local artifact reference.

Codex/openai-codex image generation stores the deliverable in the Hermes cache
and returns the absolute path in the JSON ``image`` field.  The WebUI's compact
``session.tool_calls`` records previously reduced that result to truncated JSON,
so a settled chat exposed only raw text/no usable image.  The streaming layer now
prefers the image path and appends the established ``MEDIA:`` reference;
``static/ui.js`` then renders that reference through the existing authenticated
``/api/media`` image path in the open tool card.
"""

from __future__ import annotations

import json

from api.streaming import _extract_tool_calls_from_messages, _tool_result_snippet


def _image_payload(path: str = "/root/.hermes/cache/images/generated.png") -> dict:
    return {
        "success": True,
        "image": path,
        "modality": "text",
        "provider": "openai-codex",
    }


def test_tool_result_snippet_prefers_generated_image_media_reference():
    result = _tool_result_snippet(json.dumps(_image_payload()))

    assert result.startswith("/root/.hermes/cache/images/generated.png\n")


def test_extracted_image_generate_tool_call_preserves_media_reference():
    tid = "call-image-1"
    image = "/root/.hermes/cache/images/generated.png"
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": tid,
                    "function": {
                        "name": "image_generate",
                        "arguments": json.dumps({"prompt": "a snow scene"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": tid,
            "content": json.dumps(_image_payload(image)),
        },
    ]

    calls = _extract_tool_calls_from_messages(messages)

    assert len(calls) == 1
    assert calls[0]["name"] == "image_generate"
    assert f"{image}\nMEDIA:{image}" == calls[0]["snippet"]


def test_failed_image_generate_result_does_not_mint_media_reference():
    result = _tool_result_snippet(
        json.dumps(
            {
                "success": False,
                "image": None,
                "error": "provider unavailable",
                "error_type": "api_error",
            }
        )
    )

    assert result == "provider unavailable"
    assert "MEDIA:" not in result
