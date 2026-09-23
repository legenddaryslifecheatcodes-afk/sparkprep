# SparkPrep performance baseline — 2026-09-20

Measured with `backend/tools/profile_stages.py` (whole loop), `profile_cmyk.py` (inside the CMYK step) and
`profile_cmyk_scale.py` (size scaling + peak memory). Re-run them to compare like with like.

**Machine:** the owner's Windows PC, 4 cores, other programs running (CPU 39–57% during the clean run). This is
NOT Render. Earlier production measurements (plain 10.6 MP purple cover): upload+scan 1.9 s, auto-fix 7.6 s.
Use these numbers for *where time goes* and *relative* cost, not as absolute production promises.

## Cover — 3810×2775 RGB (10.6 MP) — full loop, median [min–max] seconds, 3 runs

| Stage | Seconds |
|---|---|
| Upload handling (parse/store, loopback) | 0.04 [0.02–0.05] |
| Initial scan on upload | 2.32 [2.31–2.67] |
| Issue detection (= Agent 1's scan) | 2.33 [2.30–2.44] — cover text-margin check (OCR) 1.73, ink-coverage check 0.59 |
| **Verified pipeline, total** | **28.3 [25.2–30.8]** |
| ↳ Agent 1 triage | 2.32 |
| ↳ Agent 2 repair (existing engine) | 23.4 [20.2–26.1] |
| &nbsp;&nbsp;&nbsp;↳ **CMYK conversion + ink clamp** | **17.0 [13.9–19.7]** |
| &nbsp;&nbsp;&nbsp;↳ safe-margin repair | 0.82 |
| &nbsp;&nbsp;&nbsp;↳ engine's own re-check + margin OCR | 5.5 |
| ↳ Agent 3 verification | 2.41 |
| ↳ Agent 4 audit / sign-off | 0.01 |
| Confirm rescan (Enter) | 2.30 |
| Final output (export PDF/X-1a) | 2.79 |

Inside CMYK conversion (quiet run, 15.2 s): RGB→CMYK math 8.3 s, ink clamp 7.7 s, TIFF save 0.5 s,
decode/array/PIL glue ~0.4 s. Two full-size float64 numpy passes are essentially all of it.

## Interior — 300-page PDF — full loop

| Stage | Seconds |
|---|---|
| Upload handling | 0.01 |
| Initial scan on upload (basic = page 1) | 0.35 |
| Issue detection (Agent 1) | 0.39 |
| **Verified pipeline, total** | **8.58 [8.35–10.41]** — repair 7.91 (margin/page-size repair 2.83 + Ghostscript PDF/X-1a 4.34), verification 0.40, audit 0.00 |
| Paid Advanced Check, file already clean (300 pp) | 2.42 |
| Paid Advanced Check, **bad file** (scan + repair + rescan) | **6.60 [6.55–8.15]** — repair 2.80, scans 3.59 |
| Final output (export) | 0.28 |

The "300-page check in 8.8 s" figure the owner quoted was one run of the paid check on a bad file; the repeatable
figure is ~6.6 s on a quiet machine (one earlier run took 23 s while the PC was busy). Ghostscript step is only
exercised when the PDF has live transparency/layers — not yet measured with such a file.

## What the "36–78 s" numbers really were
The cover verified pipeline while this PC was at ~99% CPU from other programs (that run: pipeline 67–137 s, CMYK
conversion 52–115 s). They were never the 300-page workload. Same code, quiet machine: 25–31 s. **Load changes
the cover pipeline by ~4×; the interior pipeline barely moves.**

## Scaling and MEMORY (the real constraint)
| Cover size | convert_to_cmyk | peak memory |
|---|---|---|
| 10.6 MP (standard 6×9 wrap) | 10–17 s (≈1 s/MP) | **≈1,470 MB** |
| 27 MP (large/hardcover wrap) | 72–97 s (≈2.7–3.6 s/MP, worse than linear) | ≥1,630 MB (this PC hit its RAM limit and paged) |

Render's instance is 2 GB. A single 10.6 MP conversion already needs ~1.5 GB; larger files will exceed it and be
OOM-killed (taking the API down for everyone), regardless of any time limit. Root cause: `clamp_total_ink_coverage`
and `rgb_array_to_cmyk_array` (file_processor.py) build several full-image float64 arrays. Moving work to a
background queue does NOT fix this — memory, not duration, is the limit.

## Routing proposal (data-based, NOT implemented)
* Interior ≤ 300 pages: always live — everything ≤ ~10 s, insensitive to load.
* Cover: route on pixel count (known at upload). Live for ≲ 12 MP (standard paperback wraps, ~25–30 s quiet);
  heavy tier above that — but only after the conversion is made memory-safe (row-band processing), otherwise the
  heavy tier just OOMs in the background.
* Re-measure with a transparency-heavy interior (Ghostscript path) before finalising the interior rule.

## Instrumentation that stays in production (costs nothing)
`/autofix/verified` now returns `pipeline.timings` (triage, triage_scan, repair, verify, verify_integrity,
verify_scan, audit, total) and logs one `verified_autofix … timings=…` line per run, so real production
numbers accumulate for the routing decision.

---
# UPDATE 2026-09-20 (later): memory-safe CMYK conversion — Render-like results

Change: `convert_to_cmyk` now processes the image in horizontal bands (~1M pixels each) instead of several
full-image float64 copies; `check_total_ink_coverage` measures already-CMYK files exactly (banded, integer math)
instead of shrinking them (shrinking added rounding noise that made correct files look "still over 240%").
Proven pixel-identical to the old whole-image math (test: 10 image types x 2 limits, many forced bands).

Harness: `tools/render_like_test.py` runs the real API in its own process inside a Windows Job Object with a
**2 GB memory cap and 1 CPU core** (Render 1c-2g), counting memory of the server PLUS Tesseract/Ghostscript
helpers, through the production endpoints: slot-upload -> /autofix/verified -> /confirm -> /export -> download.

| Scenario (2 GB cap, 1 core) | Peak memory (% of cap) | Wall time | Result |
|---|---|---|---|
| 1 x 10.6 MP | 480 MB (23%) | 76 s | confirmed, CMYK, ink 240.0%, export DeviceCMYK |
| 1 x 27 MP | 672 MB (33%) | 186 s | confirmed |
| 2 x 10.6 MP at once | 747 MB (36%) | 87 s | both confirmed |
| 2 x 27 MP at once | 1,110 MB (54%) | 430 s | both confirmed, ink 240.0%, DeviceCMYK |
| OLD memory mode, 2 x 10.6 MP | 2,056 MB (100%) | 78 s | both repairs FAILED (memory), files rolled back untouched |
| OLD memory mode, 1 x 27 MP | 2,182 MB (107%) | 139 s | repair FAILED (memory), file rolled back untouched |

Idle server ~216 MB. Caveat: a real Render container OOM-KILLS the process at the cap (the emulation raises
MemoryError instead), so the old mode would have taken the API down, not just failed the job.
Side effect worth knowing: with 1 core, another customer's plain request (health check) waited up to ~9-20 s
while heavy cover jobs ran (median 0.02 s) -- CPU contention, not memory.

---
# UPDATE 2026-09-20 (evening): heavy steps moved off the server's main thread

Cause of other customers stalling during heavy jobs: autofix() ran ~a dozen heavy steps (cover text/OCR check,
interior margin repair, PDF audits, file analysis) directly on the event loop, freezing the whole API while they ran.
They now run through run_with_timeout (worker thread) — identical results, same code.

Worst wait for another customer's plain request while heavy jobs run (2 GB / 1 core harness):
| Scenario | Before | After |
|---|---|---|
| 1 x 10.6 MP | 2.44 s | 0.44 s |
| 1 x 27 MP | 17.38 s | 2.10 s |
| 2 x 10.6 MP | 9.22 s | 1.19 s |
Peak memory unchanged (482 / 672 / 747 MB); outputs still correct (ink 240.0%, DeviceCMYK); wall time 44 s / 136 s / 74 s.
Production check after deploying 04f5590: /api/health -> ocr.available=true (Tesseract 5.5.0).

---

# 2026-09-23 — customer-size files (~11 MB and ~24.5 MB covers, ~10 MB and ~25 MB 300-page interiors)

Measured with `tools/profile_steps.py` (per step, printed live) on photo-like files (smooth colour + grain), owner's
PC under heavy background load. Three fixes, each checked for correctness before shipping:

| Cover step (11 MB photo cover) | Before | After |
|---|---|---|
| Cover text check (OCR), per read | 195 s | 5.7–8 s first read, 0.0 s repeats |
| OCR reads per customer flow | 6–7 | 2 (original file, repaired file) — cached per file (path+size+mtime) |
| CMYK conversion + ink clamp | 13.5–18 s | 10 s (24.5 MB cover: 29–36 s -> 18 s) |
| Export cover PDF/X-1a | 18–26 s | 3.8 s (24.5 MB cover: 43 s -> 7.6 s) |
| Same-book fingerprint | 6.2 s | 0 s (reuses the cached OCR) |

Causes: (1) OCR re-run on the identical file 6-7x per flow, and image covers OCR'd at full resolution (photo grain
makes tesseract's sparse-text mode crawl) -- now once per file, at 200 DPI like PDF covers. (2) ReportLab ASCII85-
encoding every image in pure Python (its C accelerator isn't installed here or on Render) -- now binary streams
(`rl_config.useA85 = 0`), identical image data, 25% smaller PDFs. (3) CMYK math in float64 -- RGB->CMYK now exact
integer maths (differs from the old float64 only on 0.84% of colours sitting on a .5 tie, by 1/255, where float64
rounded wrongly), ink clamp in float32 (0 differences over all 16.7M RGB colours at 240%/270%).

Interiors (10–25 MB, 300 pp): every step under ~10 s (load-dependent); nothing needed.
