"""Merge neighbouring single-picture pages of a PDF into double-width spread pages BY DECISION (after somebody looked at the
UNCERTAIN contact sheet and saw that the two halves are one picture).

    python merge_pages.py book.pdf 98-99 120-121        # page numbers as shown in the sheet (#n  p98 | p99), left-right order
    python merge_pages.py book.pdf 31-30                # right-to-left book: sheet says 'p31 | p30', left page is 31
    python merge_pages.py book.pdf 98-99 --dry

The pages must be plain one-image pages of identical size. The PDF is rewritten in place (original -> Recycle Bin).
"""
import argparse, os, sys, tempfile, shutil
import pikepdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import photobook_lib as L


def one_image(pg):
    xo = pg.get('/Resources', {}).get('/XObject')
    if xo is None or len(xo) != 1:
        return None
    x = list(xo.values())[0]
    return x if str(x.get('/Subtype')) == '/Image' else None


def merge(src_path, pairs, dst_path):
    pdf = pikepdf.open(src_path)
    try:
        # pairs are (left page, right page); right-to-left books give e.g. 31-30 (left is page 31)
        lefts = {min(a, b) - 1: (a - 1, b - 1) for a, b in pairs}
        skip = {max(a, b) - 1 for a, b in pairs}
        new = pikepdf.new()
        for i, pg in enumerate(pdf.pages):
            if i in skip:
                continue
            if i not in lefts:
                new.pages.append(pg); continue
            li, ri = lefts[i]
            pa, pb = pdf.pages[li], pdf.pages[ri]
            xa, xb = one_image(pa), one_image(pb)
            if xa is None or xb is None:
                raise ValueError('page %d or %d is not a plain one-image page' % (li + 1, ri + 1))
            wa, ha = [float(v) for v in (pa.MediaBox[2] - pa.MediaBox[0], pa.MediaBox[3] - pa.MediaBox[1])]
            wb, hb = [float(v) for v in (pb.MediaBox[2] - pb.MediaBox[0], pb.MediaBox[3] - pb.MediaBox[1])]
            if abs(wa - wb) > 1 or abs(ha - hb) > 1:
                raise ValueError('pages %d and %d differ in size' % (li + 1, ri + 1))
            names = []
            xd = pikepdf.Dictionary()
            for k, x in enumerate((xa, xb)):
                n = '/Im%d' % k
                xd[n] = new.copy_foreign(x)
            content = ('q %.4f 0 0 %.4f 0 0 cm /Im0 Do Q q %.4f 0 0 %.4f %.4f 0 cm /Im1 Do Q' % (wa, ha, wb, hb, wa)).encode()
            page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, wa + wb, ha],
                                      Resources=pikepdf.Dictionary(XObject=xd), Contents=new.make_stream(content))
            new.pages.append(pikepdf.Page(page))
        new.save(dst_path)
        return len(new.pages), len(pdf.pages)
    finally:
        pdf.close()


def main():
    sys.stdout.reconfigure(errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('pdf')
    ap.add_argument('pairs', nargs='+', help='left-right page numbers, e.g. 98-99')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    pairs = []
    for t in a.pairs:
        x, y = t.split('-'); x, y = int(x), int(y)
        if abs(y - x) != 1:
            sys.exit('only neighbouring pages (n-(n+1), or n-(n-1) for right-to-left): %s' % t)
        pairs.append((x, y))
    tmp = os.path.join(tempfile.mkdtemp(prefix='merge_'), 'o.pdf')
    n_out, n_in = merge(a.pdf, pairs, tmp)
    assert n_out == n_in - len(pairs), (n_out, n_in)
    print('%d pages -> %d pages' % (n_in, n_out))
    if a.dry:
        os.remove(tmp); return
    L.recycle(a.pdf); shutil.move(tmp, a.pdf)
    print('merged', a.pairs)


if __name__ == '__main__':
    main()
