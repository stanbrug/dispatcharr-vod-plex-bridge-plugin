# End-to-end test

Real Dispatcharr (Docker) + `mock_services.py` (fake Xtream Codes provider,
Radarr, Sonarr and Plex) on an internal Docker network named `vodtest`.

```bash
docker network create --internal vodtest
docker run -d --name mock-services --network vodtest -v "$PWD/tests/integration:/t:ro" \
  python:3.12-slim python -u /t/mock_services.py
docker run -d --name dispatcharr-test --network vodtest \
  -e DISPATCHARR_ENV=aio -e REDIS_HOST=localhost -e CELERY_BROKER_URL=redis://localhost:6379/0 \
  -v "$PWD:/data/plugins/vod_plex_bridge" ghcr.io/dispatcharr/dispatcharr:latest
# once Dispatcharr has migrated (retry if Redis isn't up yet):
docker exec -i dispatcharr-test python /app/manage.py shell < tests/integration/setup_dispatcharr.py
docker restart dispatcharr-test          # plugin auto-starts with the saved settings
for step in dry sync plex dedupe redirect; do
  docker exec mock-services python -u /t/harness.py $step
done
```

Expected:

- **dry / sync**: movies from the NL group only (EN group disabled, adult
  group hidden); *Soldaat van Oranje* skipped (Radarr has the file), the
  movie without ids skipped, *Zwartboek* + *Oorlogswinter* added. Episodes:
  *Flikken Maastricht* S01E01 skipped (Sonarr has it), 4 others added. Plex
  refreshes are limited to `/mnt/vod/movies` and `/mnt/vod/series/auto`.
- **dedupe**: after Radarr/Sonarr "download" *Oorlogswinter* and *Flikken*
  S01E02, the only Plex calls are
  `DELETE /library/metadata/101/media/1001` and
  `DELETE /library/metadata/301/media/2001` — the VOD versions; the merged
  Radarr/Sonarr versions and the Radarr/Sonarr-only items are untouched.
- **redirect**: 302 to Dispatcharr's `/proxy/vod/movie/...`.
- Disabling every enabled group afterwards: the removal is *held back* by the
  mass-removal safety valve. Setting *Streams kept free for viewers* to the
  account's max streams: a fresh auto-synced title answers 503 (analysis
  deferred).
