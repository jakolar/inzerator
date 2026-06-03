# DMPOK bulk download

Operating manual for pulling the entire ČÚZK DMPOK-TIFF dataset (~12 600 SM5 sheets, ~880 GB ZIP / ~1.0 TB extracted) onto a local disk over ~12 nights.

## TL;DR

```bash
# 1. one-time inventory crawl (~1 min, ~12 600 sheets)
BULK_OUT_DIR=/Volumes/Elements/cuzk-bulk python3 bulk_dmpok_inventory.py

# 2. install launchd plists (auto-start 22:00, auto-stop 06:00)
cp contrib/launchd/com.inzerator.bulk-dmpok.{start,stop}.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.inzerator.bulk-dmpok.start.plist
launchctl load -w ~/Library/LaunchAgents/com.inzerator.bulk-dmpok.stop.plist

# 3. wait. Check progress whenever:
BULK_OUT_DIR=/Volumes/Elements/cuzk-bulk python3 bulk_dmpok_status.py
```

After ~12 nights the disk holds the full DMPOK coverage and `gen_heightfield.py` can use it by pointing `INZERATOR_CACHE` at the bulk output dir.

## What gets downloaded

Just **DMPOK** (Digitální Model Povrchu, "Operativně Krátkodobý" — průběžně aktualizovaný DSM s vegetací a budovami). One TIFF + one TFW worldfile per SM5 sheet.

Distribution endpoint: `https://openzu.cuzk.gov.cz/opendata/DMPOK-TIFF/epsg-5514/<MAPNOM>.zip`.

Not downloaded by this tooling: `DMR5G` (bare-earth) — current pipeline fetches it on-demand from ArcGIS `exportImage`. Not downloaded: ortofoto. Add separately if needed.

## Disk layout

```
/Volumes/Elements/cuzk-bulk/
  sheets.json                 # canonical state — single source of truth
  download.log                # append-only, per-sheet outcomes + retries
  launchd.{out,err,stop.out,stop.err}.log
  .lock/                      # active-process lock dir (auto-released on exit)
  dmpok_tiff_<MAPNOM>/        # matches existing INZERATOR_CACHE convention
    <MAPNOM>.tif              # so `INZERATOR_CACHE=...` plugs in directly
    <MAPNOM>.tfw
```

Override the root via `BULK_OUT_DIR` env (default `/Volumes/Elements/cuzk-bulk`).

## Sheet state machine

```
pending → downloading → done       (success — TIFF on disk)
                     → missing     (404 — ČÚZK has no DMPOK for this sheet)
                     → failed      (5 attempts exhausted, last error in JSON)
```

`failed` sheets are **not auto-retried** across runs — that prevents a flaky one from burning the worker pool. Operator nudges them back manually via `bulk_dmpok_status.py --retry-failed`.

If the script dies uncleanly (kernel panic, power loss), any `downloading` entries are recovered to `pending` on next start.

## The three scripts

### `bulk_dmpok_inventory.py` — one-shot

Queries `KladyMapovychListu/25` (the SM5 mapový list layer) page-by-page (`resultOffset` / `MAPNOM ASC` for deterministic pagination) and writes every `MAPNOM` into `sheets.json` as `pending`. Idempotent: re-running preserves `done`/`failed`/`missing` entries, only refreshes the pool of pending ones.

Run once at start, optionally re-run if ČÚZK adds new sheets later.

### `bulk_dmpok.py` — main downloader

- 3 worker threads (`ThreadPoolExecutor`), 0.5–1.5 s jitter between sheets per worker
- 5 attempts per sheet with `min(60, 2^attempt) + jitter` backoff
- Per-sheet flow: `requests.get` stream → `zf.extractall` → normalise filename → delete ZIP → `sheets.json` atomic write
- `SIGTERM` / `SIGINT` sets a stop event; in-flight downloads either finish their current ZIP or abort and re-mark `pending`; state flushes; lock dir released; clean exit
- Lock dir `<BULK_OUT_DIR>/.lock/` prevents two instances clobbering `sheets.json`
- User-Agent `inzerator/1.0; alacremex@gmail.com` (matches the email to ČÚZK)

No internal time-window check — that's launchd's job.

### `bulk_dmpok_status.py` — read-only summary

```
DMPOK bulk download — /Volumes/Elements/cuzk-bulk
  done:     8234 / 12600 (65.3 %)
  pending:  4360
  failed:      4
  missing:     2   (404 — not published by ČÚZK)
  in-flight (stale):     0
  on disk: 612.4 GB
  rate:    42.1 sheets/min (last 87 min) → ETA 1.7 h (0.2 nights at 8 h/night)
```

Flags:
- `-v` / `--verbose` — top failure reason groupings
- `--retry-failed` — flip every `failed` → `pending`

## launchd plists

Two plists in `contrib/launchd/`:

- `com.inzerator.bulk-dmpok.start.plist` — `StartCalendarInterval` Hour=22 → launches `python3 bulk_dmpok.py` with `BULK_OUT_DIR=/Volumes/Elements/cuzk-bulk`
- `com.inzerator.bulk-dmpok.stop.plist` — `StartCalendarInterval` Hour=6 → runs `pkill -TERM -f bulk_dmpok.py`

### Install

```bash
cp contrib/launchd/com.inzerator.bulk-dmpok.{start,stop}.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.inzerator.bulk-dmpok.start.plist
launchctl load -w ~/Library/LaunchAgents/com.inzerator.bulk-dmpok.stop.plist
```

Verify they're scheduled:

```bash
launchctl list | grep bulk-dmpok
```

### Uninstall / pause for a while

```bash
launchctl unload ~/Library/LaunchAgents/com.inzerator.bulk-dmpok.start.plist
launchctl unload ~/Library/LaunchAgents/com.inzerator.bulk-dmpok.stop.plist
# files can stay; just don't reload them
```

### Manual one-off run (outside the night window)

```bash
BULK_OUT_DIR=/Volumes/Elements/cuzk-bulk python3 bulk_dmpok.py
# Ctrl-C to pause cleanly — same SIGINT handler as launchd's pkill
```

### Force-kill (rarely needed)

If the SIGTERM handler hangs (network stack jammed):

```bash
pkill -KILL -f bulk_dmpok.py
rm -rf /Volumes/Elements/cuzk-bulk/.lock   # release the lock dir
```

## Capacity / time budget

- **Aggregate throughput**: ~882 GB / 90 h = 2.7 MB/s → 0.9 MB/s per worker (well within ČÚZK's tolerance per the bulk-email)
- **Per sheet**: ~70 MB ZIP → ~26 s at 2.7 MB/s aggregate
- **Per night** (22:00–06:00 = 8 h): ~1 070 sheets, ~75 GB
- **Total**: ~12 nights for a full pull
- **Disk**: ~1.0 TB extracted on `/Volumes/Elements` (9.1 TiB free → ~11 % used)

## Wiring back into the viewer pipeline

Once the disk holds DMPOK for the area of interest:

```bash
INZERATOR_CACHE=/Volumes/Elements/cuzk-bulk python3 gen_heightfield.py --slug <slug>
```

`gen_heightfield.py` already understands the `dmpok_tiff_<CODE>/` directory layout via `discover_sm5` (see `gen_heightfield.py`), so no code change — just an env var.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Another bulk_dmpok already running` | Lock dir exists. If you're sure no `bulk_dmpok.py` PID is alive: `rm -rf /Volumes/Elements/cuzk-bulk/.lock`. |
| `sheets.json not found` | Run `bulk_dmpok_inventory.py` first. |
| Many `failed` sheets with `ConnectionError` | ČÚZK is having a bad day. Wait a night, then `bulk_dmpok_status.py --retry-failed`. |
| Many `missing` (404) | Expected for a fraction of sheets. ČÚZK doesn't publish DMPOK for every SM5. |
| launchd `launchctl list` shows non-zero last exit | Check `~/Library/Logs/inzerator/bulk-dmpok-start.err.log`. If first install failed silently with exit 78, the plist's `StandardOutPath`/`StandardErrorPath` was pointing at `/Volumes/Elements/`; launchd user agents lack TCC permission to write there. Plists in this repo already log under `~/Library/Logs/`. |
| launchd plist doesn't fire | Check `launchctl list | grep bulk-dmpok`. If absent, re-run `launchctl load -w …`. |
| Disk full mid-run | SIGTERM the process, free space, restart. State is consistent — only `done` sheets are counted. |
| Macbook sleeps at night | Either disable sleep during the campaign (`caffeinate -i` in tmux, or System Settings → Energy), or use Power Adapter schedule. launchd won't fire during sleep. |
