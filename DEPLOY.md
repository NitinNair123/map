# SafeRoute on an Ubuntu VPS: checklist

Do these in order. Replace `saferoute.example.com` with your domain and `nitin` with your VPS username.

## 1. Before you start
- A domain (or a free DuckDNS name) whose **A record points to the VPS IP**. Phones only share location with HTTPS sites, so this isn't optional.
- 2 GB RAM is comfortable. On 1 GB, skip the OpenStreetMap import (step 4) and add swap:
  `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab`

## 2. Code and Python
```
sudo apt update && sudo apt install -y python3 python3-pip git
sudo git clone https://github.com/NitinNair123/map /opt/saferoute && sudo chown -R nitin /opt/saferoute
pip install --break-system-packages pyarrow   # faster Overture streets and places; optional but recommended
```

## 3. Copy the keys (never put them on GitHub)
From your laptop (PowerShell has `scp` built in):
```
scp "C:\Users\nitin\Downloads\hackathon apps\saferoute-new\data\*.txt" nitin@VPS_IP:/opt/saferoute/data/
```
That copies `mapillary_token.txt`, `geoapify_key.txt`, `otd_key.txt`, `tomtom_key.txt` and `report_salt.txt`. Also copy `data/reports.db` if you want to keep the reports made so far. Then on the VPS: `chmod 600 /opt/saferoute/data/*.txt`

## 4. Map data (optional)
The server downloads map tiles on demand, so it works without this. For instant routes in Delhi NCR run `pip install --break-system-packages osmium && python3 /opt/saferoute/import_osm.py` once (about 575 MB download, 10-15 min, needs 2 GB RAM). Or copy `data/tiles/` from the laptop instead.

## 5. Run it as a service (replaces SafeRoute.vbs)
`sudo nano /etc/systemd/system/saferoute.service`:
```
[Unit]
Description=SafeRoute
After=network-online.target

[Service]
User=nitin
WorkingDirectory=/opt/saferoute
Environment=TZ=Asia/Kolkata
ExecStart=/usr/bin/python3 server.py 8000
Restart=always

[Install]
WantedBy=multi-user.target
```
Then `sudo systemctl enable --now saferoute` and check with `journalctl -u saferoute -f`.

`TZ=Asia/Kolkata` matters: VPSs run on UTC, and anything without a time from the phone would otherwise be rated 5.5 hours off.

## 6. HTTPS with Caddy
```
sudo apt install -y caddy
echo 'saferoute.example.com {
    reverse_proxy localhost:8000
}' | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy
```
Caddy gets and renews the free certificate itself.

**Don't set `SAFEROUTE_PUBLIC=1`** in this setup. Caddy talks to SafeRoute from the same machine, which is already allowed, and leaving it off means nobody can reach the plain-HTTP port 8000 directly. Only set it if you skip Caddy, which you shouldn't.

## 7. Firewall
```
sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
```

## 8. Check
Open `https://saferoute.example.com/app/` on your phone, allow location, plan a route. The service log should say Live buses / Live traffic: on.

## Updating later
`cd /opt/saferoute && git pull && sudo systemctl restart saferoute`

## Before selling or going big
A real contact email, a privacy policy, and keep an eye on disk: `data/tiles/` grows as people plan trips in new places.
