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

## F. Overlapping half pages in an existing PDF (`overlap_stitch.py`, uses `overlap.py`)
Some scans cut one picture into two pages that SHARE a narrow band (one scanned book: ~3-5 % of the page width, pages 4-5, 6-7, ...). The one-column seam rule cannot see that.
`overlap_stitch.py book.pdf [--dry] [--pages a-b] [--force n] [--out file]`: a strip of the left page's right edge is searched in the right page's left part (OpenCV template matching), then the whole shared band is compared in colour against vertically shifted controls
(`diff <= 0.4 * ctrl`, `score >= 0.85`, `diff <= 15`). Both orders are tried (right-to-left books). The merge is lossless: both JPEGs stay, the page is the union width, each image is clipped at the middle of the overlap.
For an already finished PDF; `auto.py` does the same on page images by default (the dry-run on 9 other books found no such pairs). Plain sea/sky/wall pairs cannot be judged by it: look at them.

## Stages (roadmap)
The merge logic is built in stages, each one validated and switched on on its own:
1. **Read** (done): every page is decoded once at reduced JPEG scale (`seam_load.py`).
2. **Seam merge** (done): `seam_match`; pairs that almost pass go to a contact sheet for a human look (UNCERTAIN).
3. **Flat-background screening** (done as a queue filter, nothing is merged): 34 % of neighbouring pairs have a flat seam (gray column std < 6), 99 % of those are constant light colours.
   Data: 65 flat-seam wide pages (the 2716 single wide scans of the library cut in the middle), looked at one by one: 33 are one picture, 32 are two different pages scanned side by side
   (text pages, blank + photo, two photos) which serve as extra negatives; 2011 neighbouring pairs of the library as negatives (they contain a few unlabelled spreads, so false-positive counts are upper bounds).
   One seam column carries almost no information: seam difference, offset, slopes and extrapolation do not separate (AUC 0.5-0.6). What helps: brightness (true flat spreads are mostly dark pictures, 8 of 12; the pairs are mostly white pages)
   and, where the faint row profile has structure (pstd >= 0.5), its correlation across the seam (6/6 true >= 0.74; library 16 of 51 >= 0.7; but text pages also correlate: 9/9 layout >= 0.8).
   `flat_keep`: constant light seam dropped; queue 688 -> 55 of 2011, 11/12 true kept. No tier is precise enough to merge automatically (best: pcorr >= 0.85, 4 true vs 7 layout vs 9 library).
   The positives come from scans of open books (centre gutter line), so recall is only indicative.
4. **Overlap removal** (done, `overlap.py`, default on): the pages of a picture share a band of only 3-5 % of the page width. Prescreen on 450-column pictures
   (template score >= 0.7, 8 template columns: all 29 true pairs of the test book pass, 33 % of ordinary neighbours do), strict test on 900-column pictures
   (`decide`: score >= 0.85, band difference <= 0.4 x the vertically shifted control). 29/29 found, dx error median 0 / max 2 px, 0/54 false. At the 272-row analysis
   scale only 16/29 are found because the band is 5-9 columns there, hence the separate pictures. About 0.15 s per neighbouring pair.
5. **Gap repair** (interface done, `gap_repair.py`, `ai_hook.py`): which pair has a missing band, and how wide, comes from a human (`--gaps`) or from `ai_hook.gaps`.
   A task = RGBA canvas (left | transparent band | right) + mask + json; filled by `<task>.filled.png`, `ai_hook.fill_gap(job)` or the OpenCV `classic_fill` (narrow gaps, tests only);
   `compose` takes only the gap (feathered edge) from the filled picture so the scanned pixels stay untouched. Invented content (faces!): opt-in per pair, any model download only after asking.
Pairs the stages cannot decide go to a human look at every stage.

## auto.py (entry point)
`python auto.py <folder-or-pdf> ... [--out DIR] [--seam fast|full] [--no-trim] [--no-overlap] [--gaps FILE] [--force]` starts from a folder of images or complete PDFs (not zips):
drop blank / ad / duplicate pages, trim white borders, bring pages of the main aspect to the largest size of the group, merge seams
(`--seam fast` = reduced-scale pictures, `--seam full` = original 2-px rule on full-resolution pictures; CUDA version planned, not written),
join overlapping pages, queue the plain-colour pairs, hand the undecided pairs to `ai_hook.review()`, repair the gaps named by `--gaps` / `ai_hook.gaps()` (default: nothing).
Never put API keys or account data in this repository.
