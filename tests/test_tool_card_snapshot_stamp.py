"""Frontend behavior: settled tool-card previews carry the frozen digest.

Drives the real ``buildToolCard`` from ``static/ui.js`` through Node and pins
that a settled ``image_generate`` tool call carrying ``_media_snapshots`` has
its generated-image preview URL (and download href) stamped with
``&snap=<digest>`` — the same stamping contract settled assistant messages
use. A card without the map renders the plain (live) URL.
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

_IMAGE_PATH = "/root/.hermes/cache/images/snap-fixture.png"
_DIGEST = "a" * 64

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
const _mediaPlayerHtml=(k,s,n)=>`<${k} src="${esc(s)}"></${k}>`;
const S = {session:{session_id:'s-snap'}};
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
  while(i<src.length&&depth>0){ if(src[i]==='{') depth++; else if(src[i]==='}') depth--; i++; }
  if(depth) throw new Error(name+' unbalanced');
  return src.slice(start,i);
}
eval(extractFunc('_isSafeDataImageUri'));
eval(extractFunc('_dataImageHtml'));
eval(extractFunc('_mdImageHtml'));
eval(extractFunc('_inlineMediaHtmlForRef'));
eval(extractFunc('_stampMediaSnapshots'));
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
    path = tmp_path_factory.mktemp("tool_card_snapshots") / "driver.js"
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


def _generate_call(**extra) -> dict:
    tc = {
        "name": "image_generate",
        "snippet": f"{_IMAGE_PATH}\nMEDIA:{_IMAGE_PATH}",
        "tid": "call-snap-ui",
        "args": {"prompt": "fixture"},
        "done": True,
    }
    tc.update(extra)
    return tc


def test_card_with_snapshot_map_stamps_preview_url(driver_path):
    html = _card(driver_path, _generate_call(
        _media_snapshots={_IMAGE_PATH: _DIGEST}))

    assert "generated-image-preview" in html
    assert f"api/media?path=%2Froot%2F.hermes%2Fcache%2Fimages%2Fsnap-fixture.png&snap={_DIGEST}" in html
    # The download href rides the same stamped URL.
    assert html.count(f"&snap={_DIGEST}") >= 2


def test_card_without_snapshot_map_keeps_live_url(driver_path):
    html = _card(driver_path, _generate_call())

    assert "generated-image-preview" in html
    assert "&snap=" not in html
    assert "api/media?path=%2Froot%2F.hermes%2Fcache%2Fimages" in html


def test_card_with_unrelated_snapshot_map_keeps_live_url(driver_path):
    """A digest keyed by a different path must not stamp this card's URL."""
    html = _card(driver_path, _generate_call(
        _media_snapshots={"/tmp/other.png": _DIGEST}))

    assert "&snap=" not in html


def test_invalid_digest_shape_is_not_stamped(driver_path):
    html = _card(driver_path, _generate_call(
        _media_snapshots={_IMAGE_PATH: "not-a-digest"}))

    assert "&snap=" not in html