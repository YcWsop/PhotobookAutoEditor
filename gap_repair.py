"""补缝：两页本来是一张图，但中间缺了一条（老写真常见）。

缝宽不估计也不指定：把左右两张原图交给模型，让它自己补出中间那一条，宽度由它定。

流程（auto.py 在“接缝合并”之后调用，也可以单独用）：
  1. make_job()   写一份 json 任务单：左、右两张原图（复制一份放在任务旁边）的路径 + 给模型的提示词
  2. 谁来补：     a) <名字>.filled.png 已经放在任务旁边（人工或别的程序补好的：只含【缺失的中间那一条】）
                  b) ai_hook.fill_gap(job)：把两张原图交给多模态/图像生成模型，返回中间那一条的图片路径
                  c) classic_fill(job)：不用任何模型，传统修补出一条窄缝（仅用于流程测试，宽度是测试参数）
  3. compose()    左页 | 补出的一条 | 右页 拼成一张 JPEG。两页的像素原样不动；只在补出的那一条里做亮度对接，
                  让它与两边的边缘颜色连上。

没有人补的任务单留在 <书名>.gaps/ 里，书本身照旧是两页，什么都不会被改。

    python gap_repair.py make 左页.jpg 右页.jpg --out DIR            # 只写任务单
    python gap_repair.py classic DIR/左页__右页.json [--width 0.04]   # 传统修补（测试用），写出 .filled.png
    python gap_repair.py compose DIR/左页__右页.json 中间一条.png 成品.jpg
"""
import json, os, shutil, sys
import numpy as np
from PIL import Image

# The model gets the two ORIGINAL pages (job['left_img'], job['right_img']) and draws the missing middle strip itself,
# including its width (nobody estimates or fixes the width: it could not be read off the pictures reliably).
PROMPT = ('Image 1 is the left page and image 2 is the right page of ONE photograph in a photobook. The spread was scanned as two '
          'pages and a narrow vertical strip between them is missing. Draw ONLY the missing strip: it has the same height as the '
          'pages, you choose its width so that everything crossing it keeps natural proportions. Its left edge must continue the '
          'right edge of image 1 and its right edge must continue the left edge of image 2: lines, contours, background, light, '
          'grain and texture. Do not add or remove people or objects, do not add text.')


def _stem(p):
    return os.path.splitext(os.path.basename(p))[0]


def make_job(left_path, right_path, out_dir='.', name=None):
    """-> job dict (also written to <out_dir>/<name>.json). The two original pages are copied next to the job (the page images of a
    run live in a temp folder), so a waiting task can still be composed later. `filled` is where the strip picture is expected."""
    os.makedirs(out_dir, exist_ok=True)
    name = name or '%s__%s' % (_stem(left_path), _stem(right_path))
    base = os.path.join(out_dir, name)
    copies = []
    for side, src in (('left', left_path), ('right', right_path)):
        dst = '%s.%s%s' % (base, side, os.path.splitext(src)[1] or '.jpg')
        shutil.copyfile(src, dst)
        copies.append(dst)
    job = dict(name=name, left_img=copies[0], right_img=copies[1], filled=base + '.filled.png', prompt=PROMPT, json=base + '.json')
    json.dump(job, open(job['json'], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return job


def _smooth_rows(a, sigma=4):
    from scipy.ndimage import gaussian_filter1d
    return gaussian_filter1d(a, sigma, axis=0, mode='nearest')


def compose(job, strip_path, out_path):
    """left page | strip | right page -> one RGB JPEG. The strip picture may have any size: it is scaled to the pages' height
    (its width follows its own aspect ratio, so the model decides how wide the gap is). The pages' pixels stay exactly as scanned;
    only the strip gets a smooth brightness/colour correction so that its two edges meet the pages' edge columns."""
    with Image.open(job['left_img']) as a, Image.open(job['right_img']) as b, Image.open(strip_path) as s:
        left, right, strip = a.convert('RGB'), b.convert('RGB'), s.convert('RGB')
    H = left.height
    if right.height != H:
        right = right.resize((max(1, round(right.width * H / right.height)), H), Image.LANCZOS)
    sw = max(2, round(strip.width * H / strip.height))
    strip = strip.resize((sw, H), Image.LANCZOS)
    L, R, S = (np.asarray(x, np.float32) for x in (left, right, strip))
    dl = _smooth_rows(L[:, -3:].mean(1) - S[:, :3].mean(1))          # rows x 3: what the strip's left edge is off by
    dr = _smooth_rows(R[:, :3].mean(1) - S[:, -3:].mean(1))          # ... and its right edge
    t = np.linspace(0, 1, sw, dtype=np.float32)[None, :, None]
    S = np.clip(S + (1 - t) * dl[:, None, :] + t * dr[:, None, :], 0, 255)
    out = np.concatenate([np.asarray(left, np.float32), S, R], axis=1)
    Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8)).save(out_path, quality=95, subsampling=0)
    return out_path


def classic_fill(job, width=0.04):
    """no model, for flow tests only: OpenCV inpainting of a `width` (fraction of one page width) band between the pages, on a crop
    around it. Fine for a few pixels, blurry for more. Writes the STRIP to job['filled'] and returns it."""
    import cv2
    with Image.open(job['left_img']) as a, Image.open(job['right_img']) as b:
        left, right = a.convert('RGB'), b.convert('RGB')
    H = left.height
    if right.height != H:
        right = right.resize((max(1, round(right.width * H / right.height)), H), Image.LANCZOS)
    gap = max(2, round(width * left.width))
    pad = max(8, 3 * gap)
    lc = np.asarray(left)[:, -pad:, ::-1]
    rc = np.asarray(right)[:, :pad, ::-1]
    crop = np.ascontiguousarray(np.concatenate([lc, np.zeros((H, gap, 3), np.uint8), rc], axis=1))
    mask = np.zeros(crop.shape[:2], np.uint8)
    mask[:, lc.shape[1]:lc.shape[1] + gap] = 255
    done = cv2.inpaint(crop, mask, 5, cv2.INPAINT_TELEA)
    Image.fromarray(np.ascontiguousarray(done[:, lc.shape[1]:lc.shape[1] + gap, ::-1])).save(job['filled'])
    return job['filled']


def load_job(path):
    return json.load(open(path, encoding='utf-8'))


def main():
    sys.stdout.reconfigure(errors='replace')
    if len(sys.argv) < 2 or sys.argv[1] not in ('make', 'classic', 'compose'):
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == 'make':
        out, pos, k = '.', [], 0
        while k < len(args):
            if args[k] == '--out':
                out = args[k + 1]; k += 2
            else:
                pos.append(args[k]); k += 1
        print(make_job(pos[0], pos[1], out)['json'])
    elif cmd == 'classic':
        width = float(args[args.index('--width') + 1]) if '--width' in args else 0.04
        print(classic_fill(load_job(args[0]), width))
    else:
        print(compose(load_job(args[0]), args[1], args[2]))


if __name__ == '__main__':
    main()
