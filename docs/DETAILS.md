# photobook_tools - detailed notes

The default entry point is `auto.py` (see the README). This file keeps the measurements behind the seam rule and notes on the helper tools.

`ads/` holds reference ad pages (32x32 gray compare); add a new ad type by dropping an example image there.

## Spread rule (read before touching `seam_match`)
Every page is decoded ONCE and reduced while decoding (`seam_load.py`: PIL `draft`, 1/4 scale for a typical 2000-px page; 3-5x faster than
the old full decode). All judgements use the small RGB picture `rgb8` (272 rows high); full resolution is only loaded for the QR check of mostly white pages.
A neighbouring pair A | B is merged only if ALL hold (`seam_stats` / `seam_match` in photobook_lib):
1. `ratio` < 0.5: colour difference across the seam / median difference when B is slid 9-12 rows. One picture cut in two only fits row for row;
   two photos in front of the same wall fit equally badly at every shift (ratio ~ 1);
2. `jump` < 2.5: the seam difference is at most 2.5x the pictures' own column-to-column change next to the seam (rough textures have big jumps everywhere);
3. `std` >= 6: the gray seam columns have structure along the height (a flat wall, however colourful, says nothing about alignment).
One column of the small picture averages a strip of ~8 original pixels, so the test reads wide content while decoding only a reduced JPEG.
Do not add "if the seam is very smooth, accept" for merging: it merges different photos against a plain wall (it happened; `selftest.py` has that case).
Averaging a wide band (8 or 16 columns) into one profile was tried and is WORSE (accepts only 52-68 % of true spreads): the band's content changes with distance from the seam.

Measured on the library's own pages (2856 merged spreads, 6679 neighbouring pages left separate): merged spreads re-judged 97.5 % merged, 1.8 % UNCERTAIN, 0.7 % missed;
separate neighbours 0.16 % merged, 0.63 % UNCERTAIN. Spot check on two books (all spreads split back, pipeline re-run): 29/30 and 34/34 recovered;
the miss was an old false merge (white page with three small photos next to a portrait), the one extra merge was a real spread the old rule had missed.
Time per 148-page book 3 s (old rule 15 s).

Pairs that fail only the strict test (`ratio` < 0.8 and `jump` < 4) are **UNCERTAIN**: they are not merged, a contact sheet is written
(`%TEMP%\photobook_review\<book>.jpg`, `UNCERTAIN n pair(s)` in the log). Project rule: when really unsure, spend the effort and
LOOK at the picture. Merge by hand only if the two halves are one picture.

Order `half | FULL | half` (full-width scan between its halves) is handled like `half | half | FULL`: one version is kept, the higher-resolution one.

## verify_spreads.py - re-check existing merged pages
`python verify_spreads.py <pdfs or folders> [--recursive] [--fix]` re-judges every double-width page this toolkit made and, with `--fix`,
splits the ones that clearly fail (`ratio` < 0.8, `jump` < 6 + smooth fallback, i.e. only clear failures) back into two pages (original -> Recycle Bin).
Run it after any change of the merge rule; pipeline B never re-judges pages that are already merged (they are copied through).

## merge_pages.py - merge by decision
`python merge_pages.py book.pdf 98-99 120-121` merges those neighbouring one-image pages (left-right order, numbers as in the UNCERTAIN sheet)
into double-width pages in place (original -> Recycle Bin). Use it after LOOKING at a contact sheet and seeing one picture.
Right-to-left books: the sheet says `p31 | p30` -> pass `31-30` (the first number is the left page).
Note: `verify_spreads` flags smooth, soft-lit true spreads too (it is deliberately strict); look at the flagged pages before using `--fix`.

## F. Overlapping half pages (`overlap_stitch.py`)
Some scans cut one picture into two pages that SHARE a narrow band (one scanned book: ~3-5 % of the page width, pages 4-5, 6-7, ...). The one-column seam rule cannot see that.
`overlap_stitch.py book.pdf [--dry] [--pages a-b] [--force n] [--out file]`: a strip of the left page's right edge is searched in the right page's left part (OpenCV template matching), then the whole shared band is compared in colour against vertically shifted controls
(`diff <= 0.4 * ctrl`, `score >= 0.85`, `diff <= 15`). Both orders are tried (right-to-left books). The merge is lossless: both JPEGs stay, the page is the union width, each image is clipped at the middle of the overlap.
Per-book tool, NOT part of the default pipelines (the dry-run on the other 9 inline/form books found no such pairs). Plain sea/sky/wall pairs cannot be judged by it: look at them.

## Stages (roadmap)
The merge logic is built in stages, each one validated and switched on on its own:
1. **Read** (done): every page is decoded once at reduced JPEG scale (`seam_load.py`).
2. **Seam merge** (done): `seam_match`; pairs that almost pass go to a contact sheet for a human look (UNCERTAIN).
3. **Flat-background screening** (not done): about 20 % of neighbouring pairs have a flat seam (gray column std < 6). They are never merged and never reviewed today.
   True spreads with a flat seam have no labels in the library (the old rule never merged them); positives can be made by cutting single wide scans in the middle
   (extracted for tuning, not analysed yet). Candidate signals: tiny absolute colour difference, colour / brightness continuity over a ~5 % strip. Output: merge, UNCERTAIN sheet, or separate.
4. **Overlap removal** (`overlap_stitch.py`, per book, not part of the default pipelines): on the 272-row analysis pictures it finds only 13-16 of the 29 overlapping pairs of that book
   (all 29 at 0.5 scale), so it needs a half-scale confirmation and a cheap pre-screen before it can join the pipeline.
5. **AI gap repair** (idea only): a gap between two halves (old books) could be filled by inpainting. It invents pixels (faces!), so: opt-in per book, classical inpainting for narrow gaps first,
   and any model download only after asking.
Pairs the stages cannot decide go to a human look at every stage.

## auto.py (new entry point)
`python auto.py <folder-or-pdf> ... [--out DIR] [--seam fast|full] [--no-trim]` starts from a folder of images or complete PDFs (not zips):
drop blank / ad / duplicate pages, trim white borders, bring pages of the main aspect to the largest size of the group, merge seams
(`--seam fast` = reduced-scale pictures, `--seam full` = original 2-px rule on full-resolution pictures; CUDA version planned, not written),
queue the plain-colour pairs, and hand the undecided pairs to `ai_hook.review()` (default: nothing). Never put API keys or account data in this repository.
