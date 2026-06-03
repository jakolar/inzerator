# DMPOK bulk download

Operating manual for pulling the entire ČÚZK DMPOK-TIFF dataset (16 299 SM5 sheets, ~1.14 TB extracted) onto `/Volumes/Elements/`. With 4 workers the full pull lands in ~3–4 nights of 24/7 running, or ~5–6 nights if you respect the 22:00–06:00 launchd window.

## Daily runbook — copy/paste

The launchd plists in `contrib/launchd/` need Full Disk Access granted to `/usr/bin/python3` first; without it they crash with `PermissionError` on the external volume (see *Operational note: launchd vs FDA* below). Until that GUI grant happens, drive the downloader from a shell:

### Start (or resume after a pause / Mac reboot)

```bash
nohup /usr/bin/caffeinate -i -d \
  /usr/bin/python3 /Users/jan/projekty/inzerator/bulk_dmpok.py \
  > ~/Library/Logs/inzerator/bulk-dmpok-manual.log 2>&1 < /dev/null &
disown
```

Picks up wherever `sheets.json` left off — no extra flags. `caffeinate -i -d` blocks sleep + display sleep; `nohup` + `disown` detaches from your SSH session so it survives logout.

### Pause cleanly

```bash
pkill -TERM -f bulk_dmpok.py
```

In-flight downloads abort their current chunk, sheet status flips back to `pending`, `sheets.json` flushes, lock dir releases, process exits in ≤ 2 s.

### Check progress

```bash
BULK_OUT_DIR=/Volumes/Elements/cuzk-bulk python3 bulk_dmpok_status.py
```

Add `-v` for top failure reason groupings; add `--retry-failed` to flip every `failed` entry back to `pending`.

### Inspect live process

```bash
pgrep -fl bulk_dmpok.py                        # PID + cmdline
ps -p $(pgrep -f bulk_dmpok.py) -o etime,rss,stat
lsof -p $(pgrep -f bulk_dmpok.py) | grep openzu   # current TCP streams
tail -f /Volumes/Elements/cuzk-bulk/download.log  # per-sheet events
```

### One-time setup (already done on this Mac)

```bash
# 1. inventory the entire SM5 sheet grid (~16 300 codes, ~1 min)
BULK_OUT_DIR=/Volumes/Elements/cuzk-bulk python3 bulk_dmpok_inventory.py

# 2. (optional, requires FDA grant first) install launchd auto-schedule
cp contrib/launchd/com.inzerator.bulk-dmpok.{start,stop}.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.inzerator.bulk-dmpok.start.plist
launchctl load -w ~/Library/LaunchAgents/com.inzerator.bulk-dmpok.stop.plist
```

After the pull finishes, `gen_heightfield.py` uses the data via `INZERATOR_CACHE=/Volumes/Elements/cuzk-bulk python3 gen_heightfield.py --slug <slug>` — the disk layout matches the existing cache convention.

## What gets downloaded

Just **DMPOK** (Digitální Model Povrchu, "Operativně Krátkodobý" — průběžně aktualizovaný DSM s vegetací a budovami). One TIFF + one TFW worldfile per SM5 sheet.

Distribution endpoint: `https://openzu.cuzk.gov.cz/opendata/DMPOK-TIFF/epsg-5514/<MAPNOM>.zip`.

Not downloaded by this tooling: `DMR5G` (bare-earth) — current pipeline fetches it on-demand from ArcGIS `exportImage`. Not downloaded: ortofoto. Add separately if needed.

## Disk layout

```
/Volumes/Elements/cuzk-bulk/        # data — set via BULK_OUT_DIR
  sheets.json                         # canonical state — single source of truth
  download.log                        # append-only, per-sheet outcomes + retries
  dmpok_tiff_<MAPNOM>/                # matches existing INZERATOR_CACHE convention
    <MAPNOM>.tif                      # so `INZERATOR_CACHE=...` plugs in directly
    <MAPNOM>.tfw

~/Library/Caches/inzerator/         # lock dir lives on internal disk so
  bulk_dmpok-<hash>.lock/             # launchd (no FDA) can still mkdir it;
                                      # <hash> = SHA1[:8] of BULK_OUT_DIR

~/Library/Logs/inzerator/           # all log output (launchd + manual)
  bulk-dmpok-manual.log
  bulk-dmpok-start.{out,err}.log
  bulk-dmpok-stop.{out,err}.log
```

Override the data root via `BULK_OUT_DIR` env (default `/Volumes/Elements/cuzk-bulk`). The lock dir's `<hash>` suffix lets multiple `BULK_OUT_DIR`s coexist without colliding.

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

- **4** worker threads (`ThreadPoolExecutor`) by default, 0.5–1.5 s jitter between sheets per worker; override with `--workers N`
- 5 attempts per sheet with `min(60, 2^attempt) + jitter` backoff
- Per-sheet flow: `requests.get` stream → `zf.extractall` → normalise filename → delete ZIP → `sheets.json` atomic write
- `SIGTERM` / `SIGINT` sets a stop event; in-flight downloads either finish their current ZIP or abort and re-mark `pending`; state flushes; lock dir released; clean exit
- Lock dir `~/Library/Caches/inzerator/bulk_dmpok-<hash>.lock/` prevents two instances clobbering `sheets.json`
- User-Agent `inzerator/1.0; alacremex@gmail.com` (matches the email to ČÚZK)

No internal time-window check — that's launchd's job (when launchd has Full Disk Access).

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

## Operational note: launchd vs Full Disk Access

launchd user agents on macOS run inside TCC and **do not have read/write access to external volumes** by default. The plists in `contrib/launchd/` will install + load cleanly, but the spawned Python child crashes with `PermissionError [Errno 1] Operation not permitted` the moment it touches anything under `/Volumes/Elements/`. Symptom: `launchctl list | grep bulk-dmpok` shows a non-zero last exit code; `~/Library/Logs/inzerator/bulk-dmpok-start.err.log` has a `PermissionError` traceback.

Two ways out:

1. **Grant Full Disk Access to `/usr/bin/python3`** (long-term fix): *System Settings → Privacy & Security → Full Disk Access → +* → pick `/usr/bin/python3`. Requires physical or remote-GUI access. After the grant, the launchd schedule below works automatically.
2. **Run detached from a shell** that already has FDA (Terminal/SSH inherit it on most setups). See the *Daily runbook* section at the top — that's the nohup + caffeinate one-liner. Runs 24/7 rather than night-only, but you pause/resume by hand.

## launchd plists

Two plists in `contrib/launchd/` (functional once FDA is granted):

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
rm -rf ~/Library/Caches/inzerator/bulk_dmpok-*.lock   # release the lock dir
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
| `Another bulk_dmpok already running` | Lock dir exists. If you're sure no `bulk_dmpok.py` PID is alive: `rm -rf ~/Library/Caches/inzerator/bulk_dmpok-*.lock`. |
| `sheets.json not found` | Run `bulk_dmpok_inventory.py` first. |
| Many `failed` sheets with `ConnectionError` | ČÚZK is having a bad day. Wait a night, then `bulk_dmpok_status.py --retry-failed`. |
| Many `missing` (404) | Expected for a fraction of sheets. ČÚZK doesn't publish DMPOK for every SM5. |
| launchd `launchctl list` shows non-zero last exit | Check `~/Library/Logs/inzerator/bulk-dmpok-start.err.log`. If first install failed silently with exit 78, the plist's `StandardOutPath`/`StandardErrorPath` was pointing at `/Volumes/Elements/`; launchd user agents lack TCC permission to write there. Plists in this repo already log under `~/Library/Logs/`. |
| launchd plist doesn't fire | Check `launchctl list | grep bulk-dmpok`. If absent, re-run `launchctl load -w …`. |
| Disk full mid-run | SIGTERM the process, free space, restart. State is consistent — only `done` sheets are counted. |
| Macbook sleeps at night | Either disable sleep during the campaign (`caffeinate -i` in tmux, or System Settings → Energy), or use Power Adapter schedule. launchd won't fire during sleep. |
