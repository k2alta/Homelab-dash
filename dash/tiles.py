"""
Tiles for the configurable sections of the dashboard (Media & Apps, and any others you add).

Which tiles appear, in what order and under which heading, comes from services.toml.
Each tile has a `type`; a type is one function below, registered with @tile(...).
The function gets the tile (its settings) and returns what the tile shows:

    stats   [{"v": "143", "u": "GB", "l": "Series"}]      2-4 number boxes
    lines   [{"text": "...", "level": "ok|warning|error"}]  bordered rows
    more    int                                            "+N more" link under lines
    bars    [{"name": "...", "value": "12h 22m", "pct": 64}]
    list    [{"img": url, "title": "...", "sub": "...", "when": iso8601}]
    note    "small grey text above the bars/list"
    sub     {"text": "...", "dot": "green|grey|amber|red"}  replaces the subtitle
    right   "text in the header instead of the Online pill"
    link    click-through URL (overrides the default)
    alerts  [{"level": "warning|error|info", "title": "...", "text": "...", "link": "..."}]
            warnings and errors feed the "Needs attention" list

Adding a new type is one function; see the bottom of the README.
"""
import base64
import http.cookiejar
import json
import os
import re
import ssl
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

POLL, SLOW = 30, 300          # set by app.py from POLL_SECONDS / SLOW_POLL_SECONDS
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "icons")
ICON_CDN = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/{}.svg"

CTX = ssl.create_default_context()
CTX.check_hostname = False          # homelab services almost always use self-signed certs
CTX.verify_mode = ssl.CERT_NONE


# ============================================================================ small helpers
def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def n0(v):
    return "–" if v is None else f"{int(round(num(v))):,}"


def compact(v):
    """23456 -> 23.5k, 123456 -> 123k, 1234567 -> 1.2M; below 10,000 keeps thousands separators."""
    v = num(v)
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e4, "k")):
        if abs(v) >= size:
            x = v / (1e3 if suffix == "k" else size)
            return f"{x:.1f}{suffix}" if x < 100 else f"{x:.0f}{suffix}"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"


def fmt_rate(bps):
    bps = num(bps)
    if bps >= 1e6:
        return f"{bps / 1e6:.1f}", "MB/s"
    if bps >= 1e3:
        return f"{bps / 1e3:.0f}", "kB/s"
    return f"{bps:.0f}", "B/s"


def fmt_bytes(b):
    b = num(b)
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if b >= size:
            return f"{b / size:.1f}", unit
    return f"{b:.0f}", "B"


def fmt_min(m):
    m = int(num(m))
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def stat(v, label, unit=""):
    return {"v": str(v), "u": unit, "l": label}


def parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


class ConfigError(Exception):
    pass


# ============================================================================ HTTP
def request(url, method="GET", headers=None, body=None, form=None, opener=None, timeout=20):
    """Returns (status, bytes). Raises urllib.error.HTTPError for 4xx/5xx."""
    h = {"Accept": "application/json", "User-Agent": "homelab-dash"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    resp = opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, context=CTX, timeout=timeout)
    with resp:
        return resp.status, resp.read()


def get_json(url, **kw):
    _, raw = request(url, **kw)
    return json.loads(raw.decode("utf-8")) if raw else None


def cookie_opener():
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=CTX),
                                       urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def basic_auth(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


# ============================================================================ tile registry
TYPES, ALIASES = {}, {}


class Spec:
    def __init__(self, fn, name, title, subtitle, needs, every, slow, link, logo, icon):
        self.fn, self.name, self.title, self.subtitle = fn, name, title, subtitle
        self.needs, self.every, self.slow, self.link, self.logo, self.icon = needs, every, slow, link, logo, icon


def tile(name, title, subtitle, needs=("url",), every=None, slow=False, aliases=(), link=None, logo="", icon=None):
    """Register a tile type. needs = settings that must be filled in; slow = poll every SLOW_POLL_SECONDS."""
    def deco(fn):
        TYPES[name] = Spec(fn, name, title, subtitle, needs, every, slow, link, logo, icon)
        for a in aliases:
            ALIASES[a] = name
        return fn
    return deco


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Tile:
    def __init__(self, cfg, used_ids):
        raw_type = str(cfg.get("type") or "").strip().lower().replace("-", "_")
        self.type = ALIASES.get(raw_type, raw_type)
        if self.type not in TYPES:
            raise ConfigError(f"unknown tile type {cfg.get('type')!r}. Known types: {', '.join(sorted(TYPES))}")
        self.spec = spec = TYPES[self.type]
        self.missing_env = []
        self.cfg = self._expand(cfg)
        self.name = self.cfg.get("name") or spec.title
        self.subtitle = self.cfg.get("subtitle") or spec.subtitle
        self.section = self.cfg.get("section") or "Media & Apps"
        self.url = str(self.cfg.get("url") or "").rstrip("/")
        self.every = int(self.cfg.get("poll") or spec.every or (SLOW if spec.slow else POLL))
        base = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-") or self.type
        self.id, i = base, 2
        while self.id in used_ids:
            self.id, i = f"{base}-{i}", i + 1
        used_ids.add(self.id)
        self.missing = [k for k in spec.needs if not str(self.cfg.get(k) or "").strip()]
        self.icon, self.logo = self._icon(raw_type)
        self.state = {}       # per-tile scratch space: sessions, caches, previous counters

    def _expand(self, v):
        if isinstance(v, str):
            def sub(m):
                val = os.environ.get(m.group(1), "")
                if not val:
                    self.missing_env.append(m.group(1))
                return val
            return _ENV.sub(sub, v)
        if isinstance(v, dict):
            return {k: self._expand(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self._expand(x) for x in v]
        return v

    def _icon(self, raw_type):
        icon = str(self.cfg.get("icon") or "").strip()
        logo = self.spec.logo
        if icon.startswith(("http://", "https://", "/")):
            return icon, logo
        if "." in icon:                                   # a file in static/icons
            return f"icons/{icon}", logo
        for slug in ([icon] if icon else [raw_type, self.type, self.spec.icon]):
            for ext in ("svg", "png"):
                if os.path.isfile(os.path.join(ICON_DIR, f"{slug}.{ext}")):
                    return f"icons/{slug}.{ext}", logo
        if icon or self.type not in ("http", "json"):
            return ICON_CDN.format(icon or self.spec.icon or raw_type.replace("_", "-")), logo
        return "icons/generic.svg", logo

    @property
    def configured(self):
        return not self.missing

    def get(self, key, default=None):
        v = self.cfg.get(key)
        return default if v is None or v == "" else v

    def cached(self, key, seconds, fn):
        """Run fn at most every `seconds`; the rest of the time return its last result."""
        hit = self.state.get(("cache", key))
        if hit and time.time() - hit[0] < seconds:
            return hit[1]
        val = fn()
        self.state[("cache", key)] = (time.time(), val)
        return val

    def link(self):
        if self.cfg.get("link"):
            return self.cfg["link"]
        return self.spec.link(self) if self.spec.link else self.url

    def describe(self):
        return {"id": self.id, "type": self.type, "name": self.name, "sub": self.subtitle, "section": self.section,
                "icon": self.icon, "logo": self.logo, "link": self.link(), "configured": self.configured,
                "missing": [f"{k} (set {', '.join(self.missing_env)})" if self.missing_env else k for k in self.missing],
                "every": self.every}

    def poll(self):
        return self.spec.fn(self)


def load(path):
    """Read services.toml. Returns (dashboard settings, [Tile], [config problems])."""
    with open(path, "rb") as f:
        doc = tomllib.load(f)
    tiles, problems, ids = [], [], set()
    for n, cfg in enumerate(doc.get("tile") or [], 1):
        if cfg.get("enabled") is False:
            continue
        try:
            tiles.append(Tile(cfg, ids))
        except ConfigError as e:
            problems.append(f"services.toml tile #{n}: {e}")
    return doc.get("dashboard") or {}, tiles, problems


def legacy_from_env():
    """No services.toml: build the original five tiles from the old *_URL / *_KEY variables."""
    e = os.environ.get
    cfgs = [
        {"type": "plex", "url": e("PLEX_URL", ""), "token": e("PLEX_TOKEN", "")},
        {"type": "deluge", "url": e("DELUGE_URL", ""), "password": e("DELUGE_PASSWORD", "")},
        {"type": "sonarr", "url": e("SONARR_URL", ""), "key": e("SONARR_KEY", "")},
        {"type": "your_spotify", "url": e("YOURSPOTIFY_URL", ""), "token": e("YOURSPOTIFY_TOKEN", "")},
        {"type": "steam", "key": e("STEAM_KEY", ""), "steam_id": e("STEAM_ID", "")},
    ]
    ids = set()
    return [Tile(c, ids) for c in cfgs]


# ============================================================================ media servers
@tile("plex", "Plex", "Media server", needs=("url", "token"), link=lambda t: f"{t.url}/web")
def plex(t):
    def get(path, size=None):
        h = {"X-Plex-Token": t.get("token")}
        if size is not None:
            h.update({"X-Plex-Container-Start": "0", "X-Plex-Container-Size": str(size)})
        return (get_json(t.url + path, headers=h) or {}).get("MediaContainer") or {}

    sessions = get("/status/sessions")
    lines = []
    for m in (sessions.get("Metadata") or [])[:4]:
        title = f"{m['grandparentTitle']} · {m.get('title')}" if m.get("grandparentTitle") else m.get("title")
        user = (m.get("User") or {}).get("title")
        lines.append({"text": f"{title} · {user}" if user else title, "level": "ok"})

    def library():
        counts = {"movie": 0, "show": 0, "artist": 0}
        for d in get("/library/sections").get("Directory") or []:
            kind, key = d.get("type"), d.get("key")
            if kind in counts:
                mc = get(f"/library/sections/{key}/albums" if kind == "artist" else f"/library/sections/{key}/all", size=0)
                counts[kind] += int(num(mc.get("totalSize", mc.get("size"))))
        return counts

    lib = t.cached("library", SLOW, library)
    return {"stats": [stat(n0(sessions.get("size")), "Streams"), stat(n0(lib["movie"]), "Movies"),
                      stat(n0(lib["show"]), "TV shows"), stat(n0(lib["artist"]), "Albums")], "lines": lines}


@tile("jellyfin", "Jellyfin", "Media server", needs=("url", "key"), aliases=("emby",))
def jellyfin(t):
    h = {"Authorization": f'MediaBrowser Token="{t.get("key")}"', "X-Emby-Token": t.get("key")}
    sessions = get_json(f"{t.url}/Sessions?activeWithinSeconds=600", headers=h) or []
    playing = [s for s in sessions if s.get("NowPlayingItem")]
    lines = []
    for s in playing[:4]:
        it = s["NowPlayingItem"]
        title = f"{it['SeriesName']} · {it.get('Name')}" if it.get("SeriesName") else it.get("Name")
        lines.append({"text": f"{title} · {s.get('UserName')}" if s.get("UserName") else title, "level": "ok"})
    c = t.cached("counts", SLOW, lambda: get_json(f"{t.url}/Items/Counts", headers=h) or {})
    return {"stats": [stat(len(playing), "Streams"), stat(n0(c.get("MovieCount")), "Movies"),
                      stat(n0(c.get("SeriesCount")), "Series"), stat(n0(c.get("EpisodeCount")), "Episodes")], "lines": lines}


# ============================================================================ download clients
def _download_tile(down_bps, up_bps, downloading, seeding, active):
    dv, du = fmt_rate(down_bps)
    uv, uu = fmt_rate(up_bps)
    return {"stats": [stat(dv, "Download", du), stat(uv, "Upload", uu), stat(n0(downloading), "Downloading"), stat(n0(seeding), "Seeding")],
            "bars": [{"name": a["name"], "value": f"{a['progress']:.0f}%", "pct": a["progress"]} for a in active[:3]]}


@tile("deluge", "Deluge", "Torrents", needs=("url", "password"))
def deluge(t):
    if "opener" not in t.state:
        t.state.update(opener=cookie_opener(), id=0)

    def rpc(method, params):
        t.state["id"] += 1
        r = get_json(f"{t.url}/json", method="POST", opener=t.state["opener"],
                     body={"method": method, "params": params, "id": t.state["id"]}) or {}
        if r.get("error"):
            raise (PermissionError if r["error"].get("code") == 1 else RuntimeError)(r["error"].get("message"))
        return r.get("result")

    keys = [["state", "download_payload_rate", "upload_payload_rate", "progress", "name"], {}]
    try:
        ui = rpc("web.update_ui", keys)
    except PermissionError:
        if not rpc("auth.login", [t.get("password")]):
            raise RuntimeError("Deluge password rejected")
        ui = rpc("web.update_ui", keys)
    if not ui.get("connected"):
        hosts = rpc("web.get_hosts", []) or []
        if hosts:
            rpc("web.connect", [hosts[0][0]])
            ui = rpc("web.update_ui", keys)
    torrents = list((ui.get("torrents") or {}).values())
    states = [x.get("state") for x in torrents]
    active = [{"name": x.get("name"), "progress": num(x.get("progress"))} for x in torrents if x.get("state") == "Downloading"]
    out = _download_tile(sum(num(x.get("download_payload_rate")) for x in torrents), sum(num(x.get("upload_payload_rate")) for x in torrents),
                         states.count("Downloading"), states.count("Seeding"), active)
    if states.count("Error"):
        out["lines"] = [{"text": f"{states.count('Error')} torrent(s) in error", "level": "warning"}]
    return out


_QB_DL = {"downloading", "stalledDL", "metaDL", "forcedDL", "queuedDL", "checkingDL", "forcedMetaDL"}
_QB_UP = {"uploading", "stalledUP", "forcedUP", "queuedUP", "checkingUP"}


@tile("qbittorrent", "qBittorrent", "Torrents", needs=("url",), aliases=("qbit",))
def qbittorrent(t):
    if "opener" not in t.state:
        t.state["opener"] = cookie_opener()
    hdr = {"Referer": t.url, "Origin": t.url}

    def call(path):
        return get_json(f"{t.url}/api/v2/{path}", headers=hdr, opener=t.state["opener"])

    def login():
        _, raw = request(f"{t.url}/api/v2/auth/login", method="POST", headers=hdr, opener=t.state["opener"],
                         form={"username": t.get("username", ""), "password": t.get("password", "")})
        if raw.decode(errors="ignore").strip() != "Ok.":
            raise RuntimeError("qBittorrent login rejected (check username/password)")

    try:
        info = call("transfer/info")
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403) or not t.get("username"):
            raise
        login()
        info = call("transfer/info")
    torrents = call("torrents/info") or []
    dl = [x for x in torrents if x.get("state") in _QB_DL]
    out = _download_tile(info.get("dl_info_speed"), info.get("up_info_speed"), len(dl),
                         sum(1 for x in torrents if x.get("state") in _QB_UP),
                         [{"name": x.get("name"), "progress": num(x.get("progress")) * 100} for x in dl])
    bad = sum(1 for x in torrents if x.get("state") in ("error", "missingFiles"))
    if bad:
        out["lines"] = [{"text": f"{bad} torrent(s) in error", "level": "warning"}]
    return out


# ============================================================================ *arr
def _arr(t, ver, library, label):
    h = {"X-Api-Key": t.get("key")}
    base = f"{t.url}/api/{ver}"

    def lib():
        items = get_json(f"{base}/{library}", headers=h) or []
        out = {"count": len(items)}
        if library == "movie":      # Radarr: missing = monitored, released, no file
            out["missing"] = sum(1 for m in items if m.get("monitored") and m.get("isAvailable") and not m.get("hasFile"))
        return out

    lb = t.cached("library", SLOW, lib)
    if "missing" in lb:
        wanted = lb["missing"]
    else:
        wanted = int(num((get_json(f"{base}/wanted/missing?page=1&pageSize=1", headers=h) or {}).get("totalRecords")))
    queue = get_json(f"{base}/queue?page=1&pageSize=50", headers=h) or {}
    health = get_json(f"{base}/health", headers=h) or []

    issues, alerts = [], []
    for x in health:
        lvl = (x.get("type") or "").lower()
        if lvl in ("warning", "error"):
            issues.append({"text": x.get("message"), "level": lvl})
            alerts.append({"level": lvl, "title": f"{t.name} {lvl}", "text": x.get("message"), "link": x.get("wikiUrl")})
    for q in queue.get("records") or []:
        lvl = (q.get("trackedDownloadStatus") or "").lower()
        if lvl in ("warning", "error"):
            msgs = [m for sm in q.get("statusMessages") or [] for m in sm.get("messages") or []]
            reason = (msgs[0] if msgs else q.get("errorMessage")) or lvl
            issues.append({"text": f"{q.get('title')}: {reason}", "level": lvl})
            alerts.append({"level": lvl, "title": f"Download needs attention: {q.get('title')}", "text": reason})

    queued = int(num(queue.get("totalRecords")))
    if issues:
        lines = issues[:2]
    elif not queued and not wanted:
        lines = [{"text": "Queue empty, nothing missing", "level": "ok"}]
    else:
        lines = [{"text": f"{queued} in queue · {wanted} missing", "level": "ok"}]
    return {"stats": [stat(n0(lb["count"]), label), stat(n0(wanted), "Wanted"), stat(n0(queued), "Queued")],
            "lines": lines, "more": max(0, len(issues) - 2), "alerts": alerts}


@tile("sonarr", "Sonarr", "TV", needs=("url", "key"))
def sonarr(t):
    return _arr(t, "v3", "series", "Series")


@tile("radarr", "Radarr", "Movies", needs=("url", "key"))
def radarr(t):
    return _arr(t, "v3", "movie", "Movies")


@tile("lidarr", "Lidarr", "Music", needs=("url", "key"))
def lidarr(t):
    return _arr(t, "v1", "artist", "Artists")


@tile("prowlarr", "Prowlarr", "Indexers", needs=("url", "key"))
def prowlarr(t):
    h = {"X-Api-Key": t.get("key")}
    base = f"{t.url}/api/v1"
    indexers = t.cached("indexers", SLOW, lambda: get_json(f"{base}/indexer", headers=h) or [])
    failing = get_json(f"{base}/indexerstatus", headers=h) or []
    health = get_json(f"{base}/health", headers=h) or []
    lines, alerts = [], []
    for x in health:
        lvl = (x.get("type") or "").lower()
        if lvl in ("warning", "error"):
            lines.append({"text": x.get("message"), "level": lvl})
            alerts.append({"level": lvl, "title": f"{t.name} {lvl}", "text": x.get("message"), "link": x.get("wikiUrl")})
    return {"stats": [stat(len(indexers), "Indexers"), stat(sum(1 for i in indexers if i.get("enable")), "Enabled"),
                      stat(len(failing), "Failing")],
            "lines": lines[:2] or [{"text": "All indexers healthy", "level": "ok"}],
            "more": max(0, len(lines) - 2), "alerts": alerts}


@tile("seerr", "Seerr", "Requests", needs=("url", "key"), aliases=("jellyseerr", "overseerr"))
def seerr(t):
    c = get_json(f"{t.url}/api/v1/request/count", headers={"X-Api-Key": t.get("key")}) or {}
    pending = int(num(c.get("pending")))
    out = {"stats": [stat(n0(pending), "Pending"), stat(n0(c.get("processing")), "Processing"), stat(n0(c.get("available")), "Available")],
           "lines": [{"text": f"{pending} request{'s' if pending != 1 else ''} waiting for approval", "level": "warning"}] if pending
           else [{"text": "No requests waiting for approval", "level": "ok"}]}
    if pending and t.get("alert_pending", False):
        out["alerts"] = [{"level": "warning", "title": f"{t.name}: {pending} pending request(s)", "text": "Waiting for approval"}]
    return out


# ============================================================================ personal
@tile("your_spotify", "Spotify", "Listening stats", needs=("url", "token"), aliases=("spotify", "yourspotify"),
      link=lambda t: re.sub(r"/api$", "", t.url), icon="spotify")
def your_spotify(t):
    tok = urllib.parse.quote(t.get("token"))

    def stats():
        start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        q = f"?start={urllib.parse.quote(start)}&timeSplit=all&token={tok}"
        songs = get_json(f"{t.url}/spotify/songs_per{q}") or [{}]
        ms = get_json(f"{t.url}/spotify/time_per{q}") or [{}]
        artists = get_json(f"{t.url}/spotify/different_artists_per{q}") or [{}]
        return {"songs": int(num((songs[0] or {}).get("count"))), "hours": round(num((ms[0] or {}).get("count")) / 3_600_000, 1),
                "artists": len((artists[0] or {}).get("artists") or [])}

    s = t.cached("stats", SLOW, stats)
    rows = get_json(f"{t.url}/spotify/gethistory?number=3&offset=0&token={tok}") or []
    tracks = []
    for r in rows:
        tr = r.get("track") or {}
        images = sorted((tr.get("full_album") or {}).get("images") or [], key=lambda i: num(i.get("width"), 9999))
        art = next((i.get("url") for i in images if num(i.get("width"), 0) >= 64), images[-1].get("url") if images else None)
        tracks.append({"img": art or "", "title": tr.get("name"), "when": r.get("played_at"),
                       "sub": ", ".join(a.get("name", "") for a in tr.get("full_artists") or [])})
    return {"right": "Last 30 days", "stats": [stat(n0(s["songs"]), "Songs"), stat(s["hours"], "Listened", "h"), stat(n0(s["artists"]), "Artists")],
            "list": tracks, "list_art": True}


_STEAM_STATES = {0: "Offline", 1: "Online", 2: "Busy", 3: "Away", 4: "Snooze", 5: "Online", 6: "Online"}


@tile("steam", "Steam", "Gaming", needs=("key", "steam_id"), slow=True, link=lambda t: "https://store.steampowered.com", logo="logo-steam")
def steam(t):
    sid, key = str(t.get("steam_id")).strip(), t.get("key")
    if not re.fullmatch(r"\d{17}", sid):
        raise ConfigError(f"steam_id {sid!r} is not a 17-digit SteamID64")
    api = t.get("api", "https://api.steampowered.com").rstrip("/")
    prev = t.state.get("prev") or {}
    p = get_json(f"{api}/ISteamUser/GetPlayerSummaries/v2/?key={key}&steamids={sid}") or {}
    players = (p.get("response") or {}).get("players") or []
    if players:
        player = players[0]
        if player.get("steamid") and str(player["steamid"]) != sid:
            raise RuntimeError(f"Steam answered for {player['steamid']} but steam_id is {sid}. Quote the ID so it isn't rounded.")
        status = _STEAM_STATES.get(int(num(player.get("personastate"))), "Online")
        playing, profile = player.get("gameextrainfo"), player.get("profileurl")
    elif prev:          # Steam sometimes answers with an empty list; that isn't "offline"
        status, playing, profile = prev["status"], prev["playing"], prev["profile"]
    else:
        raise RuntimeError("Steam returned no player data (temporary Steam API hiccup)")
    r = (get_json(f"{api}/IPlayerService/GetRecentlyPlayedGames/v1/?key={key}&steamid={sid}&count=10") or {}).get("response") or {}
    if "total_count" not in r and "games" not in r and prev.get("games") is not None:
        games = prev["games"]          # bare {} = Steam being flaky; a real "nothing played" has total_count 0
    else:
        games = sorted(({"name": g.get("name"), "minutes": int(num(g.get("playtime_2weeks")))} for g in r.get("games") or []),
                       key=lambda g: -g["minutes"])[:4]
    t.state["prev"] = {"status": status, "playing": playing, "profile": profile, "games": games}
    on = status != "Offline"
    top = max([g["minutes"] for g in games] + [1])
    return {"sub": {"text": f"{status} · {'Playing ' + playing if playing else 'not in game'}" if on else status, "dot": "green" if on else "grey"},
            "right": "", "link": profile,
            "note": "Last 2 weeks" if games else "No games played in the last 2 weeks",
            "bars": [{"name": g["name"], "value": fmt_min(g["minutes"]), "pct": max(1, g["minutes"] / top * 100)} for g in games]}


# ============================================================================ infrastructure
@tile("authentik", "Authentik", "SSO", needs=("url", "token"))
def authentik(t):
    h = {"Authorization": f"Bearer {t.get('token')}"}
    api = f"{t.url}/api/v3"

    def count(path):
        return int(num(((get_json(f"{api}/{path}", headers=h) or {}).get("pagination") or {}).get("count")))

    def users():
        try:
            return count("core/users/?page_size=1&type=internal&type=external")   # people, not service accounts
        except urllib.error.HTTPError:
            return count("core/users/?page_size=1")

    v = t.cached("version", 3600, lambda: get_json(f"{api}/admin/version/", headers=h) or {})
    n_users = t.cached("users", SLOW, users)
    outposts = t.cached("outposts", SLOW, lambda: count("outposts/instances/?page_size=1"))
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    ev = get_json(f"{api}/events/events/?action=login_failed&ordering=-created&page_size=100", headers=h) or {}
    failed = sum(1 for e in ev.get("results") or [] if (parse_time(e.get("created")) or since) > since)
    limit = int(t.get("failed_login_alert", 10))
    lines, alerts = [], []
    if v.get("outdated"):
        lines.append({"text": f"Update available: {v.get('version_latest')}", "level": "warning"})
        alerts.append({"level": "warning", "title": f"{t.name} update available", "text": f"{v.get('version_current')} → {v.get('version_latest')}"})
    if failed >= limit:
        lines.append({"text": f"{failed} failed logins in the last 24 h", "level": "warning"})
        alerts.append({"level": "warning", "title": f"{t.name}: {failed} failed logins", "text": "In the last 24 hours"})
    if not lines:
        lines.append({"text": "Up to date, no login trouble", "level": "ok"})
    return {"sub": {"text": f"{t.subtitle} · {v.get('version_current')}" if v.get("version_current") else t.subtitle},
            "stats": [stat(n0(n_users), "Users"), stat(n0(outposts), "Outposts"), stat(n0(failed), "Failed")],
            "lines": lines, "alerts": alerts}


_PROM = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)')


@tile("cloudflared", "Cloudflare", "Tunnel", needs=("url",), aliases=("cloudflare_tunnel",),
      link=lambda t: "https://one.dash.cloudflare.com")
def cloudflared(t):
    try:
        ready = get_json(f"{t.url}/ready") or {}
    except urllib.error.HTTPError as e:
        if e.code == 503:
            raise RuntimeError("Tunnel down: no connections to Cloudflare") from None
        raise
    _, raw = request(f"{t.url}/metrics", headers={"Accept": "text/plain"})
    total = errors = 0.0
    version = None
    for line in raw.decode(errors="ignore").splitlines():
        m = _PROM.match(line)
        if not m:
            continue
        name, labels, value = m.groups()
        if name == "cloudflared_tunnel_total_requests":
            total += num(value)
        elif name == "cloudflared_tunnel_request_errors":
            errors += num(value)
        elif name == "build_info" and labels:
            vm = re.search(r'[{,]version="([^"]+)"', labels)
            version = vm.group(1) if vm else version
    now, prev = time.time(), t.state.get("prev")
    t.state["prev"] = (now, total)
    rate = (total - prev[1]) / (now - prev[0]) * 60 if prev and total >= prev[1] and now > prev[0] else None
    conns = int(num(ready.get("readyConnections")))
    want = int(t.get("expected_connections", 4))
    line = ({"text": f"Only {conns} of {want} edge connections up", "level": "warning"} if conns < want
            else {"text": f"Healthy · {conns} edge connections", "level": "ok"})
    return {"sub": {"text": f"Tunnel · {version}" if version else "Tunnel"},
            "stats": [stat(conns, "Edges"), stat("–" if rate is None else compact(rate), "Req/min"), stat(compact(errors), "Errors")],
            "lines": [line],
            "alerts": [{"level": "warning", "title": f"{t.name}: {conns} of {want} connections", "text": "Tunnel is degraded"}] if conns < want else []}


# ============================================================================ generic
@tile("http", "Website", "HTTP check", needs=("url",))
def http_check(t):
    """Is it up? Response time, status code and uptime since the dashboard started."""
    hdr = basic_auth(t.get("username"), t.get("password", "")) if t.get("username") else {}
    hdr.update(t.get("headers") or {})
    expect = t.get("expect")
    t0 = time.time()
    try:
        code, _ = request(t.get("url"), method=t.get("method", "GET"), headers=hdr, timeout=int(t.get("timeout", 10)))
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = None
    ms = (time.time() - t0) * 1000
    good = code is not None and (code in expect if isinstance(expect, list) else code < 400)
    hist = [h for h in t.state.get("hist", []) if h[0] > time.time() - 86400] + [(time.time(), good)]
    t.state["hist"] = hist
    if not good:
        raise RuntimeError(f"HTTP {code}" if code else "no response")
    up = sum(1 for h in hist if h[1]) / len(hist) * 100
    return {"stats": [stat(f"{ms:.0f}", "Latency", "ms"), stat(code, "Status"), stat(f"{up:.1f}" if up < 100 else "100", "Uptime", "%")]}


def _dig(data, path):
    for part in re.findall(r"[^.\[\]]+", str(path)):
        if isinstance(data, list):
            data = data[int(part)] if part.lstrip("-").isdigit() and -len(data) <= int(part) < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


@tile("json", "Custom", "JSON API", needs=("url", "stats"))
def json_tile(t):
    """Any JSON API: pick values by path, e.g. path = "queries.blocked" or "data[0].count"."""
    hdr = basic_auth(t.get("username"), t.get("password", "")) if t.get("username") else {}
    hdr.update(t.get("headers") or {})
    data = get_json(t.get("url"), headers=hdr)
    out = []
    for s in t.get("stats") or []:
        v, fmt, unit = _dig(data, s.get("path")), s.get("format", "number"), s.get("unit", "")
        if v is None:
            shown = "–"
        elif fmt == "bytes":
            shown, unit = fmt_bytes(v)
        elif fmt == "percent":
            shown, unit = (f"{num(v):.0f}" if num(v) >= 10 else f"{num(v):.1f}"), unit or "%"
        elif fmt == "text" or isinstance(v, str):
            shown = str(v)
        else:
            shown = compact(v)
        out.append(stat(shown, s.get("label", s.get("path")), unit))
    return {"stats": out}
