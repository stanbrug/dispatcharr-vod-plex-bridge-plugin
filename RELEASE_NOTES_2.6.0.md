# VOD To Plex 2.6.0

## New: auto-sync with Radarr / Sonarr

- Mirrors every movie and series from the VOD groups (categories) enabled on
  active M3U accounts in Dispatcharr, nightly at a configurable time or on
  demand (dashboard *Auto-sync* tab, or the plugin's *Dry run* / *Sync now*
  actions).
- A movie Radarr has a file for, or an episode Sonarr has a file for, is not
  added; when Radarr/Sonarr later get the file, the VOD copy is removed.
  Titles Radarr/Sonarr know but have no file for keep their VOD copy as a
  fallback (configurable). Matching is by TMDB id, then IMDb id.
- Series episode lists are fetched from the provider as needed (capped per run),
  new series first, then running shows.
- Capped batches with a pause between them, a maximum run time, a 7-day retry
  cooldown for titles that fail, and a safety valve that holds back removing
  most auto-synced titles at once when the catalog suddenly looks empty.
- Nothing is removed based on a failed Radarr/Sonarr fetch.

## Provider limits (max streams)

- Capacity is judged over every active profile of an account (Dispatcharr
  rotates across them), including ServerGroup credential counters.
- *Streams kept free for viewers*: background work (auto-sync batches,
  scheduled audio checks, Plex's first analysis of freshly auto-synced titles)
  never takes the last streams; auto-sync pauses while viewers need them.
- Only relations from enabled VOD groups on active accounts are used, highest
  account VOD priority first.
- Fix: the least-loaded-provider pick imported a function current Dispatcharr
  doesn't have, so every account always looked idle.
- The weekly stream refresh no longer audio-probes auto-synced titles nobody
  has played, and skips probes while the provider is near its limit.

## Shared Plex libraries

- All Plex removals only touch VOD media: when an item also holds a
  Radarr/Sonarr version, only the VOD version is deleted
  (`/library/metadata/{id}/media/{mediaId}`), never the item. Requires the new
  *Plex path of the VOD movies/series mount* settings; without them such items
  are left alone.
- Untracked-orphan cleanup of shows works per episode instead of deleting the
  whole show.
- Confirmed-size reconciliation reads the VOD part, not whichever version
  happens to be first.
- Scans are limited to the VOD folder (`?path=`), not the whole section.
- Movie filenames and show folders carry Plex `{tmdb-N}` hints.
- Bridge sessions are recognized by the configured VOD paths (was a hardcoded
  `vod-plugin` path).

## Other

- Optional access token for the dashboard/API and rclone endpoints.
- Plugin settings are re-read from Dispatcharr every minute (no restart needed,
  except for port/host).
- `/vod/` listing uses one query instead of one per movie; state saves are
  serialized and only rewrite sidecars that changed; redirects no longer save
  the full state on every request.
- Fix: every successful removal was reported in Needs Attention as
  "removal failed" (folder delete helpers returned None).
