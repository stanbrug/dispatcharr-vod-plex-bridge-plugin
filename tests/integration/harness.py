"""End-to-end check against a real Dispatcharr with the mock services.

Runs inside the mock-services container (stdlib only):
    docker exec mock-services python -u /t/harness.py <step>
Steps: dry, sync, plex, dedupe, disable (see README in this folder).
"""

import json
import sys
import time
import urllib.parse
import urllib.request

BRIDGE = "http://dispatcharr-test:8888"
MOCK = "http://127.0.0.1"


def call(url, body=None, method=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw and raw[0] in "[{" else raw


def run_sync(dry):
    started = call(f"{BRIDGE}/api/auto-sync/run", {"dry_run": dry})
    assert started.get("status") == "started", started
    for _ in range(240):
        time.sleep(1)
        status = call(f"{BRIDGE}/api/auto-sync/status")
        if not status["running"]:
            return status["last_dry_run" if dry else "last_result"]
    raise SystemExit("sync did not finish")


def show(result):
    print(json.dumps(result, indent=2, ensure_ascii=False))


def listing(path):
    html = call(f"{BRIDGE}{path}")
    names = []
    for chunk in html.split('<a href="')[1:]:
        names.append(urllib.parse.unquote(chunk.split('"', 1)[0]))
    return names


def step_dry():
    show(call(f"{BRIDGE}/api/auto-sync/test"))
    show(run_sync(True))


def step_sync():
    show(run_sync(False))
    print("movies listing:", listing("/vod/"))
    shows = listing("/vod-series/auto/")
    print("series listing:", shows)
    for show_dir in shows:
        for season in listing(f"/vod-series/auto/{urllib.parse.quote(show_dir)}"):
            print(f"  {show_dir}{season}:", listing(f"/vod-series/auto/{urllib.parse.quote(show_dir)}{urllib.parse.quote(season)}"))
    print("plex log:", call(f"{MOCK}:9700/_test/plex/log"))


def step_plex():
    """Pretend Plex scanned everything, with Radarr/Sonarr files merged into
    the same items for the titles the next step will hand to Radarr/Sonarr."""
    items = []
    for i, name in enumerate(listing("/vod/")):
        medias = [{"id": 1000 + i, "Part": [{"file": f"/mnt/vod/movies/{name}", "size": 2147483648}]}]
        if "Oorlogswinter" in name:
            medias.insert(0, {"id": 5000, "Part": [{"file": "/mnt/debrid/movies/Oorlogswinter (2008)/Oorlogswinter.mkv", "size": 9}]})
        items.append({"_type": 1, "ratingKey": str(100 + i), "title": name.split(" (")[0], "Media": medias})
    # One Radarr-only movie that must never be touched.
    items.append({"_type": 1, "ratingKey": "999", "title": "Soldaat van Oranje",
                  "Media": [{"id": 9999, "Part": [{"file": "/mnt/debrid/movies/Soldaat van Oranje (1977)/x.mkv"}]}]})
    call(f"{MOCK}:9700/_test/plex/items", {"section": 1, "items": items})

    eps = []
    n = 0
    for show_dir in listing("/vod-series/auto/"):
        base = f"/vod-series/auto/{urllib.parse.quote(show_dir)}"
        for season in listing(base):
            for fname in listing(f"{base}{urllib.parse.quote(season)}"):
                if not fname.endswith(".mkv"):
                    continue
                n += 1
                s_e = fname.split(" - ")[1]  # S01E02
                medias = [{"id": 2000 + n, "Part": [{"file": f"/mnt/vod/series/auto/{show_dir}{season}{fname}", "size": 1073741824}]}]
                if "Flikken" in show_dir and s_e == "S01E02":
                    medias.insert(0, {"id": 6000, "Part": [{"file": "/mnt/debrid/shows/Flikken Maastricht/Season 01/S01E02.mkv"}]})
                eps.append({"_type": 4, "ratingKey": str(300 + n), "title": "ep",
                            "grandparentTitle": show_dir.split(" (")[0], "parentIndex": int(s_e[1:3]),
                            "index": int(s_e[4:6]), "Media": medias})
    # Sonarr-only episode of the same show: never to be touched.
    eps.append({"_type": 4, "ratingKey": "888", "title": "ep", "grandparentTitle": "Flikken Maastricht",
                "parentIndex": 1, "index": 1,
                "Media": [{"id": 8888, "Part": [{"file": "/mnt/debrid/shows/Flikken Maastricht/Season 01/S01E01.mkv"}]}]})
    call(f"{MOCK}:9700/_test/plex/items", {"section": 2, "items": eps})
    print(f"plex seeded: {len(items)} movie items, {len(eps)} episode items")


def step_dedupe():
    """Radarr gets Oorlogswinter, Sonarr gets Flikken S01E02 -> the nightly
    run must remove only those VOD versions."""
    call(f"{MOCK}:9600/_test/movies", [
        {"id": 1, "title": "Soldaat van Oranje", "tmdbId": 12104, "hasFile": True},
        {"id": 2, "title": "Oorlogswinter", "tmdbId": 15771, "hasFile": True},
    ])
    call(f"{MOCK}:9601/_test/episodes", {
        "series": [{"id": 1, "tmdbId": 4455, "statistics": {"episodeFileCount": 2}}],
        "episodes": {"1": [
            {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True},
            {"seasonNumber": 1, "episodeNumber": 2, "hasFile": True},
        ]},
    })
    show(run_sync(False))
    print("plex log:", call(f"{MOCK}:9700/_test/plex/log"))
    print("movies listing:", listing("/vod/"))


def step_redirect():
    name = listing("/vod/")[0]
    req = urllib.request.Request(f"{BRIDGE}/vod/{urllib.parse.quote(name)}", method="GET",
                                 headers={"User-Agent": "rclone/v1.68"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        resp = opener.open(req, timeout=30)
        print("redirect status", resp.status, resp.headers.get("Location"))
    except urllib.error.HTTPError as e:
        print("redirect status", e.code, e.headers.get("Location"), e.read()[:200])


if __name__ == "__main__":
    globals()[f"step_{sys.argv[1]}"]()
