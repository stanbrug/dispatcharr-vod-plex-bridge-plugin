"""Unit tests for the pure parts of the auto-sync and the Plex safety logic.

Run from the repository root:  python -m unittest discover -s tests -v
No Dispatcharr/Django needed: everything here avoids the ORM.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arr_sync  # noqa: E402
import bridge  # noqa: E402
from arr_sync import RadarrIndex, SonarrIndex, norm_imdb, norm_tmdb, plan_episodes, plan_movies  # noqa: E402


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        key = path if not params else (path, tuple(sorted(params.items())))
        if key not in self.routes:
            raise arr_sync.ArrError(f"no route {key}")
        return self.routes[key]


class NormalizeTests(unittest.TestCase):
    def test_tmdb(self):
        self.assertEqual(norm_tmdb(603), "603")
        self.assertEqual(norm_tmdb(" 603 "), "603")
        self.assertIsNone(norm_tmdb(0))
        self.assertIsNone(norm_tmdb("0"))
        self.assertIsNone(norm_tmdb(""))
        self.assertIsNone(norm_tmdb(None))
        self.assertIsNone(norm_tmdb("tt123"))

    def test_imdb(self):
        self.assertEqual(norm_imdb("TT0133093"), "tt0133093")
        self.assertIsNone(norm_imdb("0133093"))
        self.assertIsNone(norm_imdb(None))


class RadarrIndexTests(unittest.TestCase):
    movies = [
        {"tmdbId": 603, "imdbId": "tt0133093", "hasFile": True},
        {"tmdbId": 604, "imdbId": "tt0234215", "hasFile": False},
        {"tmdbId": 0, "imdbId": "tt9999999", "hasFile": True},
    ]

    def test_owned_requires_file(self):
        idx = RadarrIndex(self.movies)
        self.assertTrue(idx.owns("603", None))
        self.assertTrue(idx.owns(None, "tt0133093"))
        self.assertFalse(idx.owns("604", "tt0234215"))  # monitored, no file yet
        self.assertTrue(idx.owns(None, "TT9999999"))  # imdb-only match
        self.assertFalse(idx.owns(None, None))
        self.assertEqual(idx.with_file, 2)

    def test_count_missing_as_owned(self):
        idx = RadarrIndex(self.movies, count_missing_as_owned=True)
        self.assertTrue(idx.owns("604", None))


class SonarrIndexTests(unittest.TestCase):
    def make(self, missing=False):
        series = [
            {"id": 1, "tmdbId": 1399, "imdbId": "tt0944947", "statistics": {"episodeFileCount": 2}},
            {"id": 2, "tmdbId": 555, "imdbId": None, "statistics": {"episodeFileCount": 0}},
        ]
        client = FakeClient({
            ("episode", (("seriesId", 1),)): [
                {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True},
                {"seasonNumber": 1, "episodeNumber": 2, "hasFile": True},
                {"seasonNumber": 1, "episodeNumber": 3, "hasFile": False},
            ],
            ("episode", (("seriesId", 2),)): [
                {"seasonNumber": 1, "episodeNumber": 1, "hasFile": False},
            ],
        })
        return SonarrIndex(client, series, count_missing_as_owned=missing), client

    def test_match_and_owned(self):
        idx, client = self.make()
        self.assertEqual(idx.find_series_id("1399", None), 1)
        self.assertEqual(idx.find_series_id(None, "tt0944947"), 1)
        self.assertIsNone(idx.find_series_id("42", None))
        self.assertEqual(idx.owned_episodes(1), {(1, 1), (1, 2)})
        # Cached: a second call doesn't hit the API again.
        idx.owned_episodes(1)
        self.assertEqual(len(client.calls), 1)

    def test_no_files_skips_fetch(self):
        idx, client = self.make()
        self.assertEqual(idx.owned_episodes(2), set())
        self.assertEqual(client.calls, [])

    def test_title_year_fallback(self):
        series = [
            {"id": 10, "title": "Expeditie Robinson (NL)", "year": 2000, "tmdbId": 0},
            {"id": 11, "title": "The Office", "year": 2005, "tmdbId": 2996},
            {"id": 12, "title": "ONE PIECE (2023)", "year": 2023, "tmdbId": 111110},
            {"id": 13, "title": "Married at First Sight (NL)", "year": 2016, "tmdbId": 627},
            {"id": 14, "title": "vtwonen weer verliefd op je huis", "year": 2019, "tmdbId": 0},
            {"id": 15, "title": "Taboo (2017)", "year": 2017, "tmdbId": 0,
             "alternateTitles": [{"title": "Taboo UK"}]},
        ]
        idx = SonarrIndex(FakeClient({}), series)
        self.assertEqual(idx.find_series_id("8308", None, title="Expeditie Robinson", year=2000), 10)
        self.assertEqual(idx.find_series_id(None, None, title="Expeditie Robinson (NL)", year=2001), 10)
        self.assertIsNone(idx.find_series_id("256480", None, title="The Office (MULTI)", year=2024))
        self.assertIsNone(idx.find_series_id("37854", None, title="One Piece", year=1999))
        self.assertEqual(idx.find_series_id(None, None, title="Married At First Sight", year=None), 13)
        self.assertEqual(idx.find_series_id("215154", None, title="vtwonen: weer verliefd op je huis", year=2019), 14)
        self.assertEqual(idx.find_series_id(None, None, title="Taboo", year=2017), 15)
        self.assertIsNone(idx.find_series_id(None, None, title="Something Else", year=2017))
        # A real id match still wins over the title.
        self.assertEqual(idx.find_series_id("2996", None, title="Other", year=1990), 11)

    def test_fetch_failure_raises(self):
        idx = SonarrIndex(FakeClient({}), [{"id": 7, "tmdbId": 1, "statistics": {"episodeFileCount": 3}}])
        with self.assertRaises(arr_sync.ArrError):
            idx.owned_episodes(7)


class PlanMoviesTests(unittest.TestCase):
    def radarr(self):
        return RadarrIndex([{"tmdbId": 10, "hasFile": True}, {"tmdbId": 11, "hasFile": False}])

    def test_plan(self):
        eligible = {
            "1": {"tmdb": "10", "imdb": None, "name": "Owned", "added": 5},
            "2": {"tmdb": "11", "imdb": None, "name": "Wanted, no file", "added": 9},
            "3": {"tmdb": "12", "imdb": None, "name": "New", "added": 7},
            "4": {"tmdb": None, "imdb": None, "name": "No ids", "added": 8},
            "5": {"tmdb": "13", "imdb": None, "name": "Cooling down", "added": 6},
            "6": {"tmdb": "14", "imdb": None, "name": "Already active", "added": 1},
        }
        activated = {
            "6": {"source": "auto"},
            "7": {"source": "auto"},     # group disabled since
            "8": {"source": "auto"},     # Radarr got the file
            "9": {"source": "manual"},   # manual, Radarr has it
            "10": {"source": "manual"},  # manual, group disabled: kept
        }
        activated_ids = {
            "6": {"tmdb": "14"}, "7": {"tmdb": "20"}, "8": {"tmdb": "10"},
            "9": {"tmdb": "10"}, "10": {"tmdb": "21"},
        }
        plan = plan_movies(eligible, activated, self.radarr(), {"5": 2000}, now=1000,
                           max_new=10, activated_ids=activated_ids)
        # Newest first, owned/no-ids/cooldown/active excluded.
        self.assertEqual(plan["add"], ["2", "3"])
        self.assertEqual(sorted(plan["remove_owned"]), ["8", "9"])
        self.assertEqual(plan["remove_ineligible"], ["7"])
        self.assertEqual(plan["skipped_owned"], 1)
        self.assertEqual(plan["skipped_no_ids"], 1)
        self.assertEqual(plan["skipped_cooldown"], 1)

    def test_manual_kept_without_dedupe(self):
        plan = plan_movies({}, {"9": {"source": "manual"}}, self.radarr(), {}, now=0, max_new=10,
                           dedupe_manual=False, activated_ids={"9": {"tmdb": "10"}})
        self.assertEqual(plan["remove_owned"], [])

    def test_cap(self):
        eligible = {str(i): {"tmdb": str(100 + i), "added": i} for i in range(10)}
        plan = plan_movies(eligible, {}, self.radarr(), {}, now=0, max_new=3)
        self.assertEqual(plan["add"], ["9", "8", "7"])
        self.assertEqual(plan["add_capped"], 7)


class PlanEpisodesTests(unittest.TestCase):
    def test_plan(self):
        eligible = {
            "e1": {"series_id": "s1", "season": 1, "episode": 1, "added": 5},
            "e2": {"series_id": "s1", "season": 1, "episode": 2, "added": 5},
            "e3": {"series_id": "s2", "season": 1, "episode": 1, "added": 9},
            "e4": {"series_id": "s3", "season": 1, "episode": 1, "added": 1},
        }
        owned = {"s1": {(1, 1)}, "s2": set(), "s3": None}  # s3: Sonarr fetch failed
        activated = {
            "a1": {"source": "auto", "series_id": "s1", "season_number": 1, "episode_number": 1},
            "a2": {"source": "auto", "series_id": "s9", "season_number": 2, "episode_number": 3},
            "a3": {"source": "auto", "series_id": "s3", "season_number": 1, "episode_number": 1},
        }
        plan = plan_episodes(eligible, activated, lambda sid: owned.get(sid, set()), {}, now=0, max_new=10)
        self.assertEqual(plan["add"], ["e3", "e2"])  # newest series first; e1 owned; e4 unknown
        self.assertEqual(plan["remove_owned"], ["a1"])
        # Neither a2 nor a3 is in the eligible set (their groups are no
        # longer enabled), so both go -- an unknown Sonarr state only
        # prevents an "owned" removal, not an "ineligible" one.
        self.assertEqual(sorted(plan["remove_ineligible"]), ["a2", "a3"])
        self.assertEqual(plan["skipped_owned"], 1)
        self.assertEqual(plan["skipped_unknown"], 1)

    def test_unknown_sonarr_never_removes_as_owned(self):
        activated = {"a": {"source": "auto", "series_id": "s", "season_number": 1, "episode_number": 1}}
        eligible = {"a": {"series_id": "s", "season": 1, "episode": 1}}
        plan = plan_episodes(eligible, activated, lambda sid: None, {}, now=0, max_new=10)
        self.assertEqual(plan["remove_owned"], [])
        self.assertEqual(plan["remove_ineligible"], [])


class PlexSafetyTests(unittest.TestCase):
    def core(self, **settings):
        return bridge.BridgeCore(dict(settings))

    @staticmethod
    def item(key, *files, title="T"):
        return {
            "ratingKey": key,
            "title": title,
            "Media": [{"id": 100 + i, "Part": [{"file": f}]} for i, f in enumerate(files)],
        }

    def test_vod_only_item_deleted_whole(self):
        core = self.core(plex_vod_movies_path="/mnt/vod/movies")
        items = [self.item("1", "/mnt/vod/movies/Heat (1995) {tmdb-949} [12].mkv")]
        dels = core._plex_vod_deletions(items, "movie", lambda it, f: bridge._vod_file_id(f) == "12")
        self.assertEqual(dels, [("/library/metadata/1", "T")])

    def test_shared_item_only_vod_version_deleted(self):
        core = self.core(plex_vod_movies_path="/mnt/vod/movies/")
        items = [self.item("1", "/mnt/debrid/movies/Heat (1995)/Heat.mkv",
                           "/mnt/vod/movies/Heat (1995) [12].mkv")]
        dels = core._plex_vod_deletions(items, "movie", lambda it, f: True)
        self.assertEqual(dels, [("/library/metadata/1/media/101", "T (VOD version)")])

    def test_radarr_file_never_touched(self):
        core = self.core(plex_vod_movies_path="/mnt/vod/movies")
        # A Radarr file that happens to end in [12].mkv, outside the VOD path.
        items = [self.item("1", "/mnt/debrid/movies/Heat [12].mkv")]
        self.assertEqual(core._plex_vod_deletions(items, "movie", lambda it, f: True), [])

    def test_prefix_is_a_folder_boundary(self):
        core = self.core(plex_vod_movies_path="/mnt/vod")
        items = [self.item("1", "/mnt/vod-other/Heat [12].mkv")]
        self.assertEqual(core._plex_vod_deletions(items, "movie", lambda it, f: True), [])

    def test_no_prefix_shared_item_left_alone(self):
        core = self.core()
        items = [self.item("1", "/data/radarr/Heat.mkv", "/vod/Heat (1995) [12].mkv")]
        self.assertEqual(core._plex_vod_deletions(items, "movie", lambda it, f: True), [])

    def test_no_prefix_vod_only_item_still_deletable(self):
        core = self.core()
        items = [self.item("1", "/vod/Heat (1995) [12].mkv")]
        dels = core._plex_vod_deletions(items, "movie", lambda it, f: True)
        self.assertEqual(dels, [("/library/metadata/1", "T")])

    def test_unwanted_vod_media_kept(self):
        core = self.core(plex_vod_movies_path="/mnt/vod/movies")
        items = [self.item("1", "/mnt/vod/movies/A [12].mkv", "/mnt/vod/movies/A [13].mkv")]
        dels = core._plex_vod_deletions(items, "movie", lambda it, f: bridge._vod_file_id(f) == "13")
        self.assertEqual(dels, [("/library/metadata/1/media/101", "T (VOD version)")])

    def test_windows_style_paths(self):
        core = self.core(plex_vod_movies_path="D:\\vod\\movies")
        items = [self.item("1", "D:\\vod\\movies\\A [12].mkv")]
        self.assertEqual(len(core._plex_vod_deletions(items, "movie", lambda it, f: True)), 1)


class NamingTests(unittest.TestCase):
    def test_movie_listing_name(self):
        core = bridge.BridgeCore({})
        self.assertEqual(core._movie_listing_name(12, "EN - Heat", 1995, "949"), "Heat (1995) {tmdb-949} [12].mkv")
        self.assertEqual(core._movie_listing_name(12, "Heat", None, None), "Heat [12].mkv")
        self.assertEqual(bridge._vod_file_id("Heat (1995) {tmdb-949} [12].mkv"), "12")
        core = bridge.BridgeCore({"plex_id_hints": False})
        self.assertEqual(core._movie_listing_name(12, "Heat", 1995, "949"), "Heat (1995) [12].mkv")

    def test_clean_title_strips_hint(self):
        core = bridge.BridgeCore({})
        self.assertEqual(core._clean_title("Heat (1995) {tmdb-949}"), "Heat")
        self.assertEqual(core._clean_title("Our Girl (GB)"), "Our Girl")

    def test_vod_file_id(self):
        self.assertEqual(bridge._vod_file_id("/x/Show - S01E02 - Pilot [987].mkv"), "987")
        self.assertEqual(bridge._vod_file_id("/x/123.mp4"), "123")
        self.assertIsNone(bridge._vod_file_id("/x/Show.S01E02.mkv"))


class AuthTests(unittest.TestCase):
    def test_token(self):
        import base64
        import server

        settings = {"access_token": "s3cret"}
        remote = {"REMOTE_ADDR": "10.0.0.5"}
        self.assertFalse(server._authorized(dict(remote), settings))
        self.assertTrue(server._authorized(dict(remote, HTTP_X_BRIDGE_TOKEN="s3cret"), settings))
        self.assertFalse(server._authorized(dict(remote, HTTP_X_BRIDGE_TOKEN="nope"), settings))
        basic = "Basic " + base64.b64encode(b"rclone:s3cret").decode()
        self.assertTrue(server._authorized(dict(remote, HTTP_AUTHORIZATION=basic), settings))
        self.assertTrue(server._authorized(dict(remote, HTTP_COOKIE="vodbridge_token=s3cret"), settings))
        self.assertTrue(server._authorized({"REMOTE_ADDR": "127.0.0.1"}, settings))
        self.assertTrue(server._authorized(dict(remote), {}))


if __name__ == "__main__":
    unittest.main()
