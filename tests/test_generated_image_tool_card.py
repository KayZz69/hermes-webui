"""Frontend behavior for generated-image tool cards.

This drives the actual ``buildToolCard`` in ``static/ui.js`` through Node, using
a small DOM stand-in plus the real helper/policy constants.  It pins two
contracts:

* successful ``image_generate`` snippets expose the local artifact as a real
  ``MEDIA:`` reference rendered through the existing ``/api/media`` image path;
* failed/running/non-image tools do not mint an image preview from untrusted
  content.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_IMAGE_PATH = "/root/.hermes/cache/images/openai_codex_gpt-image-2-medium_20260910.png"

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

class Element {
  constructor(tag){ this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attrs = {}; this.style = {}; }
  createElement(tag){ return new Element(tag); }
  setAttribute(name, value){ this.attrs[name] = String(value); }
  getAttribute(name){ return this.attrs[name]; }
  removeAttribute(name){ delete this.attrs[name]; }
}
const rowProto = Element.prototype;
global.document = { createElement: tag => new Element(tag), baseURI: 'http://127.0.0.1:8787/' };
global.window = {};

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const li = () => '';
const toolIcon = () => '';
const _isMemorySave = () => false;
const _isSkillUpdate = () => false;
const t = k => ({generated_image_preview: 'Generated image'}[k] || k);
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
const _PDF_EXTS=/\.pdf$/i;
const _HTML_EXTS=/\.html?$/i;
const _CSV_EXTS=/\.(csv|tsv)$/i;
const _EXCALIDRAW_EXTS=/\.excalidraw$/i;
const _mediaKindForName=(name='')=>{
  const clean=String(name||'').split('?')[0].toLowerCase();
  if(_AUDIO_EXTS.test(clean)) return 'audio';
  if(_VIDEO_EXTS.test(clean)) return 'video';
  if(_IMAGE_EXTS.test(clean)) return 'image';
  return '';
};
const S = {session:{session_id:'s-generated'}};
const _formatToolArgPreview = () => '';
const _toolActionKind = () => 'unknown';
const _toolActionLabelText = tc => String(tc && tc.name || 'tool');
const _toolDisplayName = tc => String(tc && tc.name || 'tool');
const _toolDisclosureIdentity = () => '';
const _toolCardAllowsDetail = () => true;
const _toolCardPreviewText = tc => String(tc && tc.snippet || '').split('\n')[0];
const _toolDetailLeadText = () => '';
const _toolDetailLeadLabel = () => 'Input';
const _redactToolTargetLabel = s => s;
const _snippetLooksLikeDiff = () => false;
const _colorDiffLines = s => s;
for (const name of ['_DATA_IMAGE_RE', '_DATA_IMAGE_SVG_RE', '_DATA_IMAGE_MAX_LEN']) {
  const m = src.match(new RegExp('const ' + name + '=([^\\n]*);'));
  if (!m) throw new Error(name + ' const not found');
  globalThis[name] = eval('(' + m[1] + ')');
}
function extractFunc(name){
  const marker='function '+name+'(';
  const start=src.indexOf(marker);
  if(start<0) throw new Error(name+' not found');
  let brace=src.indexOf('{',start), depth=1, i=brace+1;
  while(i<src.length&&depth>0){ if(src[i]==='{') depth++; else if(src[i]==='}')
    depth--; i++; }
  if(depth) throw new Error(name+' unbalanced');
  return src.slice(start,i);
}
eval(extractFunc('_isSafeDataImageUri'));
eval(extractFunc('_dataImageHtml'));
eval(extractFunc('_mdImageHtml'));
eval(extractFunc('_inlineMediaHtmlForRef'));
eval(extractFunc('_generatedImageArtifactRef'));
eval(extractFunc('buildToolCard'));

let buf='';
process.stdin.on('data', c => { buf+=c; });
process.stdin.on('end', () => {
  const tc = JSON.parse(buf);
  const row = buildToolCard(tc);
  process.stdout.write(row.innerHTML || '');
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("generated_image_tool_cards") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _card(driver_path: str, tool_call: dict) -> str:
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=json.dumps(tool_call),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def _generate_call(result: dict, *, snippet_override=None, **extra) -> dict:
    return {
        "name": "image_generate",
        "snippet": snippet_override if snippet_override is not None else json.dumps(result),
        "tid": "call-image-1",
        "args": {"prompt": "a snowy city"},
        "done": True,
        **extra,
    }


def test_successful_image_generate_card_renders_media_preview(driver_path):
    html = _card(driver_path, _generate_call({"success": True, "image": _IMAGE_PATH}))

    assert "generated-image-preview" in html
    assert "msg-artifact-image" in html
    assert 'class="tool-card open"' in html
    assert "api/media?path=%2Froot%2F.hermes%2Fcache%2Fimages" in html
    assert "Generated image" in html


def test_media_marker_in_snippet_uses_existing_renderer(driver_path):
    snippet = f"{_IMAGE_PATH}\nMEDIA:{_IMAGE_PATH}"
    html = _card(driver_path, _generate_call({"success": True, "image": _IMAGE_PATH}, snippet_override=snippet))

    assert "generated-image-preview" in html
    assert "api/media?path=%2Froot%2F.hermes%2Fcache%2Fimages" in html


def test_failed_image_generate_card_does_not_preview(driver_path):
    html = _card(
        driver_path,
        _generate_call(
            {"success": False, "image": None, "error": "provider unavailable"},
            is_error=True,
        ),
    )

    assert "generated-image-preview" not in html
    assert "api/media?path=" not in html


def test_other_tool_cannot_mint_image_preview_from_named_field(driver_path):
    html = _card(
        driver_path,
        {
            "name": "terminal",
            "snippet": json.dumps({"output": "text", "image": "/tmp/fake.png"}),
            "done": True,
        },
    )

    assert "generated-image-preview" not in html
    assert "api/media?path=%2Ftmp%2Ffake.png" not in html
