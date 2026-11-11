# SafeRoute logger kit

Logs live Delhi bus counts and TomTom traffic every hour, nonstop, on the server. The laptop copies the files every 15 minutes, by itself.

No API keys are in this kit. The installer copies them from `/opt/saferoute/data/` on the server, or asks you to paste them.

## Files

| File | Where it goes | What it does |
|---|---|---|
| `buses.py`, `traffic.py`, `ml/collect.py`, `ml/spots.py` | server | the logger (plain Python, no packages) |
| `install-logger.sh` | server | installs it as an always-on service (`saferoute-logger`), 60 traffic readings an hour |
| `pull-logs.bat` | laptop, SafeRoute folder | copies the files and merges new rows into `ml\data` |

## Steps

1. **Laptop (Claude does this):** make a key just for copying, and print its public line (`ssh-ed25519 ...`). The public line is not a secret.
2. **Server (you, once):** copy this folder to the server, then run:
   `sudo bash install-logger.sh "ssh-ed25519 AAAA... saferoute-pull"`
   It prints the last log lines and the server address.
3. **Laptop (Claude does this):** save the server address in `data\server_host.txt` and add a Task Scheduler job that runs `pull-logs.bat` every 15 minutes. Then stop the laptop's own logger.

## Checks

- Server: `systemctl status saferoute-logger` and `tail -3 /opt/saferoute-logger/ml/data/traffic.csv`
- Laptop: `ml\data\server\pull.log` says `ok` with a time.
- Stop the logger: `sudo systemctl disable --now saferoute-logger`
- Fewer readings: `sudo TRAFFIC_PER_HOUR=20 bash install-logger.sh`

The `logpull` user can only log in with the laptop's key (no password), and only reads the logger files. Its key has `restrict` set, so there is no forwarding or terminal. The logs are hourly counts only, with no vehicle ids or people.
