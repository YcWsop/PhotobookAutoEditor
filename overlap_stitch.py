"""Overlap rule for books whose spreads were scanned as two PARTIAL pages that overlap in the middle
(e.g. left page = 60 % of the picture, right page = 60 %, 20 % shared).  The ordinary seam rule compares one pixel column and cannot see
that; here a PARAGRAPH-sized strip of one page is searched in the other page and the whole overlap band is verified.

    python overlap_stitch.py book.pdf --dry            # list matching neighbouring pairs + write a preview sheet
    python overlap_stitch.py book.pdf                  # merge them in place (original -> Recycle Bin)
    python overlap_stitch.py book.pdf --pages 2-50     # only look at that page range

Merging is lossless: both JPEGs stay as they are; the page is as wide as the union and each image is clipped at the middle of the
overlap (PDF clipping path), so nothing is re-encoded or cropped destructively.
"""
import argparse, os, sys, io, shutil, tempfile
import numpy as np
import cv2
import pikepdf
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import photobook_lib as L


def one_image(pg):
    res = pg.get('/Resources')
    xo = res.get('/XObject') if res is not None else None
    if xo is None or len(xo) != 1:
        return None
    x = list(xo.values())[0]
    return x if str(x.get('/Subtype')) == '/Image' else None


def load_small(x, scale=0.5):
    im = pikepdf.PdfImage(x).as_pil_image().convert('RGB')
    w, h = im.size
    sm = im.resize((max(8, int(w * scale)), max(8, int(h * scale))), Image.BILINEAR)
    return np.asarray(sm)


def _gray(a):
    return cv2.cvtColor(a, cv2.COLOR_RGB2GRAY).astype(np.float32)


def match_pair(A, B, k_frac=0.03, ov_max=0.40, dy_frac=0.05):
    """A = left page, B = right page (RGB arrays, same width, heights may differ a little).
    The two pages share a narrow band: A's rightmost columns reappear at B's left edge.  A strip of A's right edge (the 'paragraph')
    is searched in B's left part, then the WHOLE shared band is verified in colour against vertically shifted controls.
    dx = column of A that coincides with B's column 0; dy = A row of B's row 0 (A row = B row + dy); ovl = shared width / page width."""
    ha, w = A.shape[:2]
    hb = B.shape[0]
    if B.shape[1] != w or min(ha, hb) < 80 or w < 60:
        return None
    ga, gb = _gray(A), _gray(B)
    k = max(10, int(w * k_frac))
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


def analyse(pdf, lo, hi):
    """-> list of (i, order, metrics) for neighbouring pairs (0-based index of the first page) that are accepted.
    order 'ltr': page i is the left half; 'rtl': page i+1 is the left half."""
    out, cache = [], {}
    def small(i):
        if i not in cache:
            x = one_image(pdf.pages[i])
            cache[i] = None if x is None else load_small(x)
            cache.pop(i - 3, None)
        return cache[i]
    for i in range(max(0, lo), min(len(pdf.pages) - 1, hi)):
        a, b = small(i), small(i + 1)
        if a is None or b is None or a.shape[1] != b.shape[1] or abs(a.shape[0] - b.shape[0]) > 0.2 * a.shape[0]:
            continue
        m1 = match_pair(a, b)           # a left, b right
        m2 = match_pair(b, a)           # b left, a right
        best, order = None, None
        for m, o in ((m1, 'ltr'), (m2, 'rtl')):
            if m is not None and decide(m) and (best is None or m['diff'] / max(m['ctrl'], 1e-6) < best['diff'] / max(best['ctrl'], 1e-6)):
                best, order = m, o
        out.append((i, order, best, m1, m2))
    return out


def geometry(m, wl, hl, hr):
    """Page geometry in image pixels of the LEFT image: seam x, union-intersection rows. -> dict"""
    dx, dy = m['dx'] * (wl / m['w']), m['dy'] * (wl / m['w'])
    top_i = max(0.0, dy)
    bot_i = min(hl, hr + dy)
    return dict(dx=dx, dy=dy, xs=dx + (wl - dx) / 2.0, top=top_i, Hi=bot_i - top_i)


def stitch_preview(pdf, i, order, m, H=330):
    a = np.asarray(pikepdf.PdfImage(one_image(pdf.pages[i])).as_pil_image().convert('RGB'))
    b = np.asarray(pikepdf.PdfImage(one_image(pdf.pages[i + 1])).as_pil_image().convert('RGB'))
    left, right = (a, b) if order == 'ltr' else (b, a)
    hl, w = left.shape[:2]
    hr = right.shape[0]
    g = geometry(m, w, hl, hr)
    dx, top, Hi, xs = int(round(g['dx'])), int(round(g['top'])), int(round(g['Hi'])), int(round(g['xs']))
    dyi = int(round(g['dy']))
    W = dx + w
    canvas = np.zeros((Hi, W, 3), np.uint8)
    canvas[:, 0:xs] = left[top:top + Hi, 0:xs]
    rt = top - dyi                                        # first row of the right image used
    canvas[:, xs:W] = right[rt:rt + Hi, xs - dx:w]
    im = Image.fromarray(canvas)
    return im.resize((int(im.width * H / im.height), H))


def merge(src, dst, accepted):
    """accepted: dict i -> (order, metrics). Pages i and i+1 become one page."""
    pdf = pikepdf.open(src)
    try:
        new = pikepdf.new()
        skip = {i + 1 for i in accepted}
        for k, pg in enumerate(pdf.pages):
            if k in skip:
                continue
            if k not in accepted:
                new.pages.append(pg); continue
            order, m = accepted[k]
            pl, pr = (pg, pdf.pages[k + 1]) if order == 'ltr' else (pdf.pages[k + 1], pg)
            xl, xr = one_image(pl), one_image(pr)
            wl = float(pl.MediaBox[2] - pl.MediaBox[0]); hl = float(pl.MediaBox[3] - pl.MediaBox[1])
            wr = float(pr.MediaBox[2] - pr.MediaBox[0]); hr = float(pr.MediaBox[3] - pr.MediaBox[1])
            if abs(wl - wr) > 1:
                raise ValueError('pages %d/%d differ in width' % (k + 1, k + 2))
            g = geometry(m, wl, hl, hr)
            dx, xs, top, Hi, dy = g['dx'], g['xs'], g['top'], g['Hi'], g['dy']
            total_w = dx + wr
            H = Hi
            yl = Hi + top - hl                        # PDF y grows upward
            yr = Hi - dy + top - hr
            xd = pikepdf.Dictionary()
            xd['/Im0'] = new.copy_foreign(xl)
            xd['/Im1'] = new.copy_foreign(xr)
            content = ('q 0 0 %.4f %.4f re W n %.4f 0 0 %.4f 0 %.4f cm /Im0 Do Q '
                       'q %.4f 0 %.4f %.4f re W n %.4f 0 0 %.4f %.4f %.4f cm /Im1 Do Q') % (
                xs, H, wl, hl, yl, xs, total_w - xs, H, wr, hr, dx, yr)
            page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, total_w, H],
                                      Resources=pikepdf.Dictionary(XObject=xd), Contents=new.make_stream(content.encode()))
            new.pages.append(pikepdf.Page(page))
        new.save(dst)
        return len(new.pages), len(pdf.pages)
    finally:
        pdf.close()


def main():
    sys.stdout.reconfigure(errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('pdf')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--pages', default=None, help='page range a-b (1-based, first page of the pair)')
    ap.add_argument('--force', default='', help='comma list of first-page numbers of pairs that were LOOKED AT and are one picture although the strict test says no (best direction is used)')
    ap.add_argument('--out', default=None, help='write the merged book here instead of replacing the source')
    ap.add_argument('--sheet', default=os.path.join(tempfile.gettempdir(), 'overlap_preview.jpg'))
    a = ap.parse_args()
    lo, hi = 0, 10 ** 9
    if a.pages:
        x, y = a.pages.split('-'); lo, hi = int(x) - 1, int(y)
    with pikepdf.open(a.pdf) as pdf:
        res = analyse(pdf, lo, hi)
        acc = {}
        prev = -9
        forced = {int(x) - 1 for x in a.force.split(',') if x.strip()}
        for i, order, best, m1, m2 in res:
            if best is None and i in forced:
                cand = [(m['diff'] / max(m['ctrl'], 1e-6), o, m) for m, o in ((m1, 'ltr'), (m2, 'rtl')) if m is not None]
                if cand:
                    _, order, best = min(cand, key=lambda t: t[0])
            if best is not None and i != prev + 1:        # never chain: a page belongs to one pair
                acc[i] = (order, best); prev = i
        tiles = []
        for i, (order, m) in acc.items():
            print('pair %d-%d %s score %.2f second %.2f overlap %.0f%% dy %d diff %.1f ctrl %.1f' % (
                i + 1, i + 2, order, m['score'], m['second'], m['ovl'] * 100, m['dy'], m['diff'], m['ctrl']))
            t = stitch_preview(pdf, i, order, m)
            ImageDraw.Draw(t).text((3, 3), '%d-%d' % (i + 1, i + 2), fill='red')
            tiles.append(t)
        if tiles:
            cols = 4
            rows = (len(tiles) + cols - 1) // cols
            W = max(t.width for t in tiles)
            sheet = Image.new('RGB', (cols * (W + 4), rows * 334), 'gray')
            for k, t in enumerate(tiles):
                sheet.paste(t, ((k % cols) * (W + 4), (k // cols) * 334))
            sheet.save(a.sheet)
            print('preview:', a.sheet)
        print('%d pair(s) accepted' % len(acc))
        n_in = len(pdf.pages)
    if a.dry or not acc:
        return
    if a.out:
        n_out, n_in2 = merge(a.pdf, a.out, acc)
        print('merged %d pairs into %s: %d -> %d pages' % (len(acc), a.out, n_in2, n_out))
        return
    tmp = os.path.join(tempfile.mkdtemp(prefix='ovl_'), 'o.pdf')
    n_out, n_in2 = merge(a.pdf, tmp, acc)
    assert n_out == n_in2 - len(acc), (n_out, n_in2, len(acc))
    import gc; gc.collect()
    L.recycle(a.pdf); shutil.move(tmp, a.pdf)
    print('merged %d pairs: %d -> %d pages' % (len(acc), n_in2, n_out))


if __name__ == '__main__':
    main()
