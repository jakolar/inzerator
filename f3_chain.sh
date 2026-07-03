#!/bin/zsh
# F3 chain — high-zoom pre-bake (spec D3). Runs unattended, each stage is
# filesystem-resumable; safe to kill (SIGTERM drains) and re-launch.
#
#   nohup /usr/bin/caffeinate -i -d ./f3_chain.sh > ~/Library/Logs/inzerator/f3-chain.log 2>&1 & disown
#
# Stage 0 waits for the currently running ortho z<=16 dispatch (if any).
# Stage 1: heightmap z=16 base + z=15 agg (celoplošně; z<=14 exists → skip)
# Stage 2: heightmap z=18 base (populated mask) + z=17 agg
# Stage 3: ortho     z=18 base (populated mask) + z=17 agg
set -e
P=/Users/jan/projekty/inzerator
L=~/Library/Logs/inzerator
MASK=/Volumes/Elements/cuzk-pyramid/populated.json

echo "[$(date '+%F %T')] F3 chain start"
while pgrep -f 'dispatch_ortho_pyramid.py --workers' > /dev/null; do
  sleep 300
done
echo "[$(date '+%F %T')] stage 1: heightmap z=15..16"
/usr/bin/python3 $P/dispatch_pyramid.py --zmax 16 --zmin 15 --workers 4 \
  >> $L/f3-heightmap-1516.log 2>&1
echo "[$(date '+%F %T')] stage 2: heightmap z=17..18 (mask)"
/usr/bin/python3 $P/dispatch_pyramid.py --zmax 18 --zmin 17 --workers 4 \
  --mask $MASK >> $L/f3-heightmap-1718.log 2>&1
echo "[$(date '+%F %T')] stage 3: ortho z=17..18 (mask)"
/usr/bin/python3 $P/dispatch_ortho_pyramid.py --zmax 18 --zmin 17 --workers 4 \
  --mask $MASK >> $L/f3-ortho-1718.log 2>&1
echo "[$(date '+%F %T')] F3 chain done"
