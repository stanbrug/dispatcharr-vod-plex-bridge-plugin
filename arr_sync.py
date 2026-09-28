"""Radarr/Sonarr-aware automatic sync for VOD To Plex.

Keeps the activated VOD library in step with two sources of truth:

* Dispatcharr decides WHAT may appear: only movies/series from VOD groups
  (categories) that are enabled on an active M3U account.
* Radarr/Sonarr decide what must NOT appear: a movie Radarr already has a
  file for, or an episode Sonarr already has a file for, stays out of the
  VOD library so Plex never shows two copies of the same title. When
  Radarr/Sonarr later gets the file, the nightly run removes the VOD copy.

A title Radarr/Sonarr knows about but has no file for yet keeps its VOD
copy -- it is the fallback until the real download lands.

The sync never removes anything based on a failed Radarr/Sonarr fetch: if
either API can't be read, that half of the run is skipped entirely.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime

import requests

logger = logging.getLogger("vod_plex_bridge.arr_sync")

_IMDB_RE = re.compile(r"^tt\d+$")


def norm_tmdb(value):
    """TMDB ids arrive as int, str, "0" or "" depending on the source."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    return s if s.isdigit() and s.strip("0") else None


def norm_imdb(value):
    s = str(value or "").strip().lower()
    return s if _IMDB_RE.match(s) else None


class ArrError(Exception):
    pass


class ArrClient:
    """Minimal Radarr/Sonarr v3 API client (both share the same auth/shape)."""

    def __init__(self, name, base_url, api_key, timeout=60):
        self.name = name
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    def configured(self):
        return bool(self.base_url and self.api_key)

    def get(self, path, params=None):
        try:
            resp = requests.get(
                f"{self.base_url}/api/v3/{path.lstrip('/')}",
                headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ArrError(f"{self.name}: {type(e).__name__}: {e}") from e
        if resp.status_code != 200:
            raise ArrError(f"{self.name}: HTTP {resp.status_code} on /api/v3/{path}")
        try:
            return resp.json()
        except ValueError as e:
            raise ArrError(f"{self.name}: invalid JSON on /api/v3/{path}") from e

    def test(self):
        status = self.get("system/status")
        return {"app": status.get("appName", self.name), "version": status.get("version")}


class RadarrIndex:
    """Which movies Radarr already provides.

    owned = Radarr has a file (hasFile). count_missing_as_owned widens that to
    every movie Radarr knows about, for users who'd rather wait for the
    download than watch the VOD copy in the meantime.
    """

    def __init__(self, movies, count_missing_as_owned=False):
        self.owned_tmdb = set()
        self.owned_imdb = set()
        self.total = 0
        self.with_file = 0
        for m in movies or []:
            self.total += 1
            has_file = bool(m.get("hasFile"))
            if has_file:
                self.with_file += 1
            if not (has_file or count_missing_as_owned):
                continue
            tmdb = norm_tmdb(m.get("tmdbId"))
            imdb = norm_imdb(m.get("imdbId"))
            if tmdb:
                self.owned_tmdb.add(tmdb)
            if imdb:
                self.owned_imdb.add(imdb)

    @classmethod
    def fetch(cls, client, count_missing_as_owned=False):
        return cls(client.get("movie"), count_missing_as_owned=count_missing_as_owned)

    def owns(self, tmdb_id, imdb_id):
        tmdb = norm_tmdb(tmdb_id)
        imdb = norm_imdb(imdb_id)
        return bool((tmdb and tmdb in self.owned_tmdb) or (imdb and imdb in self.owned_imdb))


_TRAILING_TAG_RE = re.compile(r"\s*\((?:\d{4}|[A-Za-z]{2,5}(?:\s?[A-Za-z]{2,4})?)\)\s*$")


def norm_title(title):
    """Comparable form of a show title: trailing "(2017)" / "(NL)" / "(MULTI)"
    tags dropped, lowercase, letters and digits only ("&" == "and")."""
    title = title or ""
    while True:
        stripped = _TRAILING_TAG_RE.sub("", title)
        if stripped == title:
            break
        title = stripped
    return re.sub(r"[^0-9a-z]", "", title.lower().replace("&", "and"))


class SonarrIndex:
    """Which episodes Sonarr already provides.

    Series are matched on TMDB id first, then IMDb id (Dispatcharr stores no
    TVDB id), then -- because Sonarr often has no TMDB id for local/Dutch
    shows (tmdbId 0) -- on title + year: same normalized title and a year at
    most one apart, or, when either side lacks a year, a title that only one
    Sonarr series has. Different shows sharing a title (The Office 2005 vs
    2024, One Piece 1999 vs 2023) differ in year and stay apart.

    Episode lists are only fetched for matched series that have at least one
    file, and cached for the lifetime of this index (one sync run).
    """

    def __init__(self, client, series_list, count_missing_as_owned=False):
        self.client = client
        self.count_missing_as_owned = count_missing_as_owned
        self._by_tmdb = {}
        self._by_imdb = {}
        self._by_title = {}  # normalized title -> [(sonarr id, year)]
        self._series = {}
        self._episodes = {}
        self.total = 0
        for s in series_list or []:
            sid = s.get("id")
            if sid is None:
                continue
            self.total += 1
            self._series[sid] = s
            tmdb = norm_tmdb(s.get("tmdbId"))
            imdb = norm_imdb(s.get("imdbId"))
            if tmdb:
                self._by_tmdb.setdefault(tmdb, sid)
            if imdb:
                self._by_imdb.setdefault(imdb, sid)
            titles = {s.get("title")} | {a.get("title") for a in s.get("alternateTitles") or []}
            for t in titles:
                key = norm_title(t)
                if key:
                    self._by_title.setdefault(key, [])
                    if (sid, s.get("year")) not in self._by_title[key]:
                        self._by_title[key].append((sid, s.get("year")))

    @classmethod
    def fetch(cls, client, count_missing_as_owned=False):
        return cls(client, client.get("series"), count_missing_as_owned=count_missing_as_owned)

    def find_series_id(self, tmdb_id, imdb_id, title=None, year=None):
        tmdb = norm_tmdb(tmdb_id)
        if tmdb and tmdb in self._by_tmdb:
            return self._by_tmdb[tmdb]
        imdb = norm_imdb(imdb_id)
        if imdb and imdb in self._by_imdb:
            return self._by_imdb[imdb]
        candidates = self._by_title.get(norm_title(title)) if title else None
        if not candidates:
            return None
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        if year is not None:
            close = [sid for sid, y in candidates if y and abs(int(y) - year) <= 1]
            if len(close) == 1:
                return close[0]
            if close or all(y for _, y in candidates):
                return None  # ambiguous, or only other years: a different show
        return candidates[0][0] if len(candidates) == 1 else None

    def owned_episodes(self, sonarr_series_id):
        """{(season, episode)} Sonarr covers for this series. Raises ArrError
        on a failed fetch so the caller can skip the series instead of
        treating "couldn't check" as "owns nothing"."""
        if sonarr_series_id is None:
            return set()
        if sonarr_series_id in self._episodes:
            return self._episodes[sonarr_series_id]

        series = self._series.get(sonarr_series_id, {})
        stats = series.get("statistics") or {}
        file_count = stats.get("episodeFileCount")
        if not self.count_missing_as_owned and file_count == 0:
            self._episodes[sonarr_series_id] = set()
            return self._episodes[sonarr_series_id]

        episodes = self.client.get("episode", params={"seriesId": sonarr_series_id})
        owned = set()
        for ep in episodes or []:
            if not (ep.get("hasFile") or self.count_missing_as_owned):
                continue
            season = ep.get("seasonNumber")
            number = ep.get("episodeNumber")
            if season is None or number is None:
                continue
            owned.add((int(season), int(number)))
        self._episodes[sonarr_series_id] = owned
        return owned


def plan_movies(eligible, activated, radarr, failed_until, now, max_new, require_ids=True,
                dedupe_manual=True, activated_ids=None):
    """Pure planning step for movies (no ORM, no I/O -- unit-testable).

    eligible: {movie_id: {"tmdb", "imdb", "name", "added"}} from enabled groups.
    activated: {movie_id: entry} (the bridge's _activated).
    activated_ids: {movie_id: {"tmdb", "imdb"}} ids for activated movies, which
        may no longer be in `eligible` (group disabled since).
    radarr: RadarrIndex.
    failed_until: {movie_id: epoch} activation cooldowns.

    Returns dict with "add", "remove_owned", "remove_ineligible", counters.
    """
    activated_ids = activated_ids or {}
    plan = {
        "add": [],
        "remove_owned": [],
        "remove_ineligible": [],
        "skipped_owned": 0,
        "skipped_no_ids": 0,
        "skipped_cooldown": 0,
        "eligible": len(eligible),
        "add_capped": 0,
    }

    for mid, entry in activated.items():
        source = entry.get("source", "manual")
        ids = activated_ids.get(mid) or eligible.get(mid) or {}
        owned = radarr.owns(ids.get("tmdb"), ids.get("imdb"))
        if owned and (source == "auto" or dedupe_manual):
            plan["remove_owned"].append(mid)
        elif source == "auto" and mid not in eligible:
            plan["remove_ineligible"].append(mid)

    candidates = []
    for mid, info in eligible.items():
        if mid in activated:
            continue
        if radarr.owns(info.get("tmdb"), info.get("imdb")):
            plan["skipped_owned"] += 1
            continue
        if require_ids and not (info.get("tmdb") or info.get("imdb")):
            plan["skipped_no_ids"] += 1
            continue
        if failed_until.get(mid, 0) > now:
            plan["skipped_cooldown"] += 1
            continue
        candidates.append((info.get("added") or 0, mid))

    # Newest additions first: a capped nightly run should bring in what the
    # provider just added before working through the back catalog.
    candidates.sort(key=lambda c: c[0], reverse=True)
    plan["add"] = [mid for _, mid in candidates[:max_new]] if max_new > 0 else []
    plan["add_capped"] = max(0, len(candidates) - len(plan["add"]))
    return plan


def plan_episodes(eligible_episodes, activated, owned_lookup, failed_until, now, max_new,
                  dedupe_manual=True):
    """Pure planning step for episodes.

    eligible_episodes: {episode_id: {"series_id", "season", "episode", "added"}}
    activated: bridge._episodes_activated
    owned_lookup(series_id) -> set of (season, episode) Sonarr has, or None when
        the Sonarr state for that series couldn't be determined (skip it).
    """
    plan = {
        "add": [],
        "remove_owned": [],
        "remove_ineligible": [],
        "skipped_owned": 0,
        "skipped_cooldown": 0,
        "skipped_unknown": 0,
        "eligible": len(eligible_episodes),
        "add_capped": 0,
    }

    for eid, entry in activated.items():
        source = entry.get("source", "manual")
        sid = entry.get("series_id")
        owned = owned_lookup(sid) if sid else set()
        key = (entry.get("season_number"), entry.get("episode_number"))
        if owned is not None and key in owned and (source == "auto" or dedupe_manual):
            plan["remove_owned"].append(eid)
        elif source == "auto" and eid not in eligible_episodes:
            plan["remove_ineligible"].append(eid)

    candidates = []
    for eid, info in eligible_episodes.items():
        if eid in activated:
            continue
        owned = owned_lookup(info["series_id"])
        if owned is None:
            plan["skipped_unknown"] += 1
            continue
        if (info["season"], info["episode"]) in owned:
            plan["skipped_owned"] += 1
            continue
        if failed_until.get(eid, 0) > now:
            plan["skipped_cooldown"] += 1
            continue
        candidates.append((info.get("added") or 0, info["series_id"], info["season"], info["episode"], eid))

    # Keep whole seasons together (newest series first) so a capped run
    # doesn't leave every show with a scattering of random episodes.
    candidates.sort(key=lambda c: (-c[0], c[1], c[2], c[3]))
    plan["add"] = [c[4] for c in candidates[:max_new]] if max_new > 0 else []
    plan["add_capped"] = max(0, len(candidates) - len(plan["add"]))
    return plan


class AutoSync:
    """Nightly/manual sync driver. One run at a time; state in auto_sync.json."""

    STATE_FILE = "auto_sync.json"
    FAILED_COOLDOWN_SECS = 7 * 86400
    # Series whose episode list Dispatcharr hasn't fetched yet, or fetched
    # longer ago than this, get re-fetched (one provider API call each, not a
    # stream connection) so new episodes of running shows show up.
    SERIES_REFRESH_AGE_SECS = 3 * 86400
    SERIES_REFRESH_DELAY_SECS = 1.0
    CAPACITY_POLL_SECS = 30
    PLAN_SAMPLE_SIZE = 50

    def __init__(self, bridge):
        self.bridge = bridge
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._progress = {"state": "idle"}
        self._state = {
            "last_run_date": None,
            "last_result": None,
            "last_dry_run": None,
            "failed_movies": {},
            "failed_episodes": {},
        }

    # --- settings -------------------------------------------------------

    def _s(self, key, default=None):
        value = self.bridge.settings.get(key, default)
        return default if value in (None, "") else value

    def _int(self, key, default):
        try:
            return int(self._s(key, default))
        except (TypeError, ValueError):
            return default

    def _bool(self, key, default):
        value = self._s(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def enabled(self):
        return self._bool("auto_sync_enabled", False)

    def radarr(self):
        return ArrClient("Radarr", self._s("radarr_url", ""), self._s("radarr_api_key", ""))

    def sonarr(self):
        return ArrClient("Sonarr", self._s("sonarr_url", ""), self._s("sonarr_api_key", ""))

    # --- persistence ------------------------------------------------------

    def _state_path(self):
        return os.path.join(self.bridge._data_dir, self.STATE_FILE)

    def load(self):
        try:
            with open(self._state_path(), "r") as f:
                loaded = json.load(f)
            with self._state_lock:
                self._state.update(loaded)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"Auto-sync state load failed: {e}")

    def _save(self):
        path = self._state_path()
        tmp = path + ".tmp"
        try:
            with self._state_lock:
                data = json.dumps(self._state)
            with open(tmp, "w") as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"Auto-sync state save failed: {e}")

    def _prune_cooldowns(self, now):
        with self._state_lock:
            for key in ("failed_movies", "failed_episodes"):
                self._state[key] = {k: v for k, v in self._state.get(key, {}).items() if v > now}

    def record_failures(self, kind, ids):
        if not ids:
            return
        until = time.time() + self.FAILED_COOLDOWN_SECS
        key = "failed_movies" if kind == "movie" else "failed_episodes"
        with self._state_lock:
            bucket = self._state.setdefault(key, {})
            for i in ids:
                bucket[str(i)] = until
        self._save()

    # --- status / control -------------------------------------------------

    def status(self):
        with self._state_lock:
            state = {
                "last_run_date": self._state.get("last_run_date"),
                "last_result": self._state.get("last_result"),
                "last_dry_run": self._state.get("last_dry_run"),
                "cooldown_movies": len(self._state.get("failed_movies", {})),
                "cooldown_episodes": len(self._state.get("failed_episodes", {})),
            }
        return {
            "enabled": self.enabled(),
            "running": self.is_running(),
            "progress": dict(self._progress),
            "schedule": self._s("auto_sync_time", "03:30"),
            "radarr_configured": self.radarr().configured(),
            "sonarr_configured": self.sonarr().configured(),
            **state,
        }

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, dry_run=False, trigger="manual"):
        if self.is_running():
            return {"status": "busy", "message": "A sync is already running"}
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_guarded, args=(dry_run, trigger),
            daemon=True, name="vod-bridge-auto-sync",
        )
        self._thread.start()
        return {"status": "started", "dry_run": dry_run}

    def request_stop(self):
        self._stop.set()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)

    # Removing titles because their VOD group is no longer enabled is the one
    # removal driven purely by Dispatcharr's own data. An empty or partial
    # catalog (provider refresh in progress, DB hiccup) would look exactly
    # like "every group was disabled", so a removal of more than this share
    # of the auto-synced titles is held back unless explicitly allowed.
    MASS_REMOVAL_FRACTION = 0.5
    MASS_REMOVAL_MIN = 25

    def _guard_mass_removal(self, plan, auto_count):
        n = len(plan["remove_ineligible"])
        if not n or self._bool("auto_sync_allow_mass_removal", False):
            return
        if plan["eligible"] == 0 or (n >= self.MASS_REMOVAL_MIN and n > auto_count * self.MASS_REMOVAL_FRACTION):
            logger.warning(
                f"Auto-sync: holding back removal of {n} of {auto_count} auto-synced titles whose "
                f"group looks disabled (eligible now: {plan['eligible']}). Enable "
                f"auto_sync_allow_mass_removal if this is intended."
            )
            plan["remove_ineligible_held_back"] = n
            plan["remove_ineligible"] = []

    def maybe_run_scheduled(self, now=None):
        """Called from the bridge watchdog (~every 10s). Starts the daily run
        once the configured local time has passed and it hasn't run today."""
        if not self.enabled() or self.is_running():
            return False
        now_dt = datetime.fromtimestamp(now or time.time())
        try:
            hh, mm = (int(x) for x in str(self._s("auto_sync_time", "03:30")).split(":", 1))
        except ValueError:
            hh, mm = 3, 30
        if (now_dt.hour, now_dt.minute) < (hh, mm):
            return False
        today = now_dt.strftime("%Y-%m-%d")
        with self._state_lock:
            if self._state.get("last_run_date") == today:
                return False
            # Claimed before the run starts: a run that crashes or finds
            # Radarr down must not be retried every 10 seconds all day.
            self._state["last_run_date"] = today
        self._save()
        self.start(dry_run=False, trigger="schedule")
        return True

    def _set_progress(self, **kwargs):
        self._progress = {**self._progress, **kwargs, "updated_at": time.time()}

    def _run_guarded(self, dry_run, trigger):
        if not self._run_lock.acquire(blocking=False):
            return
        started = time.time()
        self._progress = {"state": "planning", "dry_run": dry_run, "trigger": trigger,
                          "started_at": started, "updated_at": started}
        try:
            result = self._run(dry_run)
        except Exception as e:
            logger.exception("Auto-sync run failed")
            result = {"status": "error", "message": f"{type(e).__name__}: {e}"}
        result["dry_run"] = dry_run
        result["trigger"] = trigger
        result["started_at"] = started
        result["finished_at"] = time.time()
        with self._state_lock:
            self._state["last_dry_run" if dry_run else "last_result"] = result
        self._save()
        self._progress = {"state": "idle", "updated_at": time.time()}
        self._run_lock.release()
        self._log_summary(result)

    def _log_summary(self, result):
        label = "Auto-sync dry run" if result.get("dry_run") else "Auto-sync"
        if result.get("status") == "error":
            self.bridge._log_event("error", f"{label} failed: {result.get('message')}")
            return
        parts = []
        for kind in ("movies", "episodes"):
            section = result.get(kind) or {}
            if section.get("skipped"):
                parts.append(f"{kind}: skipped ({section['skipped']})")
                continue
            verb = "would add" if result.get("dry_run") else "added"
            parts.append(
                f"{kind}: {verb} {section.get('added', len(section.get('add', [])))}, "
                f"removed {section.get('removed', len(section.get('remove_owned', [])) + len(section.get('remove_ineligible', [])))}"
            )
        self.bridge._log_event("info", f"{label}: " + "; ".join(parts))

    # --- the run ------------------------------------------------------------

    def _run(self, dry_run):
        now = time.time()
        self._prune_cooldowns(now)
        self.bridge._refresh_settings_from_db()
        result = {"status": "ok"}
        result["movies"] = self._sync_movies(dry_run, now)
        if self._stop.is_set():
            result["status"] = "stopped"
            return result
        result["episodes"] = self._sync_episodes(dry_run, now)
        if self._stop.is_set():
            result["status"] = "stopped"
        return result

    def _sample(self, ids, names):
        return [names.get(i, f"#{i}") for i in ids[: self.PLAN_SAMPLE_SIZE]]

    def _sync_movies(self, dry_run, now):
        if not self._bool("auto_sync_movies", True):
            return {"skipped": "disabled"}
        radarr_client = self.radarr()
        if not radarr_client.configured():
            return {"skipped": "Radarr not configured"}

        self._set_progress(state="reading Radarr")
        try:
            radarr = RadarrIndex.fetch(
                radarr_client, count_missing_as_owned=self._bool("arr_count_missing_as_owned", False)
            )
        except ArrError as e:
            return {"skipped": f"Radarr unreachable: {e}"}

        self._set_progress(state="reading Dispatcharr movies")
        eligible = self.bridge._eligible_movies()
        activated_ids = self.bridge._movie_ids_for(list(self.bridge._activated.keys()))
        with self._state_lock:
            failed = dict(self._state.get("failed_movies", {}))
        plan = plan_movies(
            eligible, self.bridge._activated, radarr, failed, now,
            max_new=self._int("auto_sync_max_new_movies", 250),
            require_ids=self._bool("auto_sync_require_ids", True),
            dedupe_manual=self._bool("auto_sync_dedupe_manual", True),
            activated_ids=activated_ids,
        )
        self._guard_mass_removal(
            plan, sum(1 for e in self.bridge._activated.values() if e.get("source") == "auto")
        )
        names = {mid: info.get("name") for mid, info in eligible.items()}
        for mid, entry in self.bridge._activated.items():
            names.setdefault(mid, entry.get("strm_folder") or f"#{mid}")

        summary = {
            "radarr_movies": radarr.total,
            "radarr_with_file": radarr.with_file,
            "eligible": plan["eligible"],
            "skipped_owned": plan["skipped_owned"],
            "skipped_no_ids": plan["skipped_no_ids"],
            "skipped_cooldown": plan["skipped_cooldown"],
            "add_capped": plan["add_capped"],
            "add": self._sample(plan["add"], names),
            "remove_owned": self._sample(plan["remove_owned"], names),
            "remove_ineligible": self._sample(plan["remove_ineligible"], names),
            "add_count": len(plan["add"]),
            "remove_owned_count": len(plan["remove_owned"]),
            "remove_ineligible_count": len(plan["remove_ineligible"]),
            "remove_ineligible_held_back": plan.get("remove_ineligible_held_back", 0),
        }
        if dry_run:
            return summary

        removals = plan["remove_owned"] + plan["remove_ineligible"]
        removed = 0
        if removals:
            self._set_progress(state=f"removing {len(removals)} movie(s)")
            removed = self.bridge.deactivate_movies({"movie_ids": removals}).get("deactivated", 0)
        summary["removed"] = removed

        summary["added"] = 0
        summary["failed"] = 0
        if plan["add"] and not self._stop.is_set():
            outcome = self._activate_in_batches("movie", plan["add"])
            summary.update(outcome)
        return summary

    def _sync_episodes(self, dry_run, now):
        if not self._bool("auto_sync_series", True):
            return {"skipped": "disabled"}
        sonarr_client = self.sonarr()
        if not sonarr_client.configured():
            return {"skipped": "Sonarr not configured"}

        category = self.bridge._ensure_auto_series_category()
        if category is None:
            return {"skipped": "no Plex series library configured (plex_series_library_section)"}

        self._set_progress(state="reading Sonarr")
        try:
            sonarr = SonarrIndex.fetch(
                sonarr_client, count_missing_as_owned=self._bool("arr_count_missing_as_owned", False)
            )
        except ArrError as e:
            return {"skipped": f"Sonarr unreachable: {e}"}

        eligible_series = self.bridge._eligible_series()

        # Pull episode lists for series Dispatcharr hasn't fetched yet (or
        # fetched a while ago). Capped per run: each is a provider API call
        # (not a stream). Done in dry runs too -- it only updates
        # Dispatcharr's catalog, and without it a first dry run would report
        # zero episodes because nothing has been fetched yet.
        refresh_cap = self._int("auto_sync_max_series_refresh", 200)
        stale = self.bridge._series_needing_episode_refresh(
            list(eligible_series.keys()), self.SERIES_REFRESH_AGE_SECS
        )
        fetched = 0
        for i, sid in enumerate(stale[:refresh_cap]):
            if self._stop.is_set():
                break
            self._set_progress(state=f"fetching episode lists {i + 1}/{min(len(stale), refresh_cap)}")
            if self.bridge._refresh_series_episodes(sid):
                fetched += 1
            time.sleep(self.SERIES_REFRESH_DELAY_SECS)

        self._set_progress(state="reading Dispatcharr episodes")
        eligible = self.bridge._eligible_episodes(eligible_series)

        series_ids_for = {}  # dispatcharr series id -> sonarr series id (or None)

        def _sonarr_id(dispatcharr_sid):
            if dispatcharr_sid not in series_ids_for:
                info = eligible_series.get(dispatcharr_sid) or self.bridge._series_ids_for([dispatcharr_sid]).get(dispatcharr_sid) or {}
                series_ids_for[dispatcharr_sid] = sonarr.find_series_id(
                    info.get("tmdb"), info.get("imdb"), title=info.get("title"), year=info.get("year"))
            return series_ids_for[dispatcharr_sid]

        owned_cache = {}

        def owned_lookup(dispatcharr_sid):
            if dispatcharr_sid in owned_cache:
                return owned_cache[dispatcharr_sid]
            try:
                owned = sonarr.owned_episodes(_sonarr_id(dispatcharr_sid))
            except ArrError as e:
                logger.warning(f"Auto-sync: Sonarr episode fetch failed for series {dispatcharr_sid}: {e}")
                owned = None
            owned_cache[dispatcharr_sid] = owned
            return owned

        with self._state_lock:
            failed = dict(self._state.get("failed_episodes", {}))
        self._set_progress(state="matching episodes against Sonarr")
        plan = plan_episodes(
            eligible, self.bridge._episodes_activated, owned_lookup, failed, now,
            max_new=self._int("auto_sync_max_new_episodes", 500),
            dedupe_manual=self._bool("auto_sync_dedupe_manual", True),
        )
        self._guard_mass_removal(
            plan, sum(1 for e in self.bridge._episodes_activated.values() if e.get("source") == "auto")
        )

        names = {eid: info.get("label") for eid, info in eligible.items()}
        for eid, entry in self.bridge._episodes_activated.items():
            names.setdefault(eid, (
                f"{entry.get('series_name', '?')} "
                f"S{int(entry.get('season_number') or 0):02d}E{int(entry.get('episode_number') or 0):02d}"
            ))

        summary = {
            "sonarr_series": sonarr.total,
            "eligible_series": len(eligible_series),
            "episode_lists_fetched": fetched,
            "episode_lists_pending": max(0, len(stale) - fetched),
            "eligible": plan["eligible"],
            "skipped_owned": plan["skipped_owned"],
            "skipped_cooldown": plan["skipped_cooldown"],
            "skipped_unknown": plan["skipped_unknown"],
            "add_capped": plan["add_capped"],
            "add": self._sample(plan["add"], names),
            "remove_owned": self._sample(plan["remove_owned"], names),
            "remove_ineligible": self._sample(plan["remove_ineligible"], names),
            "add_count": len(plan["add"]),
            "remove_owned_count": len(plan["remove_owned"]),
            "remove_ineligible_count": len(plan["remove_ineligible"]),
            "remove_ineligible_held_back": plan.get("remove_ineligible_held_back", 0),
        }
        if dry_run:
            return summary

        removals = plan["remove_owned"] + plan["remove_ineligible"]
        removed = 0
        if removals:
            self._set_progress(state=f"removing {len(removals)} episode(s)")
            removed = self.bridge.deactivate_episodes({"episode_ids": removals}).get("deactivated", 0)
        summary["removed"] = removed

        summary["added"] = 0
        summary["failed"] = 0
        if plan["add"] and not self._stop.is_set():
            outcome = self._activate_in_batches("episode", plan["add"], category=category)
            summary.update(outcome)
        return summary

    # --- activation pacing ----------------------------------------------------

    def _wait_for_capacity(self, deadline):
        """Block until providers have more than the viewer reserve free.
        Returns False if the run window ends (or stop is requested) first."""
        while not self._stop.is_set():
            if self.bridge._sync_capacity_available():
                return True
            if time.time() >= deadline:
                return False
            self._set_progress(state="paused: waiting for free provider streams")
            self._stop.wait(self.CAPACITY_POLL_SECS)
        return False

    def _activate_in_batches(self, kind, ids, category=None):
        batch_size = max(1, self._int("auto_sync_batch_size", 25))
        delay = max(0, self._int("auto_sync_batch_delay_secs", 90))
        window = max(10, self._int("auto_sync_max_minutes", 240)) * 60
        deadline = time.time() + window

        added, failed_ids, stopped_reason = 0, [], None
        for start in range(0, len(ids), batch_size):
            if self._stop.is_set():
                stopped_reason = "stop requested"
                break
            if time.time() >= deadline:
                stopped_reason = "run window ended"
                break
            if not self._wait_for_capacity(deadline):
                stopped_reason = "no free provider streams before the run window ended"
                break

            batch = ids[start:start + batch_size]
            self._set_progress(state=f"adding {kind}s {start + 1}-{start + len(batch)} of {len(ids)}")
            if kind == "movie":
                result = self.bridge.activate_movies_auto(batch)
            else:
                result = self.bridge.activate_episodes_auto(batch, category)
            added += result.get("activated", 0)
            failed_ids.extend(result.get("failed_ids", []))

            if start + batch_size < len(ids):
                # Give Plex time to scan and analyze this batch before the
                # next one lands -- analysis is what opens provider streams.
                self._stop.wait(delay)

        self.record_failures(kind, failed_ids)
        out = {"added": added, "failed": len(failed_ids)}
        if stopped_reason:
            out["stopped_early"] = stopped_reason
        return out
