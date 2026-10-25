"""Reports from the app in SQLite. People stay anonymous: only a salted hash of their address is kept, for the hourly
limit and one vote per person per street per report type (the rating counts only each person's latest)."""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime

PER_HOUR = 10


class Reports:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        salt = os.path.join(os.path.dirname(path), "report_salt.txt")  # in data/, which is gitignored
        if not os.path.exists(salt):
            with open(salt, "w") as f:
                f.write(secrets.token_hex(16))
        with open(salt) as f:
            self.salt = f.read().strip()
        self.db.execute("CREATE TABLE IF NOT EXISTS reports (lat REAL, lon REAL, type TEXT, ts REAL, hour INTEGER, weekday INTEGER, by TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS reports_by ON reports (by, ts)")
        self.db.commit()
        old = os.path.join(os.path.dirname(path), "reports.json")
        if os.path.exists(old):  # reports saved before the database existed
            with open(old) as f:
                rows = json.load(f)
            self.db.executemany("INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, NULL)",
                                [(r["lat"], r["lon"], r["type"], r["ts"], r.get("hour"), r.get("weekday")) for r in rows])
            self.db.commit()
            os.replace(old, old + ".imported")

    def all(self):
        cur = self.db.execute("SELECT lat, lon, type, ts, hour, weekday, by FROM reports")
        out = [dict(zip(("lat", "lon", "type", "ts", "hour", "weekday", "by"), r)) for r in cur]
        for r in out:
            if r["hour"] is None:
                t = datetime.fromtimestamp(r["ts"])
                r["hour"], r["weekday"] = t.hour, t.weekday()
        return out

    def add(self, lat, lon, kind, address):
        """The new report, or None when this person already sent PER_HOUR reports in the last hour."""
        by = hashlib.sha256((self.salt + address).encode()).hexdigest()[:16]
        now = datetime.now()
        with self.lock:
            sent = self.db.execute("SELECT COUNT(*) FROM reports WHERE by = ? AND ts > ?", (by, time.time() - 3600)).fetchone()[0]
            if sent >= PER_HOUR:
                return None
            r = {"lat": lat, "lon": lon, "type": kind, "ts": now.timestamp(), "hour": now.hour, "weekday": now.weekday(), "by": by}
            self.db.execute("INSERT INTO reports VALUES (:lat, :lon, :type, :ts, :hour, :weekday, :by)", r)
            self.db.commit()
        return r
