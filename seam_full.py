"""Full-resolution seam test: the ORIGINAL rule (2-px edge strips of the full picture), kept next to the fast reduced-scale
`seam_match` of photobook_lib. Slower (every page is decoded at full size) but needs no reduction.
Selected with `seam_load.FULL = True` (auto.py --seam full). A CUDA version is planned, not written."""
import numpy as np


def edges(im):
    """full-size PIL image -> the 2 outermost columns on each side, gray and RGB (all the rule needs)"""
    rgb = np.asarray(im.convert('RGB'))
    g = np.asarray(im.convert('L'))
    return dict(gL=g[:, :2].copy(), gR=g[:, -2:].copy(), eL=rgb[:, :2].copy(), eR=rgb[:, -2:].copy())


def _aligned(A, B, thr=0.6, smooth_ok=False):
    """colour, row-for-row alignment: seam difference at the true alignment < thr x the difference with B slid 30-40 rows"""
    a = A['eR'].astype(np.float32).mean(axis=1)
    b = B['eL'].astype(np.float32).mean(axis=1)
    h = len(a)
    if h < 200:
        return True

    def d(s):
        x, y = (a[s:], b[:h - s]) if s >= 0 else (a[:h + s], b[-s:])
        return float(np.abs(x - y).mean())
    far = float(np.median([d(-40), d(-30), d(30), d(40)]))
    if d(0) < thr * far:
        return True
    if not smooth_ok:
        return False
    ctrl = float(np.abs(a - np.roll(b, h // 3, axis=0)).mean())
    return d(0) < 5.6 and d(0) < 0.25 * ctrl


def edge_match(A, B, align_thr=0.6, smooth_ok=False):
    """True if A's right edge continues into B's left edge. Candidate: 2-px gray strips close (< 12, and < 0.3 x control shifted by h/3)
    or seam jump <= 1.5 x the picture's own column change; then it must also pass _aligned()."""
    if A['w'] != B['w'] or A['h'] != B['h'] or A['h'] <= A['w']:
        return False, 1e9
    gr, gl = A['gR'].astype(np.float32), B['gL'].astype(np.float32)
    ar, bl = gr.mean(axis=1), gl.mean(axis=1)
    if ar.std() < 6 or bl.std() < 6:
        return False, 1e9
    d = np.abs(ar - bl).mean()
    shift = max(len(ar) // 3, 7)
    ok = d < 12 and d < 0.3 * np.abs(ar - np.roll(bl, shift)).mean()
    if not ok:
        seam = np.abs(gr[:, -1] - gl[:, 0]).mean()
        internal = (np.abs(gr[:, -1] - gr[:, -2]).mean() + np.abs(gl[:, 0] - gl[:, 1]).mean()) / 2 + 1.0
        ctrl1 = np.abs(gr[:, -1] - np.roll(gl[:, 0], shift)).mean()
        if seam < 1.5 * internal and seam < 0.3 * ctrl1:
            ok, d = True, min(d, seam)
    if ok and not _aligned(A, B, align_thr, smooth_ok):
        return False, 1e9
    return ok, float(d)
