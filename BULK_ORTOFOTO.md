# Ortofoto bulk download

Operating manual for pulling the entire ČÚZK ortofoto dataset (16 301 SM5
sheets, newest acquisition per sheet, ~1.1 TB of JPEG @ 0.1239 m/px) onto
`/Volumes/Elements/`. Clone of the DMPOK bulk system — same engine, same
operational model. Design rationale in
`docs/superpowers/specs/2026-06-05-bulk-ortofoto-download-design.md`.

With 4 workers the full pull lands in ~3–5 nights of 24/7 running, ~6–8 if you
respect a 22:00–06:00 window.

## One-time setup — build the inventory

```bash
# Parse the ortofoto ATOM index once (~24 MB), pick newest year per sheet,
# write /Volumes/Elements/cuzk-bulk/ortofoto_sheets.json (~16 301 entries).
BULK_OUT_DIR=/Volumes/Elements/cuzk-bulk python3 bulk_ortofoto_inventory.py
```

Re-run any time to refresh (e.g. after ČÚZK flies a new cycle): terminal
states are preserved unless the resolved year changed, in which case that
sheet is reset to `pending` so the newer image replaces the stale one.

## Start (or resume after a pause / Mac reboot)

```bash
nohup /usr/bin/caffeinate -i -d \
  /usr/bin/python3 /Users/jan/projekty/inzerator/bulk_ortofoto.py \
  > ~/Library/Logs/inzerator/bulk-ortofoto-manual.log 2>&1 < /dev/null &
disown
```

Picks up wherever `ortofoto_sheets.json` left off — no extra flags.
`caffeinate -i -d` blocks sleep; `nohup` + `disown` survive SSH logout.

## Pause cleanly

```bash
pkill -TERM -f bulk_ortofoto.py
```

In-flight downloads abort, their sheets flip back to `pending`, state flushes,
lock releases, process exits in ≤ 2 s.

## Check progress

```bash
BULK_OUT_DIR=/Volumes/Elements/cuzk-bulk python3 bulk_ortofoto_status.py
```

`-v` adds top failure groupings; `--retry-failed` flips every `failed` back to
`pending`. **Sanity check:** `missing` should stay ~0 — ortho covers all of
ČR. A climbing `missing` count means the ZIP url pattern is wrong (re-check
`bulk_ortofoto_inventory.py`), not a real coverage gap.

## Inspect live process

```bash
pgrep -fl bulk_ortofoto.py
ps -p $(pgrep -f bulk_ortofoto.py) -o etime,rss,stat
lsof -p $(pgrep -f bulk_ortofoto.py) | grep openzu      # current TCP streams
tail -f /Volumes/Elements/cuzk-bulk/ortofoto_download.log
```

## Output layout

```
/Volumes/Elements/cuzk-bulk/
  ortofoto_sheets.json
  ortofoto_download.log
  ortofoto_<MAPNOM>/
    WRTO24.<year>.<MAPNOM>.jpg     # ~63–76 MB, 0.1239 m/px, S-JTSK
    WRTO24.<year>.<MAPNOM>.jgw     # + .TM33N / .TM34N world-file variants
```

The ZIP is deleted after extract (it only wraps the JPEG). Dir prefix
`ortofoto_` matches the server raw-crop glob, so the bulk can later feed the
ad-hoc raw-crop path via a configurable search root.

## Notes

- Concurrency 4 workers + 0.5–1.5 s jitter = same politeness budget ČÚZK
  accepted for the DMPOK pull. Don't raise without reason.
- `download.log` from DMPOK and `ortofoto_download.log` are separate files;
  the two bulks coexist in one `BULK_OUT_DIR` (distinct state + lock).
- DMPOK pull is complete (16 299/16 299), so there's no contention to manage.
