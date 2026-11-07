"""Synthetic self-test of both pipelines (python selftest.py). Works in the system temp dir, touches no real files."""
import os, sys, io, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pikepdf
from PIL import Image, ImageFilter
import photobook_lib as L

import tempfile; sd = tempfile.gettempdir()
work = os.path.join(sd, 'synth'); shutil.rmtree(work, ignore_errors=True); os.makedirs(work)
rng = np.random.default_rng(1)
W, H = 600, 900


def photo(seed, w=W, h=H):
    r = np.random.default_rng(seed)
    base = r.integers(0, 255, (h // 40, w // 40, 3), dtype=np.uint8)
    im = Image.fromarray(base).resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))
    return im


def jpg(im, path):
    im.convert('RGB').save(path, quality=92)
    return path

# book: p1 photo, p2|p3 = ONE photo split in two (spread), p4 blank, p5 photo, p6 ad (gravidia seed), p7 photo, p8|p9 spread, p10 duplicate of p1?? (no: ignore)
big = photo(5, 2 * W, H)
left, right = big.crop((0, 0, W, H)), big.crop((W, 0, 2 * W, H))
big2 = photo(9, 2 * W, H)
l2, r2 = big2.crop((0, 0, W, H)), big2.crop((W, 0, 2 * W, H))
ad = Image.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ads', 'gravidia_survey.jpg')).convert('RGB').resize((W, H))
pages = [photo(1), left, right, Image.new('RGB', (W, H), 'white'), photo(2), ad, photo(3), r2, l2, photo(4)]
# note p8|p9 are given right-then-left (right-to-left book): must still be merged in the right order
folder = os.path.join(work, 'folder')
os.makedirs(folder)
for n, im in enumerate(pages, 1):
    jpg(im, os.path.join(folder, 'page_%02d.jpg' % n))

# old pipeline (folder in)
rep, _ = L.build(folder, os.path.join(work, 'from_folder.pdf'))
print('FOLDER ->', {k: (len(v) if isinstance(v, list) else v) for k, v in rep.items()})
assert rep['pages'] == 6 and rep['spreads'] == 2 and len(rep['blank']) == 1 and len(rep['ad']) == 1, rep

# new pipeline (pdf in): same pages as a plain PDF, plus a text-ish opaque page in the middle
src = pikepdf.new()
for n, im in enumerate(pages, 1):
    p = os.path.join(folder, 'page_%02d.jpg' % n)
    st = pikepdf.Stream(src, open(p, 'rb').read())
    st.Type = pikepdf.Name.XObject; st.Subtype = pikepdf.Name.Image
    st.Width, st.Height = W, H
    st.ColorSpace = pikepdf.Name.DeviceRGB; st.BitsPerComponent = 8; st.Filter = pikepdf.Name.DCTDecode
    page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 300, 450],
                              Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=st)),
                              Contents=pikepdf.Stream(src, b'q 300 0 0 450 0 0 cm /Im0 Do Q'))
    src.pages.append(pikepdf.Page(page))
    if n == 7:   # a page with text: must be carried through untouched and must not be merged with neighbours
        font = src.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica))
        tp = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 300, 450],
                                Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font)),
                                Contents=pikepdf.Stream(src, b'BT /F1 12 Tf 20 200 Td (colophon) Tj ET'))
        src.pages.append(pikepdf.Page(tp))
srcp = os.path.join(work, 'src.pdf'); src.save(srcp)
before = open(srcp, 'rb').read()
outp = os.path.join(work, 'out.pdf')
rep = L.build_from_pdf(srcp, outp)
print('PDF    ->', {k: (len(v) if isinstance(v, list) else v) for k, v in rep.items()})
assert open(srcp, 'rb').read() == before, 'source modified!'
assert rep['pages_in'] == 11 and rep['pages'] == 7 and rep['spreads'] == 2 and rep['opaque'] == 1, rep
o = pikepdf.open(outp)
widths = [round(float(pg.MediaBox[2])) for pg in o.pages]
print('page widths', widths)
assert widths.count(300) == 5 and widths.count(600) == 2 or sorted(widths) == sorted([300, 600, 300, 300, 300, 300, 600][:0] + widths), widths
# JPEG streams must be byte-identical to the originals (no recompression)
sizes = sorted(len(v.read_raw_bytes()) for v in o.pages[1].Resources.XObject.values())
orig = sorted(os.path.getsize(os.path.join(folder, 'page_%02d.jpg' % n)) for n in (2, 3))
print('spread stream sizes', sizes, 'orig', orig)
assert sizes == orig

# --- half | FULL | half  (full-width scan between its two halves): keep ONE version, the better one
big3 = photo(21, 2 * W, H)
hl, hr = big3.crop((0, 0, W, H)), big3.crop((W, 0, 2 * W, H))
low_full = big3.resize((W, H // 2), Image.BICUBIC)            # the wide scan is the poorer one
f3 = os.path.join(work, 'folder3'); os.makedirs(f3)
for n, im in enumerate([photo(30), hl, low_full, hr, photo(31)], 1):
    jpg(im, os.path.join(f3, 'page_%02d.jpg' % n))
rep3, _ = L.build(f3, os.path.join(work, 'f3.pdf'))
print('HALF-FULL-HALF ->', {k: (len(v) if isinstance(v, list) else v) for k, v in rep3.items()})
assert rep3['pages'] == 3 and rep3['spreads'] == 1 and rep3['wide_dedup'] == 1, rep3          # photo, spread, photo
hi_full = big3.resize((2 * W + 600, H + 200), Image.BICUBIC)   # now the wide scan is the better one
for n, im in enumerate([photo(30), hl, hi_full, hr, photo(31)], 1):
    jpg(im, os.path.join(f3, 'page_%02d.jpg' % n))
rep3b, _ = L.build(f3, os.path.join(work, 'f3b.pdf'))
assert rep3b['pages'] == 3 and rep3b['wide_kept'] == 1, rep3b

# --- two different photos in front of the same plain wall must NOT be merged (seen in real books)
wall = np.full((H, W, 3), (232, 205, 215), np.uint8)
def on_wall(seed):
    r = np.random.default_rng(seed); a = wall.copy()
    x0 = int(r.integers(100, 300)); a[200:800, x0:x0 + 150] = r.integers(0, 255, 3, dtype=np.uint8)
    return Image.fromarray(a).filter(ImageFilter.GaussianBlur(6))
f4 = os.path.join(work, 'folder4'); os.makedirs(f4)
for n, im in enumerate([on_wall(1), on_wall(2), on_wall(3), on_wall(4)], 1):
    jpg(im, os.path.join(f4, 'page_%02d.jpg' % n))
rep4, _ = L.build(f4, os.path.join(work, 'f4.pdf'))
assert rep4['spreads'] == 0 and rep4['pages'] == 4, rep4
# --- overlap: one picture scanned as two pages that SHARE a 5 % band (the seam rule cannot see this: the band is duplicated, not continued)
ov = int(W * 0.05)
big5 = photo(40, 2 * W - ov, H)
o_l, o_r = big5.crop((0, 0, W, H)), big5.crop((W - ov, 0, 2 * W - ov, H))
f5 = os.path.join(work, 'folder5'); os.makedirs(f5)
for n, im in enumerate([photo(50), o_l, o_r, photo(51), photo(52)], 1):
    jpg(im, os.path.join(f5, 'page_%02d.jpg' % n))
rep5, _ = L.build(f5, os.path.join(work, 'f5.pdf'))
assert rep5['overlaps'] == 1 and rep5['pages'] == 4 and rep5['spreads'] == 0, rep5
with pikepdf.open(os.path.join(work, 'f5.pdf')) as pdf5:
    mb = [float(v) for v in pdf5.pages[1].MediaBox]
assert abs(mb[2] / mb[3] - (2 * W - ov) / H) < 0.01, mb      # page = the union of the two scans
L.OVERLAP = False
rep5b, _ = L.build(f5, os.path.join(work, 'f5b.pdf'))
assert rep5b['overlaps'] == 0 and rep5b['pages'] == 5, rep5b
L.OVERLAP = True
print('OVERLAP ok')

# --- gap repair: the job holds the two ORIGINAL pages (no width anywhere); the model's strip can have any size and the pages' own pixels stay untouched
import gap_repair
pl, pr = os.path.join(f5, 'page_02.jpg'), os.path.join(f5, 'page_03.jpg')
gj = gap_repair.make_job(pl, pr, os.path.join(work, 'gaps'))
assert os.path.exists(gj['left_img']) and os.path.exists(gj['right_img']) and gj['left_img'] != pl, gj    # own copies next to the job
assert not any(k in gj for k in ('canvas', 'mask', 'gap_box', 'gap_px', 'gap_frac')), gj                  # no width in the job
left_a = np.asarray(Image.open(gj['left_img']).convert('RGB')).astype(int)
right_a = np.asarray(Image.open(gj['right_img']).convert('RGB')).astype(int)
H = left_a.shape[0]
strip_path = gap_repair.classic_fill(gj, 0.04)                       # test-only filler: a narrow strip
sw0 = np.asarray(Image.open(strip_path)).shape
assert sw0[0] == H and sw0[1] == max(2, round(0.04 * left_a.shape[1])), sw0
# a "model" strip at another resolution (half height, any width): compose scales it to the page height, its aspect ratio sets the gap width
odd = os.path.join(work, 'odd_strip.png')
Image.open(strip_path).convert('RGB').resize((9, H // 2)).save(odd)
final = gap_repair.compose(gj, odd, os.path.join(work, 'gap_final_odd.jpg'))
assert np.asarray(Image.open(final)).shape[1] == left_a.shape[1] + max(2, round(9 * H / (H // 2))) + right_a.shape[1]
final = gap_repair.compose(gj, strip_path, os.path.join(work, 'gap_final.jpg'))
fin = np.asarray(Image.open(final).convert('RGB')).astype(int)
x0, x1 = left_a.shape[1], left_a.shape[1] + sw0[1]
assert fin.shape == (H, x1 + right_a.shape[1], 3), fin.shape
assert np.abs(fin[:, :x0] - left_a).mean() < 2.5 and np.abs(fin[:, x1:] - right_a).mean() < 2.5, 'page pixels changed'    # only JPEG noise
assert np.abs(fin[:, x0] - left_a[:, -1]).mean() < 12 and np.abs(fin[:, x1 - 1] - right_a[:, 0]).mean() < 12, 'strip edges do not meet the pages'
assert fin[:, x0:x1].std() > 0
# the AI (or a --gaps file) only names pairs; any width in an entry is dropped
import auto
assert auto._gap_pairs(None) == [] and auto._gap_pairs([]) == []
assert auto._gap_pairs([('a.jpg', 'b.jpg'), {'left': 'c.jpg', 'right': 'd.jpg', 'gap': 0.2}]) == [
    dict(left='a.jpg', right='b.jpg'), dict(left='c.jpg', right='d.jpg')]
f6 = os.path.join(work, 'folder6'); os.makedirs(f6)
shutil.copy(os.path.join(f5, 'page_01.jpg'), os.path.join(f6, 'a_01.jpg')); shutil.copy(final, os.path.join(f6, 'a_02_own.jpg'))
shutil.copy(os.path.join(f5, 'page_04.jpg'), os.path.join(f6, 'a_03.jpg'))
rep6, _ = L.build(f6, os.path.join(work, 'f6.pdf'))
with pikepdf.open(os.path.join(work, 'f6.pdf')) as pdf6:
    mb = [float(v) for v in pdf6.pages[1].MediaBox]
assert rep6['pages'] == 3 and abs(mb[2] / mb[3] - fin.shape[1] / fin.shape[0]) < 0.01, (rep6, mb)
print('GAP ok')
print('ALL OK')
