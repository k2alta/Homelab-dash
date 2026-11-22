#!/usr/bin/env python3
"""
homelab-dash: one-page homelab dashboard.

A background poller per service keeps the latest results in memory (and WAN throughput
history in SQLite). The browser only ever talks to this server, so API keys stay here
and any number of open tabs cost the services nothing extra.

  /                 the dashboard (static/index.html)
  /api/state        everything the page shows, one JSON document
  /api/throughput   WAN history for the chart (?hours=24, 5-minute averages)
  /healthz          liveness

Standard library only - runs on the stock python image, no pip install.
TrueNAS, UniFi and Running apps are configured with environment variables (compose file).
Everything else is a tile defined in services.toml; tile types live in tiles.py.
"""
import base64
import json
import logging
import mimetypes
import os
import re
import socket
import sqlite3
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tiles

# ============================================================================ config
env = lambda k, d="": os.environ.get(k, d).strip()

POLL = int(env("POLL_SECONDS", "30"))            # default for everything
SLOW = int(env("SLOW_POLL_SECONDS", "300"))      # top clients, Steam, Spotify stats, library counts
PORT = int(env("PORT", "3000"))
DB_PATH = env("DB_PATH", "/data/helper.db")      # same file the old helper used: history carries over
RETENTION_DAYS = int(env("RETENTION_DAYS", "7"))
TOP_N = int(env("TOP_N", "10"))
PRIMARY_POOL = env("PRIMARY_POOL")              # pool shown big on the TrueNAS tile (default: largest)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

TRUENAS_URL = env("TRUENAS_URL").rstrip("/")
TRUENAS_KEY = env("TRUENAS_KEY")
GLANCES_URL = env("GLANCES_URL").rstrip("/")
DOCKER_URL = env("DOCKER_URL", "http://dockerproxy:2375").rstrip("/")
UNIFI_URL = env("UNIFI_URL").rstrip("/")
UNIFI_KEY = env("UNIFI_KEY")
UNIFI_SITE = env("UNIFI_SITE", "default")
UNIFI_NAME = env("UNIFI_NAME", "UniFi Dream Machine Pro")
SERVICES_FILE = env("SERVICES_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "services.toml"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dash")

CTX = ssl.create_default_context()          # LAN services use self-signed certs
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# ============================================================================ state
_lock = threading.Lock()
STATE = {}   # source -> {"ok": bool, "error": str|None, "updated": epoch, "data": {...}}


def put(source, data=None, error=None):
    with _lock:
        prev = STATE.get(source, {})
        STATE[source] = {
            "ok": error is None,
            "error": error,
            "updated": int(time.time()) if error is None else prev.get("updated"),
            "data": data if error is None else prev.get("data"),   # keep last good data on failure
        }


def get(source):
    with _lock:
        return dict(STATE.get(source) or {})


# ============================================================================ helpers
def http_json(url, method="GET", headers=None, body=None, opener=None, timeout=20):
    h = {"Accept": "application/json"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    if opener:
        resp = opener.open(req, timeout=timeout)
    else:
        resp = urllib.request.urlopen(req, context=CTX, timeout=timeout)
    with resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8")) if raw else None


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def mbps(bytes_per_sec):
    return round(num(bytes_per_sec) * 8 / 1_000_000, 2)


def err_text(e):
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        return f"unreachable ({e.reason})"
    return f"{type(e).__name__}: {e}"


# ============================================================================ minimal websocket client (for TrueNAS)
class WebSocket:
    """Just enough RFC 6455 for JSON-RPC: text frames, fragmentation, ping/pong, close."""

    def __init__(self, url, timeout=20):
        u = urllib.parse.urlparse(url)
        secure = u.scheme in ("wss", "https")
        host, port = u.hostname, u.port or (443 if secure else 80)
        sock = socket.create_connection((host, port), timeout=timeout)
        if secure:
            sock = CTX.wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {u.path or '/'} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101" not in status:
            raise ConnectionError(f"websocket handshake failed: {status}")
        self.sock = sock

    def _exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode, payload):
        hdr = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack("!H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack("!Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text):
        self._send(0x1, text.encode("utf-8"))

    def recv_text(self):
        parts = []
        while True:
            b1, b2 = self._exact(2)
            fin, op, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._exact(8))[0]
            mask = self._exact(4) if b2 & 0x80 else None
            data = self._exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x8:
                raise ConnectionError("websocket closed by server")
            if op == 0x9:
                self._send(0xA, data)
                continue
            if op == 0xA:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8")

    def close(self):
        try:
            self._send(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class TrueNASSession:
    def __init__(self):
        # Always wss: TrueNAS 25.04+ revokes an API key that is ever sent over plain ws/http.
        base = re.sub(r"^https?://", "", TRUENAS_URL)
        self.ws = WebSocket(f"wss://{base}/api/current")
        self.id = 0
        if self.call("auth.login_with_api_key", [TRUENAS_KEY]) is not True:
            self.ws.close()
            raise RuntimeError("TrueNAS API key was rejected")

    def call(self, method, params=None):
        self.id += 1
        self.ws.send_text(json.dumps({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params or []}))
        while True:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == self.id:
                if msg.get("error"):
                    e = msg["error"]
                    raise RuntimeError(f"{method}: {e.get('message') if isinstance(e, dict) else e}")
                return msg.get("result")

    def close(self):
        self.ws.close()


# ============================================================================ pollers
def poll_truenas():
    s = TrueNASSession()
    try:
        info = s.call("system.info")
        alerts = s.call("alert.list") or []
        pools = s.call("pool.query") or []
        names = [p.get("name") for p in pools if p.get("name")]
        datasets = []
        if names:
            try:
                datasets = s.call("pool.dataset.query", [[["name", "in", names]], {"extra": {"retrieve_children": False}}])
            except Exception:
                datasets = s.call("pool.dataset.query", [[["name", "in", names]]])
        apps = s.call("app.query") or []
    finally:
        s.close()

    def parsed(ds, key):
        v = (ds or {}).get(key)
        return num(v.get("parsed") if isinstance(v, dict) else v)

    pool_out = []
    for p in pools:
        ds = next((d for d in datasets or [] if d.get("name") == p.get("name")), None)
        used, avail = parsed(ds, "used"), parsed(ds, "available")
        total = used + avail
        pool_out.append({"name": p.get("name"), "healthy": bool(p.get("healthy")), "status": p.get("status"),
                         "used": used, "total": total, "pct": round(used / total * 100, 1) if total else None})

    active = [a for a in alerts if not a.get("dismissed")]
    app_out = []
    for a in apps:
        app_out.append({
            "name": a.get("name"),
            "state": a.get("state"),
            "update": bool(a.get("upgrade_available") or a.get("image_updates_available")),
            "version": a.get("human_version") or a.get("version"),
            "custom": bool(a.get("custom_app")),
        })
    put("truenas", {
        "hostname": info.get("hostname"),
        "model": info.get("model"),
        "cores": info.get("cores"),
        "physmem": info.get("physmem"),
        "uptime_s": num(info.get("uptime_seconds")),
        "load1": (info.get("loadavg") or [None])[0],
        "pools": pool_out,
        "alerts": [{"level": (a.get("level") or "INFO").upper(), "text": (a.get("formatted") or a.get("klass") or "").strip(),
                    "klass": a.get("klass")} for a in active],
        "apps": app_out,
    })


def poll_glances():
    cpu = http_json(f"{GLANCES_URL}/api/4/cpu")
    mem = http_json(f"{GLANCES_URL}/api/4/mem")
    put("glances", {"cpu_pct": num(cpu.get("total")), "mem_used": num(mem.get("used")),
                    "mem_total": num(mem.get("total")), "mem_pct": num(mem.get("percent"))})


_net_prev = {}   # container id -> (t, rx, tx)


def _container_stats(cid):
    st = http_json(f"{DOCKER_URL}/containers/{cid}/stats?stream=false", timeout=25)
    cs, ps = st.get("cpu_stats") or {}, st.get("precpu_stats") or {}
    cpu_d = num((cs.get("cpu_usage") or {}).get("total_usage")) - num((ps.get("cpu_usage") or {}).get("total_usage"))
    sys_d = num(cs.get("system_cpu_usage")) - num(ps.get("system_cpu_usage"))
    ncpu = cs.get("online_cpus") or len((cs.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    # percent of the whole host (docker stats shows percent of one core: that number / ncpu)
    cpu = (cpu_d / sys_d) * 100 if sys_d > 0 and cpu_d > 0 else 0.0
    ms = st.get("memory_stats") or {}
    inactive = num((ms.get("stats") or {}).get("inactive_file") or (ms.get("stats") or {}).get("total_inactive_file"))
    mem = max(0.0, num(ms.get("usage")) - inactive)
    rx = sum(num(n.get("rx_bytes")) for n in (st.get("networks") or {}).values())
    tx = sum(num(n.get("tx_bytes")) for n in (st.get("networks") or {}).values())
    now = time.time()
    prev = _net_prev.get(cid)
    _net_prev[cid] = (now, rx, tx)
    rx_r = tx_r = 0.0
    if prev and now > prev[0] and rx >= prev[1] and tx >= prev[2]:
        rx_r, tx_r = (rx - prev[1]) / (now - prev[0]), (tx - prev[2]) / (now - prev[0])
    return {"cpu": cpu, "mem": mem, "rx": rx_r, "tx": tx_r, "ncpu": ncpu}


def poll_docker():
    containers = http_json(f"{DOCKER_URL}/containers/json")
    groups = {}
    for c in containers:
        labels = c.get("Labels") or {}
        project = labels.get("com.docker.compose.project") or (c.get("Names") or ["/?"])[0].lstrip("/")
        app = re.sub(r"^ix-", "", project)
        groups.setdefault(app, []).append(c.get("Id"))
    ids = [cid for v in groups.values() for cid in v]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(ids, pool.map(lambda i: _safe(_container_stats, i), ids)))
    apps = []
    for app, cids in groups.items():
        rs = [results[i] for i in cids if results.get(i)]
        apps.append({"name": app, "containers": len(cids),
                     "cpu": round(sum(r["cpu"] for r in rs), 2), "mem": sum(r["mem"] for r in rs),
                     "rx_mbps": mbps(sum(r["rx"] for r in rs)), "tx_mbps": mbps(sum(r["tx"] for r in rs))})
    for cid in list(_net_prev):
        if cid not in results:
            _net_prev.pop(cid, None)
    put("docker", {"apps": apps})


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        log.debug("stats failed: %s", e)
        return None


def unifi_get(path):
    return http_json(UNIFI_URL + path, headers={"X-API-KEY": UNIFI_KEY})


def db_exec(sql, args=(), fetch=False):
    with _lock:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            cur = conn.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            conn.commit()
            return rows
        finally:
            conn.close()


def poll_unifi():
    data = unifi_get(f"/proxy/network/api/s/{UNIFI_SITE}/stat/health").get("data", [])
    sub = {s.get("subsystem"): s for s in data}
    wan, lan, wlan = sub.get("wan") or {}, sub.get("lan") or {}, sub.get("wlan") or {}
    rx, tx = num(wan.get("rx_bytes-r")), num(wan.get("tx_bytes-r"))
    now = int(time.time())
    db_exec("INSERT OR REPLACE INTO samples (ts, rx, tx) VALUES (?, ?, ?)", (now, rx, tx))
    db_exec("DELETE FROM samples WHERE ts < ?", (now - RETENTION_DAYS * 86400,))
    put("unifi", {
        "name": UNIFI_NAME,
        "wan_status": wan.get("status"),
        "wan_ip": wan.get("wan_ip"),
        "wan_uptime_s": num((wan.get("gw_system-stats") or {}).get("uptime")) or None,
        "down_mbps": mbps(rx), "up_mbps": mbps(tx),
        "lan_users": int(num(lan.get("num_user"))), "wlan_users": int(num(wlan.get("num_user"))),
    })


_MAC_SUFFIX = re.compile(r"\s[0-9a-f]{2}:[0-9a-f]{2}$", re.I)


def _client_label(c):
    name, host = (c.get("name") or "").strip(), (c.get("hostname") or "").strip()
    if name and host and _MAC_SUFFIX.search(name) and name.startswith(host):
        name = host
    return name or host or c.get("oui") or c.get("mac") or "Unknown"


def poll_unifi_top():
    end = int(time.time() * 1000)
    t = unifi_get(f"/proxy/network/v2/api/site/{UNIFI_SITE}/traffic?start={end - 86_400_000}&end={end}&includeUnidentified=true")
    clients = {}
    for entry in (t or {}).get("client_usage_by_app", []):
        c = entry.get("client") or {}
        rec = clients.setdefault(c.get("mac") or _client_label(c), {"name": _client_label(c), "bytes": 0.0})
        for u in entry.get("usage_by_app") or []:
            rec["bytes"] += num(u.get("total_bytes")) or (num(u.get("bytes_received")) + num(u.get("bytes_transmitted")))
    put("unifi_top", {"clients": sorted(clients.values(), key=lambda r: -r["bytes"])[:TOP_N]})


def throughput(hours):
    hours = max(1, min(hours, RETENTION_DAYS * 24))
    bucket = 3600 if hours <= 48 else 6 * 3600
    since = int(time.time()) - hours * 3600
    rows = db_exec(f"SELECT (ts / {bucket}) * {bucket} AS b, AVG(rx), AVG(tx) FROM samples WHERE ts >= ? GROUP BY b ORDER BY b",
                   (since,), fetch=True)
    five = db_exec("SELECT MAX(d) FROM (SELECT AVG(rx) AS d FROM samples WHERE ts >= ? GROUP BY ts / 300)", (since,), fetch=True)
    return {"bucket_seconds": bucket, "hours": hours,
            "peak_5min_mbps": mbps(five[0][0]) if five and five[0][0] is not None else None,
            "points": [{"t": r[0], "down": mbps(r[1]), "up": mbps(r[2])} for r in rows]}


# ============================================================================ scheduling
SOURCES = [
    # key, label, poll fn, interval, configured?
    ("truenas", "TrueNAS", poll_truenas, POLL, bool(TRUENAS_URL and TRUENAS_KEY)),
    ("glances", "Glances", poll_glances, POLL, bool(GLANCES_URL)),
    ("docker", "Docker", poll_docker, POLL, bool(DOCKER_URL)),
    ("unifi", "UniFi", poll_unifi, POLL, bool(UNIFI_URL and UNIFI_KEY)),
    ("unifi_top", "UniFi top clients", poll_unifi_top, SLOW, bool(UNIFI_URL and UNIFI_KEY)),
]
LABELS = {k: label for k, label, *_ in SOURCES}

# configurable tiles (services.toml, or the old PLEX_/DELUGE_/... variables if that file doesn't exist)
tiles.POLL, tiles.SLOW = POLL, SLOW
DASH = {}
TILES = []
CONFIG_PROBLEMS = []


def load_tiles():
    global DASH, TILES, CONFIG_PROBLEMS
    if os.path.isfile(SERVICES_FILE):
        try:
            DASH, TILES, CONFIG_PROBLEMS = tiles.load(SERVICES_FILE)
            log.info("loaded %d tiles from %s", len(TILES), SERVICES_FILE)
        except Exception as e:         # bad TOML: show it on the page instead of crashing
            DASH, TILES, CONFIG_PROBLEMS = {}, [], [f"services.toml could not be read: {e}"]
            log.error("%s", CONFIG_PROBLEMS[0])
    else:
        DASH, TILES = {}, tiles.legacy_from_env()
        log.info("no %s - using the PLEX_/DELUGE_/SONARR_/YOURSPOTIFY_/STEAM_ variables", SERVICES_FILE)
    for p in CONFIG_PROBLEMS:
        log.warning("%s", p)


def poll_tile(t):
    data = t.poll()
    put(f"tile:{t.id}", data)


def loop(key, fn, every):
    while True:
        started = time.time()
        try:
            fn()
        except Exception as e:
            put(key, error=err_text(e))
            log.warning("%s: %s", key, err_text(e))
        time.sleep(max(5, every - (time.time() - started)))


# ============================================================================ state document
TN_LEVEL = {"INFO": "info", "NOTICE": "info", "WARNING": "warning", "ERROR": "error",
            "CRITICAL": "error", "ALERT": "error", "EMERGENCY": "error"}


def build_state():
    with _lock:
        snap = {k: dict(v) for k, v in STATE.items()}
    configured = {k for k, _, _, _, ok in SOURCES if ok}

    def section(k):
        s = snap.get(k) or {}
        return {"configured": k in configured, "ok": s.get("ok"), "error": s.get("error"),
                "updated": s.get("updated"), "data": s.get("data")}

    out = {k: section(k) for k, *_ in SOURCES}
    alerts = []

    tn = (snap.get("truenas") or {}).get("data") or {}
    for a in tn.get("alerts") or []:
        alerts.append({"source": "TrueNAS", "level": TN_LEVEL.get(a["level"], "info"), "title": "TrueNAS alert", "text": a["text"]})
    updates = [a["name"] for a in tn.get("apps") or [] if a.get("update")]
    for name in updates:
        alerts.append({"source": "TrueNAS", "level": "warning", "title": f"Update available: {name}", "text": "New app version or image in TrueNAS Apps"})
    for p in CONFIG_PROBLEMS:
        alerts.append({"source": "Dashboard", "level": "error", "title": "Configuration problem", "text": p})
    for k in ("truenas", "unifi"):
        s = snap.get(k) or {}
        if k in configured and s.get("ok") is False:
            alerts.append({"source": LABELS[k], "level": "error", "title": f"{LABELS[k]} unreachable", "text": s.get("error")})

    tile_out = []
    services = [(k, k in configured) for k in ("truenas", "unifi")]
    for t in TILES:
        s = snap.get(f"tile:{t.id}") or {}
        d = s.get("data") or {}
        tile_out.append({**t.describe(), "ok": s.get("ok"), "error": s.get("error"), "updated": s.get("updated"), "data": s.get("data")})
        services.append((f"tile:{t.id}", t.configured))
        for a in d.get("alerts") or []:
            alerts.append({"source": t.name, **a})
        if t.configured and s.get("ok") is False:
            alerts.append({"source": t.name, "level": "error", "title": f"{t.name} unreachable", "text": s.get("error")})
    out["tiles"] = tile_out

    online = sum(1 for k, ok in services if ok and (snap.get(k) or {}).get("ok"))
    offline = sum(1 for k, ok in services if ok and (snap.get(k) or {}).get("ok") is False)
    out.update({
        "now": int(time.time()),
        "poll_seconds": POLL,
        "slow_poll_seconds": SLOW,
        "kpis": {"online": online, "offline": offline,
                 "attention": sum(1 for a in alerts if a["level"] in ("warning", "error") and not a["title"].endswith("unreachable"))},
        "alerts": sorted(alerts, key=lambda a: {"error": 0, "warning": 1, "info": 2}.get(a["level"], 3)),
        "app_updates": updates,
        "primary_pool": PRIMARY_POOL,
        "title": DASH.get("title") or "HomeLab",
        "links": {"truenas": TRUENAS_URL, "unifi": UNIFI_URL},
    })
    return out


# ============================================================================ HTTP
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/api/state":
                return self._json(build_state())
            if u.path == "/api/throughput":
                return self._json(throughput(int(q.get("hours", ["24"])[0])))
            if u.path == "/healthz":
                return self._json({"ok": True})
            rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            full = os.path.realpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                return self._send(200, f.read(), ctype)
        except Exception as e:
            log.exception("request failed")
            return self._json({"error": str(e)}, 500)

    def log_message(self, fmt, *args):
        pass


def main():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db_exec("CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, rx REAL, tx REAL)")
    for key, label, fn, every, ok in SOURCES:
        if ok:
            threading.Thread(target=loop, args=(key, fn, every), daemon=True, name=key).start()
        else:
            log.info("%s not configured, skipping", label)
    load_tiles()
    for t in TILES:
        if t.configured:
            threading.Thread(target=loop, args=(f"tile:{t.id}", lambda t=t: poll_tile(t), t.every), daemon=True, name=t.id).start()
        else:
            log.info("tile %s (%s) not configured: missing %s", t.name, t.type, ", ".join(t.describe()["missing"]))
    log.info("homelab-dash on :%d (poll %ds, slow %ds)", PORT, POLL, SLOW)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
