#!/usr/bin/env bash
# Install jobq as a system service (systemd, cgroup v2). Run once from the account that will own the queue:
#   sudo bash <queue dir>/systemd/install.sh [--exclusive-swap] [--queue-ram-gb N]
# --exclusive-swap  : disable swap for system.slice and user.slice, so queue jobs are the only processes that swap
# --queue-ram-gb N  : RAM cap of all queue jobs together (default: total RAM - 5 GB); beyond it they page to swap
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
EXCL=0; QRAM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --exclusive-swap) EXCL=1 ;;
    --queue-ram-gb) QRAM="$2"; shift ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac; shift
done
DIR="$(cd "$(dirname "$0")/.." && pwd)"                 # the queue directory (jobq.py + state)
OWNER="${SUDO_USER:-$(stat -c %U "$DIR/jobq.py")}"
HOME_DIR="$(getent passwd "$OWNER" | cut -d: -f6)"
[ -n "$QRAM" ] || QRAM=$(( $(awk '/MemTotal/ {print int($2 / 1048576)}' /proc/meminfo) - 5 ))

cat > /etc/systemd/system/jobq.slice <<UNIT
[Unit]
Description=jobq: shared job queue

[Slice]
MemorySwapMax=infinity
# all queue jobs together: beyond this RAM they page to swap instead of pushing other users out of memory
MemoryMax=${QRAM}G
# lowest CPU and I/O weight: other users' work and interactive sessions always win, idle cores go to the queue
CPUWeight=1
IOWeight=1
UNIT

cat > /etc/systemd/system/jobq.service <<UNIT
[Unit]
Description=jobq daemon ($DIR)
RequiresMountsFor=$DIR
StartLimitIntervalSec=0

[Service]
User=$OWNER
Group=$(id -gn "$OWNER")
Slice=jobq.slice
# the daemon gives each job its own child cgroup (RAM cap, swap allowed, freezer)
Delegate=yes
# restarting or stopping the daemon leaves running jobs alone; the next daemon adopts them
KillMode=process
Environment=HOME=$HOME_DIR
ExecStart=/usr/bin/python3 $DIR/jobq.py daemon
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT

ln -sf "$DIR/jobq.py" /usr/local/bin/jobq
if [ "$EXCL" = 1 ]; then
  systemctl set-property system.slice MemorySwapMax=0
  systemctl set-property user.slice MemorySwapMax=0
fi
systemctl daemon-reload
systemctl enable --now jobq.service
sleep 2
systemctl --no-pager status jobq.service | head -8
for s in system.slice user.slice jobq.slice; do echo "$s swap.max $(cat /sys/fs/cgroup/$s/memory.swap.max 2>/dev/null)"; done
