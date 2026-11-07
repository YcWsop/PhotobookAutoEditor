"""Re-check the double-width spread pages of existing PDFs with the CURRENT spread rule and split the ones that fail
back into two single pages. Needed whenever the rule in photobook_lib.seam_match gets stricter.

    python verify_spreads.py a.pdf b.pdf              # report only: which spread pages fail
    python verify_spreads.py "F:\\books" --recursive   # every pdf in a folder tree
    python verify_spreads.py a.pdf --fix              # split failing spreads in place (original -> Recycle Bin, page count checked)

Only pages that look exactly like ones this toolkit made are judged: no text, exactly two images of the same size
placed side by side and a page twice as wide as one image. Other pages (publisher layouts, text) are never touched.
"""
import argparse, os, sys, json, tempfile, shutil, time
import numpy as np, pikepdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import photobook_lib as L


def spread_pages(pdf):
    """Yield (page index, [xobject_left, xobject_right]) for pages that look like a merged spread made by this toolkit."""
    for i, pg in enumerate(pdf.pages):
        try:
            xo = pg.Resources.get('/XObject')
            if xo is None or len(xo) != 2 or '/Font' in pg.Resources:
                continue
            xs = list(xo.values())
            if any(str(x.get('/Subtype')) != '/Image' for x in xs):
                continue
            if int(xs[0].Width) != int(xs[1].Width) or int(xs[0].Height) != int(xs[1].Height):
                continue
            mb = [float(v) for v in pg.MediaBox]
            ratio = (mb[2] - mb[0]) / (mb[3] - mb[1]) / (2 * int(xs[0].Width) / int(xs[0].Height))
            if abs(ratio - 1) > 0.02:
                continue
            content = pg.Contents.read_bytes() if not isinstance(pg.Contents, pikepdf.Array) else b''.join(c.read_bytes() for c in pg.Contents)
            if b'BT' in content or content.count(b' Do') != 2:
                continue
            # left/right = order of the 'Do' operators in the content stream
            names = [n for n in xo.keys()]
            order = sorted(names, key=lambda n: content.find(('/' + str(n).lstrip('/')).encode() + b' Do'))
            yield i, [xo[n] for n in order]
        except Exception:
            continue


def info_of(img):
    W, H, d, _ = L._decode_small(img, img.read_raw_bytes())
    return dict(w=W, h=H, rgb8=d['rgb8'])


def failing_pages(pdf):
    bad, total = [], 0
    for i, (xl, xr) in spread_pages(pdf):
        total += 1
        try:
            ok, _ = L.seam_match(info_of(xl), info_of(xr), ratio_thr=0.8, jump_thr=6, smooth_ok=True)
        except Exception:
            continue
        if not ok:
            bad.append(i)
    return bad, total


def split(src_path, bad, dst_path):
    pdf = pikepdf.open(src_path)
    try:
        return _split(pdf, bad, dst_path)
    finally:
        pdf.close()


def _split(pdf, bad, dst_path):
    new = pikepdf.new()
    bad = set(bad)
    spreads = {i: xs for i, xs in spread_pages(pdf)}
    for i, pg in enumerate(pdf.pages):
        if i not in bad:
            new.pages.append(pg)
            continue
        mb = [float(v) for v in pg.MediaBox]
        w, h = (mb[2] - mb[0]) / 2, mb[3] - mb[1]
        for x in spreads[i]:
            xn = new.copy_foreign(x)
            page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, w, h],
                                      Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=xn)),
                                      Contents=new.make_stream(('q %.4f 0 0 %.4f 0 0 cm /Im0 Do Q' % (w, h)).encode()))
            new.pages.append(pikepdf.Page(page))
    new.save(dst_path)
    return len(new.pages)


def main():
    sys.stdout.reconfigure(errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('inputs', nargs='+')
    ap.add_argument('--recursive', action='store_true')
    ap.add_argument('--fix', action='store_true')
    a = ap.parse_args()
    files = []
    for p in a.inputs:
        if os.path.isdir(p):
            for r, ds, fs in os.walk(p):
                ds.sort(); files += [os.path.join(r, n) for n in sorted(fs) if n.lower().endswith('.pdf')]
                if not a.recursive: break
        elif p.lower().endswith('.pdf'):
            files.append(p)
    res = {}
    for f in files:
        t = time.time()
        try:
            with pikepdf.open(f) as pdf:
                bad, total = failing_pages(pdf)
                n_in = len(pdf.pages)
        except Exception as e:
            print('ERR', os.path.basename(f)[:40], type(e).__name__, e, flush=True); continue
        res[f] = (bad, total)
        print('%3d/%3d spread pages fail  %s  (%ds)' % (len(bad), total, os.path.basename(f)[:50], time.time() - t), flush=True)
        if a.fix and bad:
            tmp = os.path.join(tempfile.mkdtemp(prefix='verify_'), 'o.pdf')
            n_out = split(f, bad, tmp)
            assert n_out == n_in + len(bad), (n_out, n_in, len(bad))
            L.recycle(f); shutil.move(tmp, f)
            print('   SPLIT %d pages -> %d pages' % (len(bad), n_out), flush=True)
    json.dump({k: v for k, v in res.items()}, open(os.path.join(tempfile.gettempdir(), 'verify_result.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    print('SUMMARY files', len(res), 'failing spread pages', sum(len(v[0]) for v in res.values()), 'of', sum(v[1] for v in res.values()))


if __name__ == '__main__':
    main()
