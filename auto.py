"""PhotobookAutoEditor entry point: a folder of images, or one or more complete PDFs (not zips) -> one tidied PDF per book.

    python auto.py <folder-or-pdf> [<folder-or-pdf> ...] [--out DIR] [--seam fast|full] [--no-trim] [--no-overlap] [--gaps FILE] [--force]

阶段（详见 README）:
  1 prepare   取图（文件夹顺序，或 PDF 每页的每张图），去空白/广告/重复页，去白边，把主要比例的页统一成该组最大尺寸（没改动的 JPEG 原样复制）
  2 seam      读图并合并被拆成两页的一张图；重叠页（两页共用一条窄带）也在这一步用 overlap.py 找出并拼好。
              --seam fast = 缩小尺寸的图（默认），--seam full = 完整图 + 原来的 2 像素规则（慢；CUDA 版计划中，未写）
  3 flat      接缝是纯色的页对不自动合并；颜色几乎相同的进队列
  4 review    规则判不了的页对（uncertain + flat）交给 ai_hook.review()，返回的页对被合并，其余留在联系表里
  5 gap       中间缺了一条的两页（--gaps 文件点名，或 ai_hook.gaps() 返回页对，不报缝宽）：做成任务单（左右两张原图，gap_repair.py），
              交给 ai_hook.fill_gap() 补出中间那一条（宽度由模型定），或读取已补好的 .filled.png；
              没人补的任务单留在 <书名>.gaps/，书保持两页
输出: <out>/<书名>.pdf，联系表 <书名>.uncertain.jpg / .flat.jpg，队列 <书名>.queue.json（+ <书名>.work/ 页面图），补缝任务 <书名>.gaps/
"""
import argparse, json, os, re, shutil, sys, tempfile
from collections import Counter
import numpy as np
import pikepdf
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import photobook_lib as L
import seam_load
import ai_hook
import gap_repair

PAT = re.compile(rb'([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+cm\s+/(\w+)\s+Do')
WHITE, MAX_TRIM = 240, 0.08


def pdf_images(path, dst):
    """every picture of every page, page by page, left to right -> numbered files in dst; returns (paths, pages without picture)"""
    out, skipped = [], 0
    with pikepdf.open(path) as pdf:
        for n, pg in enumerate(pdf.pages, 1):
            xo = pg.get('/Resources', {}).get('/XObject', {})
            imgs = {k: v for k, v in xo.items() if str(v.get('/Subtype')) == '/Image'}
            if not imgs:
                skipped += 1
                continue
            c = pg.Contents.read_bytes() if not isinstance(pg.Contents, pikepdf.Array) else b''.join(s.read_bytes() for s in pg.Contents)
            order = ['/' + m[6].decode() for m in sorted(PAT.findall(c), key=lambda m: float(m[4]))]
            names = [k for k in order if k in imgs] or list(imgs)
            for k, name in enumerate(dict.fromkeys(names)):
                x = imgs[name]
                p = os.path.join(dst, 'src_%05d_%d.jpg' % (n, k))
                if str(x.get('/Filter')) == '/DCTDecode':
                    open(p, 'wb').write(x.read_raw_bytes())
                else:
                    pikepdf.PdfImage(x).as_pil_image().convert('RGB').save(p, quality=95, subsampling=0)
                out.append(p)
    return out, skipped


def trim_box(im):
    """crop box without the near-white border (at most MAX_TRIM of each side)"""
    g = np.asarray(im.convert('L'))
    h, w = g.shape
    rows = (g >= WHITE).mean(axis=1) > 0.995
    cols = (g >= WHITE).mean(axis=0) > 0.995

    def lead(v, limit):
        n = 0
        while n < limit and n < len(v) and v[n]:
            n += 1
        return n
    t, b = lead(rows, int(h * MAX_TRIM)), lead(rows[::-1], int(h * MAX_TRIM))
    l, r = lead(cols, int(w * MAX_TRIM)), lead(cols[::-1], int(w * MAX_TRIM))
    return (l, t, w - r, h - b)


def prepare(paths, work, trim=True):
    """stage 1 -> (final image files in `work`, report)"""
    rep = dict(images=len(paths), blank=0, ad=0, dup=0, trimmed=0, resized=0)
    infos, seen = [], set()
    for p in paths:
        i = L.load_info(p)
        if L.is_blank(i):
            rep['blank'] += 1
            continue
        if i['sha'] in seen:
            rep['dup'] += 1
            continue
        seen.add(i['sha'])
        if L.is_ad_gray(i['g'], i.get('gfull')):
            rep['ad'] += 1
            continue
        infos.append((p, i['w'], i['h']))
    boxes = {}
    for p, w, h in infos:
        bx = None
        if trim:
            with Image.open(p) as im:
                b = trim_box(im.convert('RGB'))
            bx = b if (b[2] - b[0], b[3] - b[1]) != (w, h) else None
        boxes[p] = bx
    # target size: the largest ORIGINAL size among the pages of the most common aspect
    key = lambda w, h: round((w / h) / 0.02)
    modal = Counter(key(w, h) for _, w, h in infos).most_common(1)[0][0] if infos else None
    cls = [(w, h) for _, w, h in infos if key(w, h) == modal]
    TW, TH = max(cls, key=lambda s: s[0] * s[1]) if cls else (0, 0)
    out = []
    for n, (p, w, h) in enumerate(infos, 1):
        dst = os.path.join(work, 'p%04d.jpg' % n)
        bx = boxes[p]
        inclass = key(w, h) == modal
        if bx is None and (not inclass or (w, h) == (TW, TH)) and p.lower().endswith(('.jpg', '.jpeg')):
            shutil.copy(p, dst)            # nothing to change: the original JPEG bytes
        else:
            with Image.open(p) as im:
                im = im.convert('RGB')
                if bx is not None:
                    im = im.crop(bx)
                    rep['trimmed'] += 1
                cw, ch = im.size
                if inclass and abs((cw / ch) / (TW / TH) - 1) < 0.03 and (cw, ch) != (TW, TH):
                    im = im.resize((TW, TH), Image.LANCZOS)
                    rep['resized'] += 1
                im.save(dst, quality=95, subsampling=0)
        out.append(dst)
    rep['kept'] = len(out)
    rep['target'] = [TW, TH]
    return out, rep


def _read_gaps(path):
    """--gaps file: JSON list of {"left": "p0007.jpg", "right": "p0008.jpg"} or [left, right]. Only WHICH pages: no width, the model
    that fills the gap decides it."""
    return _gap_pairs(json.load(open(path, encoding='utf-8')))


def _gap_pairs(result):
    """list of dicts / [left, right] lists (a --gaps file or the result of ai_hook.gaps()) -> [{'left', 'right'}]. Anything else in an entry,
    a width included, is dropped: the width cannot be estimated reliably and is not an input."""
    out = []
    for g in result or []:
        l, r = (g['left'], g['right']) if isinstance(g, dict) else (g[0], g[1])
        out.append(dict(left=l, right=r))
    return out


def _repair_gaps(reqs, pages, gaps_dir):
    """write a task (the two original pages) for each request; the strip between them comes from <task>.filled.png or ai_hook.fill_gap()
    (the model decides its width).
    A repaired pair becomes ONE page file '<left stem>_own.jpg' in `pages`. -> (repaired, still waiting)"""
    done = pending = 0
    for g in reqs:
        lp, rp = os.path.join(pages, g['left']), os.path.join(pages, g['right'])
        if not (os.path.exists(lp) and os.path.exists(rp)):
            print('   gap: page not found, skipped:', g['left'], g['right'], flush=True)
            continue
        job = gap_repair.make_job(lp, rp, gaps_dir)
        filled = job['filled'] if os.path.exists(job['filled']) else ai_hook.fill_gap(job)
        if not filled or not os.path.exists(filled):
            pending += 1
            continue
        out = os.path.join(pages, os.path.splitext(g['left'])[0] + L.OWN_TAG + '.jpg')
        gap_repair.compose(job, filled, out)
        os.remove(lp); os.remove(rp)
        done += 1
    return done, pending


def process(unit, out_dir, seam, trim, gap_file=None, force=False):
    name = os.path.basename(unit.rstrip('\\/'))
    name = os.path.splitext(name)[0] if os.path.isfile(unit) else name
    out_pdf = os.path.join(out_dir, name + '.pdf')
    if os.path.exists(out_pdf) and not force:
        print('SKIP (output exists, use --force to redo):', name)
        return
    if os.path.exists(out_pdf):
        os.remove(out_pdf)
    work = tempfile.mkdtemp(prefix='auto_')
    try:
        raw = os.path.join(work, 'raw')
        os.makedirs(raw)
        if os.path.isfile(unit):
            paths, skipped = pdf_images(unit, raw)
        else:
            paths, skipped = L.list_images(unit), 0
        pages = os.path.join(work, 'pages')
        os.makedirs(pages)
        files, rep = prepare(paths, pages, trim)
        rep['pages_without_image'] = skipped
        print('1 prepare:', json.dumps(rep), flush=True)
        seam_load.FULL = (seam == 'full')
        L.UNCERTAIN.clear(); L.FLAT.clear(); L.FORCE.clear()
        r, _ = L.build(pages, out_pdf)
        pairs_u = [(a, b) for a, b in L.UNCERTAIN]
        pairs_f = [(a, b) for a, b, _ in L.FLAT]
        queue = [dict(kind='uncertain', left=a['name'], right=b['name'], left_img=a['path'], right_img=b['path']) for a, b in pairs_u]
        queue += [dict(kind='flat', left=a['name'], right=b['name'], left_img=a['path'], right_img=b['path'],
                       stats={k: round(float(v), 3) for k, v in st.items()}) for a, b, st in L.FLAT]
        print('2 seam (%s): %d spread(s), %d overlap pair(s), %d page(s) | 3 flat: %d queued | uncertain: %d' % (
            seam, r['spreads'], r['overlaps'], r['pages'], len(pairs_f), len(pairs_u)), flush=True)
        L.write_review_sheet(os.path.join(out_dir, name + '.uncertain.jpg'), clear=False, pairs=pairs_u)
        L.write_review_sheet(os.path.join(out_dir, name + '.flat.jpg'), clear=False, pairs=pairs_f)
        keep = os.path.join(out_dir, name + '.work')
        gap_reqs = _read_gaps(gap_file) if gap_file else []
        if queue or gap_reqs:                # keep the page images the queue / gap tasks refer to
            shutil.rmtree(keep, ignore_errors=True)
            shutil.copytree(pages, keep)
        rebuild = False
        if queue:
            merges = ai_hook.review(queue) or []
            if merges:
                L.FORCE.update({frozenset(m): tuple(m) for m in merges})
                rebuild = True
                print('4 review: %d pair(s) merged by the reviewer' % len(merges), flush=True)
            for q in queue:   # the queue points at the kept copies of the page images
                for k in ('left_img', 'right_img'):
                    q[k] = os.path.join(keep, os.path.basename(q[k]))
            json.dump(queue, open(os.path.join(out_dir, name + '.queue.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        gap_reqs += _gap_pairs(ai_hook.gaps(queue, [os.path.join(pages, os.path.basename(f)) for f in files]))
        if gap_reqs:
            done, pending = _repair_gaps(gap_reqs, pages, os.path.join(out_dir, name + '.gaps'))
            rebuild = rebuild or done > 0
            print('5 gap: %d repaired, %d task(s) waiting in %s' % (done, pending, name + '.gaps'), flush=True)
        if rebuild:
            os.remove(out_pdf)
            L.UNCERTAIN.clear(); L.FLAT.clear()
            r, _ = L.build(pages, out_pdf)
            print('   rebuilt: %d page(s)' % r['pages'], flush=True)
        L.UNCERTAIN.clear(); L.FLAT.clear()
        print('OK', name, '->', out_pdf, flush=True)
    except Exception as e:
        print('FAILED:', name, type(e).__name__, e, flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    sys.stdout.reconfigure(errors='replace')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('inputs', nargs='+', help='folders of images and/or PDF files')
    ap.add_argument('--out', help='output folder (default: <first input dir>/arranged)')
    ap.add_argument('--seam', choices=('fast', 'full'), default='fast')
    ap.add_argument('--no-trim', action='store_true', help='do not trim white borders')
    ap.add_argument('--no-overlap', action='store_true', help='do not look for overlapping pages')
    ap.add_argument('--gaps', help='JSON file listing pairs with a missing band between them (see gap_repair.py)')
    ap.add_argument('--force', action='store_true', help='redo books whose output PDF already exists')
    a = ap.parse_args()
    L.OVERLAP = not a.no_overlap
    for u in a.inputs:
        if u.lower().endswith('.zip'):
            sys.exit('zip files are not accepted, unpack them first: ' + u)
    first = os.path.abspath(a.inputs[0].rstrip('\\/'))
    out = a.out or os.path.join(os.path.dirname(first), 'arranged')
    os.makedirs(out, exist_ok=True)
    for u in a.inputs:
        process(u, out, a.seam, not a.no_trim, a.gaps, a.force)


if __name__ == '__main__':
    main()
