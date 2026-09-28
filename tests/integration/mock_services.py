"""Fake IPTV provider (Xtream Codes API), Radarr, Sonarr and Plex for the
integration test. Pure stdlib; run inside a python container on the same
Docker network as Dispatcharr:

    python mock_services.py

Ports: 9500 provider, 9600 Radarr, 9601 Sonarr, 9700 Plex.
Plex state lives in memory: the test harness adds items with
POST /_test/plex/items and reads DELETE/refresh calls from GET /_test/plex/log.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# --- Provider catalog -------------------------------------------------------

VOD_CATEGORIES = [
    {"category_id": "1", "category_name": "NL Films", "parent_id": 0},
    {"category_id": "2", "category_name": "EN Films", "parent_id": 0},
    {"category_id": "3", "category_name": "Adults", "parent_id": 0},
]
MOVIES = [
    # stream_id, name, category, tmdb, year
    (101, "NL - Zwartboek", "1", "9075", 2006),        # new -> should be added
    (102, "NL - Soldaat van Oranje", "1", "12104", 1977),  # Radarr has file -> skip
    (103, "EN - Heat", "2", "949", 1995),               # EN group disabled -> never
    (104, "NL - Oorlogswinter", "1", "15771", 2008),    # Radarr knows, no file -> add
    (105, "NL - Geen ID Film", "1", "", 2020),          # no ids -> skip
    (106, "Adult Thing", "3", "1", 2020),               # adult group
]
SERIES_CATEGORIES = [
    {"category_id": "10", "category_name": "NL Series", "parent_id": 0},
    {"category_id": "11", "category_name": "EN Series", "parent_id": 0},
]
SERIES = [
    # series_id, name, category, tmdb, year, {season: episodes}
    (201, "NL - Flikken Maastricht", "10", "4455", 2007, {1: 3}),   # Sonarr has S01E01
    (202, "NL - Undercover", "10", "86248", 2019, {1: 2}),          # not in Sonarr
    (203, "EN - The Wire", "11", "1438", 2002, {1: 2}),             # EN disabled
]


def _movie_json(sid, name, cat, tmdb, year):
    return {
        "num": sid, "name": name, "stream_type": "movie", "stream_id": sid,
        "stream_icon": "", "rating": "7.5", "added": str(1700000000 + sid),
        "category_id": cat, "container_extension": "mkv", "tmdb": tmdb, "year": str(year),
    }


def _series_json(sid, name, cat, tmdb, year):
    return {
        "num": sid, "name": name, "series_id": sid, "cover": "", "plot": "Plot",
        "releaseDate": f"{year}-01-01", "rating": "8", "category_id": cat, "tmdb": tmdb,
        "last_modified": str(1700000000 + sid),
    }


def _series_info(series_id):
    for sid, name, cat, tmdb, year, seasons in SERIES:
        if sid == series_id:
            episodes = {}
            for season, count in seasons.items():
                episodes[str(season)] = [
                    {
                        "id": str(sid * 100 + season * 10 + n), "episode_num": n,
                        "title": f"Aflevering {n}", "container_extension": "mkv",
                        "season": season,
                        "info": {"duration_secs": 2700, "plot": "Ep plot"},
                    }
                    for n in range(1, count + 1)
                ]
            return {"info": {"name": name, "plot": "Plot", "releaseDate": f"{year}-01-01"}, "episodes": episodes}
    return {"info": {}, "episodes": {}}


# --- Radarr / Sonarr --------------------------------------------------------

RADARR_MOVIES = [
    {"id": 1, "title": "Soldaat van Oranje", "tmdbId": 12104, "imdbId": "tt0076734", "hasFile": True},
    {"id": 2, "title": "Oorlogswinter", "tmdbId": 15771, "imdbId": "tt1037149", "hasFile": False},
]
SONARR_SERIES = [
    {"id": 1, "title": "Flikken Maastricht", "tvdbId": 1, "tmdbId": 4455, "imdbId": None,
     "statistics": {"episodeFileCount": 1}},
]
SONARR_EPISODES = {
    1: [
        {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True},
        {"seasonNumber": 1, "episodeNumber": 2, "hasFile": False},
    ],
}

# --- Plex -------------------------------------------------------------------

PLEX = {"items": {"1": [], "2": []}, "log": []}
PLEX_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    service = "?"

    def log_message(self, fmt, *args):
        print(f"[{self.service}] {self.command} {self.path}", flush=True)

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        getattr(self, f"get_{self.service}")(url.path, q)

    def do_POST(self):
        url = urlparse(self.path)
        getattr(self, f"post_{self.service}", lambda *_: self._json({}, 404))(url.path)

    def do_DELETE(self):
        url = urlparse(self.path)
        if self.service != "plex":
            return self._json({}, 404)
        with PLEX_LOCK:
            PLEX["log"].append(["DELETE", url.path])
        self._json({})

    # provider
    def get_provider(self, path, q):
        if path.endswith("player_api.php"):
            action = q.get("action")
            if not action:
                return self._json({
                    "user_info": {"username": q.get("username"), "auth": 1, "status": "Active",
                                  "exp_date": "1999999999", "max_connections": "2", "active_cons": "0"},
                    "server_info": {"url": "mock-services", "port": "9500", "server_protocol": "http",
                                    "timezone": "Europe/Amsterdam", "timestamp_now": 1700000000},
                })
            if action == "get_vod_categories":
                return self._json(VOD_CATEGORIES)
            if action == "get_vod_streams":
                return self._json([_movie_json(*m) for m in MOVIES])
            if action == "get_series_categories":
                return self._json(SERIES_CATEGORIES)
            if action == "get_series":
                return self._json([_series_json(*s[:5]) for s in SERIES])
            if action == "get_series_info":
                return self._json(_series_info(int(q.get("series_id", 0))))
            if action == "get_vod_info":
                return self._json({"info": {"duration_secs": 6000, "bitrate": 4000}, "movie_data": {}})
            if action in ("get_live_categories", "get_live_streams"):
                return self._json([])
            return self._json([])
        # Stream URLs (/movie/u/p/101.mkv, /series/u/p/20111.mkv): a few bytes.
        body = b"\x1aE\xdf\xa3" + b"\0" * 1024
        self.send_response(200)
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # radarr / sonarr
    def _arr_auth(self):
        if self.headers.get("X-Api-Key") != "testkey":
            self._json({"error": "Unauthorized"}, 401)
            return False
        return True

    def get_radarr(self, path, q):
        if not self._arr_auth():
            return
        if path == "/api/v3/system/status":
            return self._json({"appName": "Radarr", "version": "6.4.4-mock"})
        if path == "/api/v3/movie":
            return self._json(RADARR_MOVIES)
        self._json({}, 404)

    def get_sonarr(self, path, q):
        if not self._arr_auth():
            return
        if path == "/api/v3/system/status":
            return self._json({"appName": "Sonarr", "version": "4.0.20-mock"})
        if path == "/api/v3/series":
            return self._json(SONARR_SERIES)
        if path == "/api/v3/episode":
            return self._json(SONARR_EPISODES.get(int(q.get("seriesId", 0)), []))
        self._json({}, 404)

    def post_radarr(self, path):
        # Test hook: replace Radarr's movie list.
        if path == "/_test/movies":
            RADARR_MOVIES[:] = self._body()
            return self._json({"ok": True})
        self._json({}, 404)

    def post_sonarr(self, path):
        if path == "/_test/episodes":
            data = self._body()
            SONARR_EPISODES.clear()
            SONARR_EPISODES.update({int(k): v for k, v in data["episodes"].items()})
            SONARR_SERIES[:] = data["series"]
            return self._json({"ok": True})
        self._json({}, 404)

    # plex
    def get_plex(self, path, q):
        if path == "/_test/plex/log":
            with PLEX_LOCK:
                return self._json(PLEX["log"])
        if path.startswith("/library/sections/") and path.endswith("/refresh"):
            with PLEX_LOCK:
                PLEX["log"].append(["REFRESH", path, q.get("path")])
            return self._json({})
        if path.startswith("/library/sections/") and path.endswith("/all"):
            section = path.split("/")[3]
            with PLEX_LOCK:
                items = [dict(i) for i in PLEX["items"].get(section, [])]
            plex_type = q.get("type")
            if plex_type:
                items = [i for i in items if str(i.get("_type")) == plex_type]
            if q.get("X-Plex-Container-Size") == "0":
                return self._json({"MediaContainer": {"totalSize": len(items), "Metadata": []}})
            return self._json({"MediaContainer": {"totalSize": len(items), "Metadata": items}})
        if path == "/status/sessions":
            body = b'<?xml version="1.0"?><MediaContainer size="0"></MediaContainer>'
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self._json({"MediaContainer": {}})

    def post_plex(self, path):
        if path == "/_test/plex/items":
            data = self._body()
            with PLEX_LOCK:
                PLEX["items"][str(data["section"])] = data["items"]
                PLEX["log"].clear()
            return self._json({"ok": True})
        self._json({}, 404)


def serve(service, port):
    handler = type(f"{service}Handler", (Handler,), {"service": service})
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()


if __name__ == "__main__":
    threads = [
        threading.Thread(target=serve, args=(name, port), daemon=True)
        for name, port in (("provider", 9500), ("radarr", 9600), ("sonarr", 9601), ("plex", 9700))
    ]
    for t in threads:
        t.start()
    print("mock services up", flush=True)
    for t in threads:
        t.join()
