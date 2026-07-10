#!/bin/zsh
# Full-ČR z=17/18 fill — the rest of the country at populated-parts
# resolution (heights LERC + ortho @2x 512^2). Every stage is
# filesystem-resumable (skip-existing); safe to kill and re-launch.
#
#   nohup /usr/bin/caffeinate -i ./full_cr_chain.sh \
#     > ~/Library/Logs/inzerator/full-cr-chain.log 2>&1 & disown
#
# Stage 2/4 re-aggregate z=17 with FORCE: the masked F3 run baked fill
# quadrants (NODATA / FILL_RGB) into z=17 wherever children outside the
# populated mask didn't exist yet.
set -e
P=/Users/jan/projekty/inzerator
L=~/Library/Logs/inzerator

while pgrep -f 'reagg_ortho.py' > /dev/null; do sleep 300; done

echo "[$(date '+%F %T')] stage 1: heights z=18 base (full ČR, skip existing)"
/usr/bin/python3 $P/dispatch_pyramid.py --zmax 18 --zmin 18 --workers 4 \
  >> $L/full-heights-18.log 2>&1

echo "[$(date '+%F %T')] stage 2: heights z=17 forced re-agg"
/usr/bin/python3 $P/reagg_level.py --layer dmpok --z 17 \
  >> $L/full-heights-17.log 2>&1

echo "[$(date '+%F %T')] stage 3: ortho z=18 base @2x (full ČR, skip existing)"
/usr/bin/python3 $P/dispatch_ortho_pyramid.py --zmax 18 --zmin 18 --workers 3 \
  --size 512 >> $L/full-ortho-18.log 2>&1

echo "[$(date '+%F %T')] stage 4: ortho z=17 forced re-agg"
/usr/bin/python3 $P/reagg_level.py --layer ortho --z 17 \
  >> $L/full-ortho-17.log 2>&1

echo "[$(date '+%F %T')] full-ČR chain done"
