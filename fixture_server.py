"""The app on the test fixture streets, offline, for the browser tests. Usage: python fixture_server.py PORT"""
import os
import sys
import tempfile
from http.server import ThreadingHTTPServer

import server
from test_safety import fixture
from tiles import TileStore

server.store = TileStore(tempfile.mkdtemp(), fetch=lambda *a: fixture())
server.is_raining = lambda *a: False
server.geocode = lambda q, near: [{"name": "City Pharmacy", "pt": [28.0007, 77.0025], "kind": "pharmacy"}]
server.REPORTS_PATH = os.path.join(tempfile.mkdtemp(), "reports.json")
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), server.Handler).serve_forever()
