"""Small-image loading for the seam test (see seam_match in photobook_lib).

A page is decoded ONCE, at the smallest JPEG scale (1/1, 1/2, 1/4, 1/8: libjpeg skips most of the inverse DCT) that still has
THUMB_H rows; everything the pipeline needs is derived from that small picture:
  rgb8  RGB at the analysis size (AN_H rows, the same for every page whatever its pixel size); the seam test reads it
  g     gray version of rgb8 (blank / duplicate / ad pre-checks)
  tn    thumbnail for the review sheets
Full resolution is only needed for the QR-code check of mostly white pages and is loaded lazily (gfull in photobook_lib).
"""
import io, math
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from PIL import Image

THREADS = 4     # fixed number of worker threads for the per-page decoding (Pillow's decoder releases the GIL, so threads scale)
_POOL = None


def pool():
    """the one shared pool of THREADS worker threads (created on first use)"""
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(THREADS, thread_name_prefix='decode')
    return _POOL


FULL = False    # True: decode at full size and also keep the full-resolution edge columns (seam_full)
AN_H = 272      # analysis height in rows
THUMB_H = 320   # review thumbnail height


def derive(im, W, H):
    """PIL RGB image (the page at any scale) -> dict(rgb8, g, tn)"""
    aw = max(8, round(AN_H * W / H))
    rgb8 = np.asarray(im.resize((aw, AN_H), Image.BOX))
    tw = max(1, round(THUMB_H * W / H))
    tn = np.asarray(im.resize((tw, THUMB_H), Image.BILINEAR))
    g = np.asarray(Image.fromarray(rgb8).convert('L'))
    return dict(rgb8=rgb8, g=g, tn=tn)


def load_small(src):
    """src: file path or the bytes of an image file -> (W, H, dict(rgb8, g, tn)); W, H = true pixel size."""
    im = Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src)
    try:
        W, H = im.size
        if im.format == 'JPEG' and not FULL:
            im.draft('RGB', (max(1, math.ceil(THUMB_H * W / H)), THUMB_H))   # decode reduced, never smaller than THUMB_H rows
        im = im.convert('RGB')
        d = derive(im, W, H)
        if FULL:
            import seam_full
            d['full'] = seam_full.edges(im)
        return W, H, d
    finally:
        im.close()
