"""补缝：两页本来是一张图，但中间缺了一条（老写真常见）。

流程（auto.py 在“接缝合并”之后调用，也可以单独用）：
  1. make_job()   把左页、空缝、右页拼成一张带透明通道的大图（缝隙处 alpha = 0），同时写出蒙版和一份 json 任务单
  2. 谁来补：     a) <名字>.filled.png 已经放在任务旁边（人工或别的程序补好的）
                  b) ai_hook.fill_gap(job)：有多模态/图像编辑能力的模型来补，返回补好的图片路径
                  c) classic_fill(job)：不用任何模型，只对很窄的缝做传统修补（仅用于流程测试和窄缝）
  3. compose()    只取补好的图在缝隙里的部分（边缘羽化几个像素），缝隙以外保持原图像素不变，存成一张 JPEG

没有人补的任务单留在 <书名>.gaps/ 里，书本身照旧是两页，什么都不会被改。

    python gap_repair.py make 左页.jpg 右页.jpg --gap 0.04 --out DIR      # 只生成透明带大图 + 蒙版 + 任务单
    python gap_repair.py classic DIR/左页__右页.json                      # 传统修补，写出 .filled.png
    python gap_repair.py compose DIR/左页__右页.json 补好.png 成品.jpg    # 把补好的图合成为成品
"""
import json, os, sys
import numpy as np
from PIL import Image, ImageFilter

PROMPT = ('This image is one photograph from a photobook that was scanned as two pages. The two halves are in place; the fully '
          'transparent vertical band between them is missing picture content. Fill ONLY the transparent band so that the picture '
          'reads as one continuous photograph: continue lines, edges, background, light and texture across the band. Do not change '
          'any opaque pixel, do not add or remove people or objects, do not add text.')


def _stem(p):
    return os.path.splitext(os.path.basename(p))[0]


def make_job(left_path, right_path, gap_frac, out_dir, name=None):
    """-> job dict (also written to <out_dir>/<name>.json). gap_frac = width of the missing band as a fraction of ONE page width."""
    os.makedirs(out_dir, exist_ok=True)
    name = name or '%s__%s' % (_stem(left_path), _stem(right_path))
    with Image.open(left_path) as a, Image.open(right_path) as b:
        left, right = a.convert('RGB'), b.convert('RGB')
    if right.height != left.height:
        right = right.resize((max(1, round(right.width * left.height / right.height)), left.height), Image.LANCZOS)
    gap = max(2, round(gap_frac * left.width))
    W, H = left.width + gap + right.width, left.height
    canvas = np.zeros((H, W, 4), np.uint8)                 # RGBA, everything transparent to begin with
    canvas[:, :left.width, :3] = np.asarray(left)
    canvas[:, :left.width, 3] = 255
    canvas[:, left.width + gap:, :3] = np.asarray(right)
    canvas[:, left.width + gap:, 3] = 255
    mask = np.zeros((H, W), np.uint8)                       # white = to be filled
    mask[:, left.width:left.width + gap] = 255
    base = os.path.join(out_dir, name)
    Image.fromarray(canvas, 'RGBA').save(base + '.png')
    Image.fromarray(mask, 'L').save(base + '.mask.png')
    job = dict(name=name, canvas=base + '.png', mask=base + '.mask.png', filled=base + '.filled.png',
               gap_box=[left.width, 0, left.width + gap, H], gap_px=gap, size=[W, H],
               left_img=left_path, right_img=right_path, prompt=PROMPT, json=base + '.json')
    json.dump(job, open(job['json'], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return job


def compose(job, filled_path, out_path, feather=None):
    """canvas + filled picture -> one RGB JPEG. Only the gap (plus a short feathered edge) is taken from `filled_path`,
    so the pixels of both pages stay exactly as scanned; the filled picture may have any size (it is scaled to the canvas)."""
    canvas = np.asarray(Image.open(job['canvas']).convert('RGBA'))
    H, W = canvas.shape[:2]
    with Image.open(filled_path) as f:
        filled = f.convert('RGB')
    if filled.size != (W, H):
        filled = filled.resize((W, H), Image.LANCZOS)
    x0, _, x1, _ = job['gap_box']
    gap = x1 - x0
    feather = max(3, round(gap * 0.4)) if feather is None else feather
    cols = np.arange(W, dtype=np.float32)
    m = np.clip(1 - np.maximum(np.maximum(x0 - cols, cols - (x1 - 1)), 0) / float(feather + 1), 0, 1)   # 1 in the gap, ramps to 0
    m = m[None, :, None]
    out = canvas[:, :, :3].astype(np.float32) * (1 - m) + np.asarray(filled, np.float32) * m
    Image.fromarray(np.clip(out + 0.5, 0, 255).astype(np.uint8)).save(out_path, quality=95, subsampling=0)
    return out_path


def classic_fill(job):
    """no model: OpenCV inpainting of the band, on a crop around it. Fine for a few pixels, blurry for wide gaps.
    Writes job['filled'] and returns it."""
    import cv2
    canvas = np.asarray(Image.open(job['canvas']).convert('RGBA'))
    H, W = canvas.shape[:2]
    x0, _, x1, _ = job['gap_box']
    pad = max(8, 3 * (x1 - x0))
    c0, c1 = max(0, x0 - pad), min(W, x1 + pad)
    crop = np.ascontiguousarray(canvas[:, c0:c1, :3][:, :, ::-1])
    mask = np.zeros(crop.shape[:2], np.uint8)
    mask[:, x0 - c0:x1 - c0] = 255
    done = cv2.inpaint(crop, mask, 5, cv2.INPAINT_TELEA)
    out = canvas[:, :, :3].copy()
    out[:, c0:c1] = done[:, :, ::-1]
    Image.fromarray(out).save(job['filled'])
    return job['filled']


def load_job(path):
    return json.load(open(path, encoding='utf-8'))


def main():
    sys.stdout.reconfigure(errors='replace')
    if len(sys.argv) < 2 or sys.argv[1] not in ('make', 'classic', 'compose'):
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == 'make':
        gap, out, pos, k = 0.04, '.', [], 0
        while k < len(args):
            if args[k] == '--gap':
                gap = float(args[k + 1]); k += 2
            elif args[k] == '--out':
                out = args[k + 1]; k += 2
            else:
                pos.append(args[k]); k += 1
        job = make_job(pos[0], pos[1], gap, out)
        print(job['canvas'], job['mask'], job['json'])
    elif cmd == 'classic':
        print(classic_fill(load_job(args[0])))
    else:
        print(compose(load_job(args[0]), args[1], args[2]))


if __name__ == '__main__':
    main()
