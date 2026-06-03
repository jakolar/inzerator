# DMPOK bulk download — performance profile

Measurement log for the per-sheet pipeline + the decisions that fell out of it. Captured 2026-06-03 after the first hour of production downloading suggested room for improvement.

## TL;DR

| Variable | Before | After |
|---|---|---|
| Workers | 3 | **4** |
| Observed rate | 7,4 sheets/min | **10,5 sheets/min** (+42 %) |
| Bottleneck | Network (TCP throughput per stream) | Same |
| ETA full pull | ~5 nights | ~3 nights |

Disk and CPU are not on the critical path — `bulk_dmpok_profile.py` confirmed download owns ≥ 99 % of the per-sheet wall clock.

## How to re-measure

```bash
# 5 sheets, internal SSD target (isolates network from USB).
python3 bulk_dmpok_profile.py --target ssd

# Same 5 sheets, Elements target. Compare to expose disk-bound behaviour.
python3 bulk_dmpok_profile.py --target elements
```

The profiler runs **serially** in its own tempdir — no shared state with the running production downloader, no lock collision.

## Findings (2026-06-03, 5 sheets profiled to internal SSD)

```
=== Profile summary (5 sheets, target=ssd) ===
  avg download : 16.78 s  (6.9 MB/s)
  avg ttfb     : 0.12 s
  avg unzip    : 0.18 s
  avg rename   : 0.000 s
  avg cleanup  : 0.001 s
  avg total    : 16.96 s
  avg ZIP size : 62 MB
  wall clock   : 84.8 s (serial; 17.0 s/sheet)
```

Per-sheet variance was wide: **1.8 MB/s to 16.5 MB/s** across the 5 streams. Single-connection throughput is shaped/jittery, not flat. Faster streams finish quickly; slower ones drag a single worker through a 30 s download.

## Decisions

### Workers 3 → 4 (D)

Adding a 4th worker amortises the slow-stream tail: instead of one worker stuck on a 30 s download while two others are idle, three other workers keep the pipe busy.

Why not 5–6? Per the bulk-email guidance to ČÚZK we promised 4–8 concurrent streams max as a politeness floor. The expert review on 2026-06-03 reaffirmed 3–4 as the "safe and sufficient" range. Disk write at 4 × 7 MB/s = 28 MB/s leaves plenty of USB 3 headroom, but downloads above ~4 streams hit diminishing returns because ČÚZK appears to shape per-IP aggregate as well as per-stream.

Change applied in `bulk_dmpok.py`:

```python
WORKERS = 4
```

CLI also accepts `--workers N` for ad-hoc tuning without editing the file.

### Why NOT increase chunk size or change requests/stream tuning

Profile shows the slow path is the network, not Python's iteration overhead — at 1.8 MB/s a stream is sending ~470 KB/chunk if we use 256 KB chunks. The current 4 MB chunks are already much larger than the bottleneck. Tweaking won't help.

### Why NOT switch to ATOM feed traversal

`openzu.cuzk.gov.cz/opendata/DMPOK-TIFF/epsg-5514/<MAPNOM>.zip` is the canonical static URL ATOM would resolve to anyway. Going through ATOM adds 2 HTTP requests per sheet (32 600 total for the full pull) without changing what we download. The URL pattern is documented and stable. (`download_ortofoto.py` uses ATOM because ortofoto's URL pattern rotates per release cycle — DMPOK doesn't.)

### Why NOT parallelise unzip across cores

Unzip is 0.18 s on a ~62 MB ZIP. Across the full 16 299 sheets that's ~49 minutes total — and it happens per-worker, so already de facto parallel up to the worker count. Multiprocessing would add IPC overhead without saving real wall-clock.

## Operational note: launchd vs Full Disk Access

The launchd plists in `contrib/launchd/` **do not work out of the box** on stock macOS. launchd user agents run in a TCC sandbox that denies `mkdir` / `read` on external volumes by default — the spawned Python child crashes with `PermissionError [Errno 1] Operation not permitted` when it tries to touch any file under `/Volumes/Elements/`.

Two workarounds:

1. **Grant Full Disk Access to `/usr/bin/python3`** via *System Settings → Privacy & Security → Full Disk Access*. After that the plists work as designed: nightly auto-start at 22:00, `pkill -TERM` at 06:00. Requires GUI access.
2. **Run detached from your shell** (this is what the current production process does). The shell session has FDA inherited from Terminal/SSH; the child Python inherits it. Process survives the shell exit via `nohup`:
   ```bash
   nohup /usr/bin/caffeinate -i -d \
     /usr/bin/python3 /Users/jan/projekty/inzerator/bulk_dmpok.py \
     > ~/Library/Logs/inzerator/bulk-dmpok-manual.log 2>&1 < /dev/null &
   disown
   ```
   No night-window throttling — runs 24/7 until manually stopped. To pause:
   ```bash
   pkill -TERM -f bulk_dmpok.py    # clean drain + flush + exit
   ```

Variant 1 is the long-term right answer; variant 2 unblocks today.

## Mistakes worth not repeating

- **Lock dir on `/Volumes/`** — moved to `~/Library/Caches/inzerator/` so it works in both launchd and shell contexts. Hashed `OUT_ROOT` into the filename lets multiple bulk targets coexist.
- **`StandardOutPath` on `/Volumes/`** — launchd silently exits 78 (config error) if it can't open the log path. Moved logs to `~/Library/Logs/inzerator/`.
- **`pkill -f "bulk_dmpok\.py"` with escaped dot** — the escape silently fails to match on macOS. Use the unescaped pattern `bulk_dmpok.py` in the stop plist (already corrected in this commit).
