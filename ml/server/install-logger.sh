#!/usr/bin/env bash
# SafeRoute data logger for the always-on server. Logs live bus counts and TomTom traffic every hour, nonstop,
# and restarts itself after crashes and reboots. Needs only python3 (no pip packages).
#
#   sudo bash install-logger.sh "<laptop public key line>"
#
# The optional argument is the laptop's public key (one line starting with ssh-ed25519). It lets the laptop
# copy the log files every 15 minutes through a locked-down user "logpull" that can only read files.
# Keys: copied from /opt/saferoute/data/ if the app is installed there, otherwise you are asked to paste them.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }
DIR=/opt/saferoute-logger
HERE=$(cd "$(dirname "$0")" && pwd)
PER_HOUR=${TRAFFIC_PER_HOUR:-60}   # 60 an hour = 1,440 TomTom calls a day, free tier is 2,500

command -v python3 >/dev/null || apt-get install -y python3
id saferoute >/dev/null 2>&1 || useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin saferoute
install -d -m 755 "$DIR" "$DIR/ml" "$DIR/ml/data"
install -d -m 700 "$DIR/data"
install -m 644 "$HERE/buses.py" "$HERE/traffic.py" "$DIR/"
install -m 644 "$HERE/ml/collect.py" "$HERE/ml/spots.py" "$DIR/ml/"

for k in otd_key.txt tomtom_key.txt; do
  if [ ! -s "$DIR/data/$k" ]; then
    if [ -s "/opt/saferoute/data/$k" ]; then
      cp "/opt/saferoute/data/$k" "$DIR/data/$k"
    else
      read -rsp "Paste the ${k%_key.txt} key (Enter to skip): " v; echo
      [ -n "$v" ] && printf '%s' "$v" > "$DIR/data/$k"
    fi
  fi
done
chmod 600 "$DIR"/data/*.txt 2>/dev/null || true
chown -R saferoute:saferoute "$DIR"

cat > /etc/systemd/system/saferoute-logger.service <<EOF
[Unit]
Description=SafeRoute data logger (bus counts and traffic, for the AI model)
After=network-online.target
Wants=network-online.target

[Service]
User=saferoute
WorkingDirectory=$DIR
Environment=TRAFFIC_PER_HOUR=$PER_HOUR
Environment=TZ=Asia/Kolkata
UMask=0022
ExecStart=/usr/bin/python3 -u ml/collect.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable saferoute-logger >/dev/null
systemctl restart saferoute-logger

if [ -n "${1:-}" ]; then
  case "$1" in ssh-ed25519\ *|ssh-rsa\ *) ;; *) echo "That doesn't look like a public key line, skipped."; set -- "";; esac
fi
if [ -n "${1:-}" ]; then
  id logpull >/dev/null 2>&1 || useradd --create-home --shell /bin/bash logpull
  passwd -l logpull >/dev/null
  install -d -m 700 -o logpull -g logpull /home/logpull/.ssh
  echo "restrict $1" > /home/logpull/.ssh/authorized_keys   # key only: no shell tricks, no forwarding, no password
  chown logpull:logpull /home/logpull/.ssh/authorized_keys; chmod 600 /home/logpull/.ssh/authorized_keys
  echo "Laptop key added for user logpull."
fi

sleep 5
journalctl -u saferoute-logger -n 5 --no-pager
echo
echo "Logger running. Files: $DIR/ml/data/ (traffic.csv, bus_counts.csv, bus_hours.csv)."
echo "Server address for the laptop: $(curl -s -4 --max-time 5 ifconfig.me || hostname -I | cut -d' ' -f1)"
