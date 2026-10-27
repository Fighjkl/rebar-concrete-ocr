"""Make generated review pages serve their own full-resolution images.

This changes presentation only; OCR and matching JSON stay untouched.
"""
from pathlib import Path
import re
import shutil


VIEWER = r'''
<style id="review-image-viewer-style">
#review-image-viewer{position:fixed;inset:0;z-index:10000;background:#122132f2;display:flex;flex-direction:column;color:white}
#review-image-viewer[hidden]{display:none}
#review-image-viewer .viewer-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 18px;background:#162b42}
#review-image-viewer .viewer-title{margin-right:auto;font-weight:600}
#review-image-viewer button{background:white;color:#17324d;border:0;border-radius:5px;padding:8px 12px;cursor:pointer}
#review-image-viewer input{width:120px;padding:0;flex:initial}
#review-image-viewer .viewer-stage{flex:1;min-height:0;overflow:auto;padding:12px;text-align:center}
#review-image-viewer .viewer-stage img{display:block;margin:0 auto;max-width:none;max-height:none}
#review-image-viewer .viewer-message{padding:10px;text-align:center}
#review-image-viewer .viewer-message:empty{display:none}
#review-image-viewer .viewer-help{font-size:12px;margin:0 18px 10px;color:#bfcfdf}
a:has(img.thumb){cursor:zoom-in}
</style>
<div id="review-image-viewer" role="dialog" aria-modal="true" aria-labelledby="viewer-title" hidden>
 <div class="viewer-controls">
  <span class="viewer-title" id="viewer-title">页面大图</span>
  <button type="button" id="viewer-fit">适应窗口</button>
  <button type="button" id="viewer-actual">原始大小</button>
  <label>缩放 <input id="viewer-zoom" type="range" min="10" max="400" value="100"></label>
  <span id="viewer-scale">100%</span>
  <button type="button" id="viewer-close" aria-label="关闭图片">关闭 · Esc</button>
 </div>
 <div class="viewer-message" id="viewer-message" aria-live="polite"></div>
 <div class="viewer-stage" id="viewer-stage"><img id="viewer-image" alt=""></div>
 <p class="viewer-help">放大后可滚动检查细节；按 Esc 关闭并返回当前记录。</p>
</div>
<script id="review-image-viewer-script">
(() => {
 'use strict';
 const get = id => document.getElementById(id);
 const viewer = get('review-image-viewer'), stage = get('viewer-stage');
 const picture = get('viewer-image'), zoom = get('viewer-zoom');
 let previousFocus = null, savedOverflow = '', scale = 100, loaded = false;
 function resize(value) {
   if (!loaded) return;
   scale = Math.max(10, Math.min(400, value));
   picture.style.width = Math.round(picture.naturalWidth * scale / 100) + 'px';
   picture.style.height = 'auto';
   zoom.value = String(Math.round(scale));
   get('viewer-scale').textContent = Math.round(scale) + '%';
 }
 function fit() {
   if (!loaded) return;
   resize(Math.min(1, Math.max(1, stage.clientWidth - 24) / picture.naturalWidth,
                         Math.max(1, stage.clientHeight - 24) / picture.naturalHeight) * 100);
   stage.scrollTop = 0; stage.scrollLeft = 0;
 }
 function close() {
   if (viewer.hidden) return;
   viewer.hidden = true;
   document.body.style.overflow = savedOverflow;
   if (previousFocus?.isConnected) previousFocus.focus();
 }
 function open(anchor) {
   previousFocus = anchor;
   savedOverflow = document.body.style.overflow;
   document.body.style.overflow = 'hidden';
   loaded = false; picture.style.visibility = 'hidden';
   get('viewer-title').textContent = anchor.querySelector('img')?.alt?.split('；')[0] || '页面大图';
   get('viewer-message').textContent = '正在加载原始分辨率图像…';
   viewer.hidden = false;
   picture.onload = () => {
     loaded = true; picture.style.visibility = 'visible';
     get('viewer-message').textContent = '';
     fit();
   };
   picture.onerror = () => {
     get('viewer-message').textContent = '图片加载失败，请刷新页面后重试。';
   };
   picture.alt = get('viewer-title').textContent;
   picture.src = anchor.href;
   get('viewer-close').focus();
 }
 document.addEventListener('click', event => {
   const anchor = event.target.closest('a');
   if (!anchor || (!anchor.querySelector('img.thumb') && anchor.id !== 'original')) return;
   event.preventDefault(); open(anchor);
 });
 get('viewer-close').onclick = close;
 get('viewer-fit').onclick = fit;
 get('viewer-actual').onclick = () => resize(100);
 zoom.oninput = () => resize(Number(zoom.value));
 document.addEventListener('keydown', event => {
   if (viewer.hidden) return;
   if (event.key === 'Escape') { event.preventDefault(); close(); }
   if (event.key === 'Tab') {
     const controls = [...viewer.querySelectorAll('button,input')];
     const first = controls[0], last = controls[controls.length - 1];
     if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
     else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
   }
 });
})();
</script>
'''


def prepare_review_images(html_path: str | Path, source_images: str | Path) -> dict:
    """Localise image links and add an in-page viewer, safe to apply twice."""
    html_path, source_images = Path(html_path), Path(source_images)
    original = html_path.read_text(encoding="utf-8")
    pages = sorted(set(int(n) for n in re.findall(
        r'"(?:page_no|nameplate_page|certificate_page)"\s*:\s*(\d+)', original)))
    target = html_path.parent / "page_images"
    target.mkdir(exist_ok=True)
    for number in pages:
        name = f"page_{number:03d}_upright.png"
        source, destination = source_images / name, target / name
        if not source.is_file():
            raise FileNotFoundError(source)
        if not destination.exists():
            shutil.copy2(source, destination)
    text = re.sub(r'\.\./[^"\s<>]+/pages/(?=page_)', 'page_images/', original)
    text = text.replace("图片链接打开完整转正页图。", "点击图像可在当前页面放大、缩放和检查原始图像。")
    if 'id="review-image-viewer-script"' not in text:
        text = text.replace("</body>", VIEWER + "</body>")
    html_path.write_text(text, encoding="utf-8")
    return {"html_path": str(html_path), "local_full_resolution_image_count": len(pages)}


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html_path", type=Path)
    parser.add_argument("--source-images", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare_review_images(args.html_path, args.source_images), ensure_ascii=False))
