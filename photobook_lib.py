"""Photobook helpers (see README.md): build one PDF per image folder -- drop blank/ad pages,
merge two-page spreads, dedupe full-width scans of spreads (keep the higher-resolution one),
uniform page size, and optional ad stripping from existing PDFs."""
import sys, os, re, io, json, zlib, hashlib
import numpy as np
from PIL import Image
import pikepdf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seam_load
import seam_full
try:
    import cv2
    _QR = cv2.QRCodeDetector()
except Exception:  # opencv optional: without it only the seed library is used
    cv2 = None
    _QR = None

ADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ads')

EXTS = {'.jpg', '.jpeg', '.png'}
PT = 0.48  # points per pixel, same as the reference PDF


def natkey(p):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', p.replace('/', '\\'))]


def list_images(folder):
    out = []
    for root, _, fs in os.walk(folder):
        for f in fs:
            if os.path.splitext(f)[1].lower() in EXTS:
                out.append(os.path.join(root, f))
    out.sort(key=lambda p: natkey(os.path.relpath(p, folder)))
    return out


def gray(im):
    return np.asarray(im.convert('L'), dtype=np.uint8)


def dhash(g):
    im = Image.fromarray(g).resize((9, 8), Image.BILINEAR)
    a = np.asarray(im, dtype=np.int16)
    return (a[:, 1:] > a[:, :-1]).flatten()


def _full_gray(src):
    """full-resolution gray of an image file (path or bytes); only needed for the QR check of mostly white pages"""
    with Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src) as im:
        return gray(im)


def load_info(path):
    W, H, d = seam_load.load_small(path)
    g = d['g']
    th = Image.fromarray(g).resize((32, 32), Image.BILINEAR)
    raw = open(path, 'rb').read()
    return dict(path=path, w=W, h=H, size=len(raw), g=g, rgb8=d['rgb8'], tn=d['tn'],
                small=np.asarray(th, dtype=np.float32), dh=dhash(g), sha=hashlib.sha1(raw).hexdigest(),
                gfull=(lambda p=path: _full_gray(p)), **d.get('full', {}))


def is_blank(info):
    s = info['small']
    return s.std() < 4 and (s.mean() > 235 or s.mean() < 20)


UNCERTAIN = []          # (info_a, info_b) pairs the rule could not decide; see write_review_sheet()
FLAT = []               # (info_a, info_b, stats) pairs whose seam is a plain colour and nearly equal on both sides: the seam rule
                        # cannot judge them (screening stage, nothing is merged from here automatically)
FORCE = {}              # frozenset({name_a, name_b}) -> (left name, right name): merges decided from outside (human / AI review)
FLAT_SEAM = 6.0         # a flat pair is queued when the colour difference across the seam is below this


SEAM_RATIO = 0.5      # merge: the seam fits at most 0.5x as badly as when B is slid up/down
SEAM_JUMP = 2.5       # merge: the seam jump is at most 2.5x the picture's own column-to-column change
SEAM_LOOK = 0.8       # not merged but still below this (jump < 4): UNCERTAIN, a contact sheet is written
SEAM_STD = 6          # gray std of each seam column along the height; a flat column says nothing about row alignment
SEAM_FAR = (-12, -9, 9, 12)   # control shifts in analysis rows (the analysis picture is seam_load.AN_H = 272 rows high)


def _sad(x, y, s):
    """mean abs difference of two row profiles with y slid down by s rows"""
    h = len(x)
    if s >= 0:
        x, y = x[s:], y[:h - s]
    else:
        x, y = x[:h + s], y[-s:]
    return float(np.abs(x - y).mean())


def seam_stats(A, B):
    """A | B: does B's left edge continue A's right edge? Measured on the small RGB analysis pictures (rgb8, 272 rows high):
    one column there is the average of a strip about 1/8 of that scale wide in the original, so the test reads wide content
    but needs only a reduced JPEG decode.
      seam   mean abs colour difference across the seam
      ratio  seam / median difference when B is slid 9-12 rows. One picture cut in two fits row for row (small);
             two photos of the same wall fit equally badly at every shift (~1)
      jump   seam / the pictures' own column-to-column change next to the seam (rough textures have big jumps everywhere)
      std    gray structure of the seam columns along the height (a flat wall says nothing about alignment, however colourful)"""
    a8, b8 = A['rgb8'], B['rgb8']
    a, b = a8[:, -1].astype(np.float32), b8[:, 0].astype(np.float32)
    s0 = _sad(a, b, 0)
    far = float(np.median([_sad(a, b, s) for s in SEAM_FAR]))
    ia = float(np.abs(a - a8[:, -2]).mean())
    ib = float(np.abs(b - b8[:, 1]).mean())
    gw = np.array([0.299, 0.587, 0.114], np.float32)
    return dict(seam=s0, ratio=s0 / (far + 1e-3), jump=s0 / ((ia + ib) / 2 + 1.0), std=min(float((a @ gw).std()), float((b @ gw).std())))


def seam_match(A, B, ratio_thr=SEAM_RATIO, jump_thr=SEAM_JUMP, smooth_ok=False):
    """True if A's right edge continues into B's left edge (A | B). Returns (ok, ratio).
    Measured on the library's own pages (2856 merged spreads, 6679 neighbouring pages left separate):
    ratio < 0.5 and jump < 2.5 accepts 97 % of the merged ones and 0.2 % of the separate ones.
    (Averaging a wide band into one profile was tried: it fits worse, because the band's content changes with distance
    from the seam. The one-column test at reduced scale is the wide-content test.)"""
    if A['w'] != B['w'] or A['h'] != B['h'] or A['h'] <= A['w']:
        return False, 1e9
    st = seam_stats(A, B)
    ok = st['std'] >= SEAM_STD and st['ratio'] < ratio_thr and st['jump'] < jump_thr
    if not ok and smooth_ok:
        # Only when deciding whether to SPLIT an existing spread: a very smooth picture (soft backdrop, bokeh) has no
        # structure to align, so judge by how tiny the colour jump is. NEVER for merging: two different photos in front
        # of the same plain wall pass this test too (that is exactly the false merge it once produced).
        ok = st['seam'] < 5.6 and st['ratio'] < 0.25
    return ok, st['ratio']


def spread_order(A, B):
    if A.get('opaque') or B.get('opaque'):
        return None
    forced = FORCE.get(frozenset((A.get('name'), B.get('name'))))
    if forced:
        return (A, B) if forced == (A.get('name'), B.get('name')) else (B, A)
    full = seam_load.FULL        # 'full' version = original 2-px rule at full resolution, else the fast reduced-scale rule
    ok1, d1 = seam_full.edge_match(A, B) if full else seam_match(A, B)   # A | B
    ok2, d2 = seam_full.edge_match(B, A) if full else seam_match(B, A)   # B | A
    if ok1 and (not ok2 or d1 <= d2):
        return (A, B)
    if ok2:
        return (B, A)
    # Not merged. If it would pass with a lenient test the rule is unsure: queue the pair for a human/agent look
    # (see write_review_sheet). Rule of the project: when really unsure, spend the effort and look at the picture.
    for X, Y in ((A, B), (B, A)):
        if (seam_full.edge_match(X, Y, align_thr=0.95)[0] if full else seam_match(X, Y, ratio_thr=SEAM_LOOK, jump_thr=4)[0]):
            UNCERTAIN.append((X, Y)); break
    else:
        if A['w'] == B['w'] and A['h'] == B['h'] and A['h'] > A['w'] and 'rgb8' in A and 'rgb8' in B:
            sts = [(seam_stats(X, Y), X, Y) for X, Y in ((A, B), (B, A))]
            st, X, Y = min(sts, key=lambda s: s[0]['seam'])
            if st['std'] < SEAM_STD and st['seam'] < FLAT_SEAM:
                FLAT.append((X, Y, st))
    return None


def wide_score(W, L, R):
    """mean abs difference (64x64 grey) between the wide scan and the two halves side by side; small = same picture"""
    if W['h'] <= 0:
        return 1e9
    th, tw = 64, 64
    wi = Image.fromarray(W['g']).resize((tw * 2, th), Image.BILINEAR)
    li = Image.fromarray(L['g']).resize((tw, th), Image.BILINEAR)
    ri = Image.fromarray(R['g']).resize((tw, th), Image.BILINEAR)
    cat = np.concatenate([np.asarray(li, dtype=np.float32), np.asarray(ri, dtype=np.float32)], axis=1)
    return float(np.abs(cat - np.asarray(wi, dtype=np.float32)).mean())


def wide_matches_pair(W, L, R):
    if W['h'] <= 0:
        return False
    th, tw = 64, 64
    wi = Image.fromarray(W['g']).resize((tw * 2, th), Image.BILINEAR)
    li = Image.fromarray(L['g']).resize((tw, th), Image.BILINEAR)
    ri = Image.fromarray(R['g']).resize((tw, th), Image.BILINEAR)
    cat = np.concatenate([np.asarray(li, dtype=np.float32), np.asarray(ri, dtype=np.float32)], axis=1)
    return np.abs(cat - np.asarray(wi, dtype=np.float32)).mean() < 12


def hamming(a, b):
    return int((a != b).sum())


# ---------------------------------------------------------------- ad pages
_SEEDS = None


def _seeds():
    """Known ad pages: every image in ads/ is a reference (32x32 gray + aspect)."""
    global _SEEDS
    if _SEEDS is None:
        _SEEDS = []
        if os.path.isdir(ADS_DIR):
            for f in sorted(os.listdir(ADS_DIR)):
                if os.path.splitext(f)[1].lower() in EXTS:
                    with Image.open(os.path.join(ADS_DIR, f)) as im:
                        g = np.asarray(im.convert('L').resize((32, 32), Image.BILINEAR), dtype=np.float32)
                        _SEEDS.append((im.size[0] / im.size[1], g))
    return _SEEDS


def is_ad_gray(g, full=None):
    """g: uint8 gray array of a page (small is fine). Ad = matches a seed in ads/, or a mostly-white page whose QR code decodes.
    full: optional callable returning the full-resolution gray array, called only for the QR check."""
    h, w = g.shape
    small = np.asarray(Image.fromarray(g).resize((32, 32), Image.BILINEAR), dtype=np.float32)
    for asp, sg in _seeds():
        if abs((w / h) / asp - 1) < 0.06 and np.abs(small - sg).mean() < 8:
            return True
    if _QR is not None and (g > 200).mean() > 0.75:
        g = full() if full is not None else g
        h, w = g.shape
        t = g
        if max(h, w) > 1400:
            sc = 1400 / max(h, w)
            t = cv2.resize(g, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)
        try:
            txt, _, _ = _QR.detectAndDecode(t)
            return bool(txt)
        except Exception:
            return False
    return False


def strip_ads_pdf(pdf_path):
    """Remove single-image ad pages from an existing PDF (in place). Returns removed page numbers."""
    import io
    pdf = pikepdf.open(pdf_path, allow_overwriting_input=True)
    drop = []
    for i, pg in enumerate(pdf.pages):
        xs = pg.Resources.get('/XObject', {})
        if len(xs) != 1:
            continue
        v = list(xs.values())[0]
        if v.get('/Subtype') != '/Image' or v.get('/Filter') != '/DCTDecode':
            continue
        with Image.open(io.BytesIO(v.read_raw_bytes())) as im:
            if is_ad_gray(gray(im)):
                drop.append(i + 1)
    for n in reversed(drop):
        del pdf.pages[n - 1]
    if drop:
        pdf.save(pdf_path)
    return drop


def recycle(path):
    """Move a file or folder to the Windows Recycle Bin."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT), ('pFrom', wintypes.LPCWSTR),
                    ('pTo', wintypes.LPCWSTR), ('fFlags', ctypes.c_ushort),
                    ('fAnyOperationsAborted', wintypes.BOOL), ('hNameMappings', ctypes.c_void_p),
                    ('lpszProgressTitle', wintypes.LPCWSTR)]
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    buf = ctypes.create_unicode_buffer(os.path.abspath(path) + '\0')  # double-NUL terminated
    op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
    op.fFlags = 0x40 | 0x10 | 0x4 | 0x400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise OSError('recycle failed (%s): %s' % (rc, path))


def make_xobject(pdf, path):
    with Image.open(path) as im:
        im.load()
        fmt = im.format
        mode = im.mode
        w, h = im.size
        if fmt == 'JPEG' and mode in ('RGB', 'L'):
            data = open(path, 'rb').read()
            st = pikepdf.Stream(pdf, data)
            st.Type = pikepdf.Name.XObject
            st.Subtype = pikepdf.Name.Image
            st.Width, st.Height = w, h
            st.ColorSpace = pikepdf.Name.DeviceRGB if mode == 'RGB' else pikepdf.Name.DeviceGray
            st.BitsPerComponent = 8
            st.Filter = pikepdf.Name.DCTDecode
            return st
        if mode in ('RGBA', 'LA', 'P', 'PA'):
            rgba = im.convert('RGBA')
            bg = Image.new('RGB', rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            im2 = bg
        else:
            im2 = im.convert('RGB') if mode != 'L' else im
        cs = pikepdf.Name.DeviceGray if im2.mode == 'L' else pikepdf.Name.DeviceRGB
        raw = zlib.compress(im2.tobytes(), 6)
        st = pikepdf.Stream(pdf, raw)
        st.Type = pikepdf.Name.XObject
        st.Subtype = pikepdf.Name.Image
        st.Width, st.Height = w, h
        st.ColorSpace = cs
        st.BitsPerComponent = 8
        st.Filter = pikepdf.Name.FlateDecode
        return st


def fit(sw, sh, bw, bh):
    """Place sw x sh into box bw x bh: stretch if aspect within 3%, else contain."""
    if abs((sw / sh) / (bw / bh) - 1) < 0.03:
        return 0, 0, bw, bh
    s = min(bw / sw, bh / sh)
    w, h = sw * s, sh * s
    return (bw - w) / 2, (bh - h) / 2, w, h


def build(folder, out_pdf):
    """One PDF from a folder of page images (old pipeline)."""
    paths = list_images(folder)
    infos = [load_info(p) for p in paths]
    for i in infos:
        i['name'] = os.path.relpath(i['path'], folder)
        i['key'] = i['path']
        i['xo'] = (lambda dst, p=i['path']: make_xobject(dst, p))
    rep = arrange(infos, out_pdf, PT)
    rep['folder'] = os.path.basename(folder)
    return rep, infos


# ------------------------------------------------------------ PDF as input (new pipeline)
def _page_single_image(pg):
    """The page's only image XObject if the page is a plain 'one picture' page, else None."""
    if pg.get('/Rotate', 0) not in (0, None):
        return None
    xs = pg.get('/Resources', {}).get('/XObject', {})
    if len(xs) != 1:
        return None
    img = list(xs.values())[0]
    if img.get('/Subtype') != '/Image' or '/SMask' in img or '/Mask' in img or img.get('/ImageMask', False):
        return None
    c = pg.get('/Contents')
    data = b''.join(x.read_bytes() for x in c) if isinstance(c, pikepdf.Array) else c.read_bytes()
    if b'BT' in data:   # has text -> leave the page alone
        return None
    return img


def _decode_small(img, raw):
    """-> (W, H, dict(rgb8, g, tn), gfull)"""
    if str(img.get('/Filter')) == '/DCTDecode':
        W, H, d = seam_load.load_small(raw)
        return W, H, d, (lambda o=img: _full_gray(o.read_raw_bytes()))
    im = pikepdf.PdfImage(img).as_pil_image()
    W, H = im.size
    return W, H, seam_load.derive(im.convert('RGB'), W, H), (lambda o=img: gray(pikepdf.PdfImage(o).as_pil_image()))


def pdf_items(src):
    """Turn each page of an open pikepdf.Pdf into an item. Plain one-picture pages are analysable;
    anything else (text, several images, rotated, undecodable) is 'opaque' and copied through unchanged."""
    items, ratios = [], []
    for n, pg in enumerate(src.pages, 1):
        it = dict(page=pg, name='p%d' % n, key='p%d' % n, opaque=True)
        try:
            img = _page_single_image(pg)
            if img is not None:
                raw = img.read_raw_bytes()
                w, h, d, gf = _decode_small(img, raw)
                g = d['g']
                th = Image.fromarray(g).resize((32, 32), Image.BILINEAR)
                it.update(opaque=False, w=w, h=h, g=g, rgb8=d['rgb8'], tn=d['tn'], small=np.asarray(th, dtype=np.float32), dh=dhash(g),
                          sha=hashlib.sha1(raw).hexdigest(), size=len(raw), gfull=gf, **d.get('full', {}),
                          xo=(lambda dst, o=img: dst.copy_foreign(o)))
                mb = [float(x) for x in pg.MediaBox]
                ratios.append((mb[2] - mb[0]) / w)
        except Exception:
            it['opaque'] = True
        items.append(it)
    pt = float(np.median(ratios)) if ratios else PT
    return items, pt


def build_from_pdf(src_path, out_pdf):
    """Re-arrange an existing PDF: merge one-photo-split-over-two-pages, drop blank/ad pages. Source is only read."""
    src = pikepdf.open(src_path)
    items, pt = pdf_items(src)
    rep = arrange(items, out_pdf, pt)
    rep['source'] = os.path.basename(src_path)
    rep['pages_in'] = len(items)
    rep['changed'] = not (rep['pages'] == len(items) and rep['spreads'] == 0 and not rep['blank'] and not rep['ad']
                          and not rep['dup'] and not rep['wide_dedup'])
    return rep


# ------------------------------------------------------------------ core
def arrange(infos, out_pdf, pt):
    """infos: items with name/key/xo (+ w,h,g,small,dh,sha when analysable, or opaque=True with a 'page').
    Writes out_pdf and returns a report dict."""
    from collections import Counter
    rep = dict(total=len(infos), blank=[], ad=[], dup=[], spreads=0, wide_dedup=0, wide_kept=0, odd_size=0,
               opaque=sum(1 for i in infos if i.get('opaque')))
    # --- drop blank / ad / exact duplicates (opaque pages are never touched)
    seen = {}
    for i in infos:
        if not i.get('opaque'):
            seen.setdefault(i['sha'], []).append(i)
    keep = []
    for i in infos:
        if i.get('opaque'):
            keep.append(i); continue
        if is_blank(i):
            rep['blank'].append(i['name']); continue
        if seen[i['sha']][0] is not i:   # exact duplicate: keep the first copy, drop later ones
            rep['dup'].append(i['name']); continue
        if is_ad_gray(i['g'], i.get('gfull')):
            rep['ad'].append(i['name']); continue
        keep.append(i)
    plain = [i for i in keep if not i.get('opaque')]
    if not plain:
        pw, ph = 1, 1
    else:
        pw, ph = Counter((i['w'], i['h']) for i in plain).most_common(1)[0][0]
    # --- spreads: greedy over consecutive kept images
    items = []  # ['single', info] | ['spread', L, R] | ['opaque', info]
    k = 0
    while k < len(keep):
        A = keep[k]
        if A.get('opaque'):
            items.append(['opaque', A]); k += 1; continue
        if k + 2 < len(keep) and not keep[k + 1].get('opaque') and not keep[k + 2].get('opaque'):
            # order  half, FULL, half : the full-width scan sits BETWEEN its two halves (the usual order is half, half, full)
            W, B = keep[k + 1], keep[k + 2]
            if ((A['w'], A['h']) == (B['w'], B['h']) and A['h'] > A['w'] and W['w'] > W['h'] * 1.15 and ph > pw):
                sc = [(wide_score(W, Lh, Rh), Lh, Rh) for Lh, Rh in ((A, B), (B, A))]
                best = min(sc, key=lambda t: t[0])
                if best[0] < 12:
                    Lh, Rh = best[1], best[2]
                    if W['w'] * W['h'] >= (Lh['w'] + Rh['w']) * Lh['h']:
                        items.append(['wide_from_pair', Lh, Rh, W])
                    else:
                        items.append(['spread', Lh, Rh])
                    rep['wide_dedup'] += 1
                    k += 3
                    continue
        if k + 1 < len(keep) and not keep[k + 1].get('opaque'):
            so = spread_order(A, keep[k + 1])
            if so:
                items.append(['spread', so[0], so[1]]); k += 2; continue
        items.append(['single', A]); k += 1
    # --- wide (full-width) duplicates of a detected spread
    final = []
    pairs = [it for it in items if it[0] == 'spread']
    wides = [it for it in items if it[0] == 'single' and it[1]['w'] > it[1]['h'] * 1.15 and ph > pw]
    removed_wide = set()
    for wd in wides:
        W = wd[1]
        for pr in pairs:
            L, R = pr[1], pr[2]
            if wide_matches_pair(W, L, R):
                if W['w'] * W['h'] >= (L['w'] + R['w']) * L['h']:
                    pr[0] = 'wide_from_pair'; pr.append(W)
                removed_wide.add(id(wd))
                rep['wide_dedup'] += 1
                break
    for it in items:
        if it[0] == 'single' and id(it) in removed_wide:
            continue
        final.append(it)
    rep['spreads'] = sum(1 for it in final if it[0] in ('spread',))
    rep['wide_kept'] = sum(1 for it in final if it[0] == 'wide_from_pair')
    # --- write pdf
    pdf = pikepdf.new()
    cache = {}

    def xo(info):
        if info['key'] not in cache:
            cache[info['key']] = info['xo'](pdf)
        return cache[info['key']]

    PW, PH = pw * pt, ph * pt
    for it in final:
        kind = it[0]
        if kind == 'opaque':
            pdf.pages.append(it[1]['page'])
            continue
        if kind == 'single':
            I = it[1]
            if I['w'] > I['h'] * 1.15 and ph > pw:   # lone wide scan = a spread
                bw, bh = PW * 2, PH
            else:
                bw, bh = PW, PH
            if (I['w'], I['h']) != (pw, ph) and bw == PW:
                rep['odd_size'] += 1
            x, y, w, h = fit(I['w'], I['h'], bw, bh)
            ops = [(xo(I), x, y, w, h)]
            page_w, page_h = bw, bh
        elif kind == 'spread':
            L, R = it[1], it[2]
            xl, yl, wl, hl = fit(L['w'], L['h'], PW, PH)
            xr, yr, wr, hr = fit(R['w'], R['h'], PW, PH)
            ops = [(xo(L), xl, yl, wl, hl), (xo(R), PW + xr, yr, wr, hr)]
            page_w, page_h = PW * 2, PH
        else:  # wide_from_pair
            I = it[3]
            x, y, w, h = fit(I['w'], I['h'], PW * 2, PH)
            ops = [(xo(I), x, y, w, h)]
            page_w, page_h = PW * 2, PH
        xd = pikepdf.Dictionary()
        cs = []
        for n, (x_obj, x, y, w, h) in enumerate(ops):
            nm = '/Im%d' % n
            xd[nm] = x_obj
            cs.append('q %.4f 0 0 %.4f %.4f %.4f cm %s Do Q' % (w, h, x, y, nm))
        page = pikepdf.Dictionary(Type=pikepdf.Name.Page,
                                  MediaBox=[0, 0, round(page_w, 2), round(page_h, 2)],
                                  Resources=pikepdf.Dictionary(XObject=xd),
                                  Contents=pikepdf.Stream(pdf, '\n'.join(cs).encode()))
        pdf.pages.append(pikepdf.Page(page))
    pdf.save(out_pdf)
    rep['pages'] = len(final)
    rep['pdf_mb'] = round(os.path.getsize(out_pdf) / 1048576, 1)
    rep['page_px'] = [pw, ph]
    return rep


if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'build':  # python photobook_lib.py build <folder> <out.pdf>
        rep, _ = build(sys.argv[2], sys.argv[3])
        print(json.dumps(rep, ensure_ascii=False))


def write_review_sheet(path, clear=True, pairs=None):
    """Side-by-side preview (left|right, in the order the rule guessed) of every pair the rule could not decide.
    Returns the list of (name_left, name_right). Look at the sheet; where the two halves are ONE picture, merge by hand
    (or rerun with the pair added); where they are two photos, do nothing."""
    custom = pairs is not None
    pairs = list(UNCERTAIN) if pairs is None else list(pairs)
    if clear and not custom:
        UNCERTAIN.clear()
    if not pairs:
        return []
    from PIL import ImageDraw
    tiles = []
    for n, (a, b) in enumerate(pairs):
        if 'tn' not in a or 'tn' not in b:
            continue
        ta, tb = a['tn'], b['tn']
        c = Image.new('RGB', (ta.shape[1] + tb.shape[1], max(ta.shape[0], tb.shape[0])), 'white')
        c.paste(Image.fromarray(ta), (0, 0)); c.paste(Image.fromarray(tb), (ta.shape[1], 0))
        d = ImageDraw.Draw(c); d.rectangle((0, 0, 190, 14), fill='black'); d.text((2, 1), '#%d %s | %s' % (n, a['name'], b['name']), fill='yellow')
        tiles.append(c)
    cols = 3
    w = max(t.width for t in tiles) + 4
    h = max(t.height for t in tiles) + 4
    sheet = Image.new('RGB', (cols * w, ((len(tiles) + cols - 1) // cols) * h), 'white')
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * w, (i // cols) * h))
    sheet.save(path, quality=80)
    return [(a['name'], b['name']) for a, b in pairs]
