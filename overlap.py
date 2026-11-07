"""Overlap rule: a picture scanned as two PARTIAL pages that share a narrow band in the middle (a few percent of the page width).
The ordinary seam rule compares one pixel column and cannot see that: the band is DUPLICATED, not continued.

Here a strip of one page's outer edge is searched in the other page (template match) and the whole shared band is verified in
colour against vertically shifted controls. Used by photobook_lib.arrange (stage 'overlap') and by overlap_stitch.py (PDFs).

  prescreen(A, B)   cheap: 450-column pictures, loose gate (high recall, some false alarms)
  find(A, B)        prescreen, then the strict test (decide) on 900-column pictures, both orders -> ('ltr'|'rtl', metrics) or None   (ltr: A is the left page)
"""
import numpy as np
import cv2
from PIL import Image

PRE_WIDTH, PRE_SCORE, PRE_K_MIN = 450, 0.7, 8     # prescreen picture width, loose template score, template columns
CONF_WIDTH = 900                                    # strict test picture width (the overlap is only ~3-5 % of the page width)


def _gray(a):
    return cv2.cvtColor(a, cv2.COLOR_RGB2GRAY).astype(np.float32)


def match_pair(A, B, k_frac=0.03, ov_max=0.40, dy_frac=0.05, k_min=10):
    """A = left page, B = right page (RGB arrays, same width, heights may differ a little).
    The two pages share a narrow band: A's rightmost columns reappear at B's left edge.  A strip of A's right edge (the 'paragraph')
    is searched in B's left part, then the WHOLE shared band is verified in colour against vertically shifted controls.
    dx = column of A that coincides with B's column 0; dy = A row of B's row 0 (A row = B row + dy); ovl = shared width / page width."""
    ha, w = A.shape[:2]
    hb = B.shape[0]
    if B.shape[1] != w or min(ha, hb) < 80 or w < 60:
        return None
    ga, gb = _gray(A), _gray(B)
    k = max(k_min, int(w * k_frac))
    dy_max = max(int(min(ha, hb) * dy_frac), abs(ha - hb) + 6)
    y0, y1 = int(ha * 0.12), int(ha * 0.88)
    tmpl = ga[y0:y1, w - k:w]
    if tmpl.std() < 5:                      # flat strip (plain wall/sky): nothing to lock on
        return None
    rb0, rb1 = max(0, y0 - dy_max), min(hb, y1 + dy_max)
    xmax = int(w * ov_max)
    reg = gb[rb0:rb1, 0:xmax]
    if reg.shape[0] < tmpl.shape[0] or reg.shape[1] < tmpl.shape[1]:
        return None
    res = cv2.matchTemplate(reg, tmpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    r2 = res.copy()
    cv2.circle(r2, loc, max(4, k), -1, -1)
    second = float(r2.max())
    ovl_px = loc[0] + k                      # B column (ovl_px-1) is A's last column
    dx = w - ovl_px
    dy = y0 - (rb0 + loc[1])

    def band_diff(ddy, extra=0):
        wd = ovl_px + extra
        if wd < 6 or wd > w:
            return 1e9
        xa = w - wd
        ya0, ya1 = max(0, dy + ddy), min(ha, hb + dy + ddy)
        if ya1 - ya0 < min(ha, hb) * 0.6:
            return 1e9
        yb0, yb1 = ya0 - dy - ddy, ya1 - dy - ddy
        a = A[ya0:ya1, xa:w].astype(np.float32)
        b = B[yb0:yb1, 0:wd].astype(np.float32)
        return float(np.abs(a - b).mean())
    diff = band_diff(0)
    off = dy_max + 8
    ctrl = float(np.median([band_diff(-off), band_diff(off), band_diff(-2 * off), band_diff(2 * off)]))
    return dict(score=float(score), second=second, ovl=ovl_px / w, dx=int(dx), dy=int(dy), diff=diff, ctrl=ctrl, w=w, ha=ha, hb=hb)


def decide(m):
    """True if the pair is one picture cut into two overlapping pages.
    A true pair: the shared band matches almost perfectly (diff far below the vertically shifted controls); two different photos never do."""
    if m is None:
        return False
    return (m['score'] >= 0.85 and m['diff'] <= 0.4 * m['ctrl'] and m['diff'] <= 15 and 0.02 <= m['ovl'] <= 0.40)


def geometry(m, wl, hl, hr):
    """Page geometry in image pixels of the LEFT image: seam x, union-intersection rows. -> dict"""
    dx, dy = m['dx'] * (wl / m['w']), m['dy'] * (wl / m['w'])
    top_i = max(0.0, dy)
    bot_i = min(hl, hr + dy)
    return dict(dx=dx, dy=dy, xs=dx + (wl - dx) / 2.0, top=top_i, Hi=bot_i - top_i)




def small_rgb(path, width):
    """the page as an RGB array `width` columns wide (JPEG draft decoding makes this cheap; never enlarged)"""
    im = Image.open(path)
    try:
        width = min(width, im.width)
        if im.format == 'JPEG':
            im.draft('RGB', (width, max(1, round(im.height * width / im.width))))
        im = im.convert('RGB')
        if im.width != width:
            im = im.resize((width, max(1, round(im.height * width / im.width))), Image.BOX)
        return np.asarray(im)
    finally:
        im.close()


def _evict(cache):
    if len(cache) > 12:                           # neighbours only: forget the oldest pictures
        for old in list(cache)[:6]:
            del cache[old]


def warm(cache, infos, width=PRE_WIDTH):
    """start decoding the prescreen pictures of the pages in `infos` on the shared pool (seam_load.pool()); _get() waits for them.
    Only pages find() could ever look at (plain portrait pages with a file)."""
    import seam_load
    for A in infos:
        if A.get('opaque') or 'path' not in A or A['h'] <= A['w']:
            continue
        k = (A['path'], width)
        if k not in cache:
            _evict(cache)
            cache[k] = seam_load.pool().submit(small_rgb, A['path'], width)


def _get(cache, path, width):
    k = (path, width)
    if k not in cache:
        _evict(cache)
        cache[k] = small_rgb(path, width)
    v = cache[k]
    if hasattr(v, 'result'):                      # a Future from warm()
        v = cache[k] = v.result()
    return v


def prescreen(A, B, cache):
    """True if the pair deserves the strict test: a loose template score at PRE_WIDTH columns, either order (all true pairs of
    the test book pass, about a third of ordinary neighbours do)"""
    a, b = _get(cache, A['path'], PRE_WIDTH), _get(cache, B['path'], PRE_WIDTH)
    for X, Y in ((a, b), (b, a)):
        m = match_pair(X, Y, k_min=PRE_K_MIN)
        if m is not None and m['score'] >= PRE_SCORE and 0.02 <= m['ovl'] <= 0.40:
            return True
    return False


def find(A, B, cache=None):
    """A, B: info dicts (path, w, h) of neighbouring pages of the same size. -> ('ltr'|'rtl', metrics) or None.
    'ltr' means A is the left page, 'rtl' that B is."""
    if A.get('opaque') or B.get('opaque') or 'path' not in A or 'path' not in B:
        return None
    if A['w'] != B['w'] or abs(A['h'] - B['h']) > 0.2 * A['h'] or A['h'] <= A['w']:
        return None
    cache = {} if cache is None else cache
    if not prescreen(A, B, cache):
        return None
    a, b = _get(cache, A['path'], CONF_WIDTH), _get(cache, B['path'], CONF_WIDTH)
    best = None
    for m, order in ((match_pair(a, b), 'ltr'), (match_pair(b, a), 'rtl')):
        if m is not None and decide(m):
            r = m['diff'] / max(m['ctrl'], 1e-6)
            if best is None or r < best[2]:
                best = (order, m, r)
    return None if best is None else (best[0], best[1])
