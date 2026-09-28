# Run with: docker exec -i dispatcharr-test python /app/manage.py shell < setup_dispatcharr.py
from apps.m3u.models import M3UAccount
from apps.plugins.models import PluginConfig
from apps.vod.models import M3UVODCategoryRelation, Movie, Series
from apps.vod.tasks import refresh_vod_content

acc, created = M3UAccount.objects.get_or_create(
    name="MockTV",
    defaults=dict(
        server_url="http://mock-services:9500", username="u", password="p",
        account_type="XC", max_streams=2, is_active=True,
    ),
)
print("account", acc.id, "created" if created else "existing")
print(refresh_vod_content(acc.id))

M3UVODCategoryRelation.objects.filter(
    m3u_account=acc, category__name__in=["EN Films", "EN Series"]
).update(enabled=False)
for r in M3UVODCategoryRelation.objects.filter(m3u_account=acc).select_related("category").order_by("category__name"):
    print("group", r.category.category_type, r.category.name, "enabled" if r.enabled else "disabled")
print("movies", sorted(Movie.objects.values_list("id", "name", "tmdb_id")))
print("series", sorted(Series.objects.values_list("id", "name", "tmdb_id")))

settings = {
    "dispatcharr_url": "http://dispatcharr-test:9191",
    "http_port": 8888,
    "dashboard_host": "dispatcharr-test",
    "auto_start_server": True,
    "plex_url": "http://mock-services:9700",
    "plex_token": "plextoken",
    "plex_library_section": 1,
    "plex_series_library_section": 2,
    "plex_vod_movies_path": "/mnt/vod/movies",
    "plex_vod_series_path": "/mnt/vod/series",
    "strm_output_dir": "/data/plugin-strm",
    "radarr_url": "http://mock-services:9600",
    "radarr_api_key": "testkey",
    "sonarr_url": "http://mock-services:9601",
    "sonarr_api_key": "testkey",
    "auto_sync_enabled": False,
    "auto_sync_batch_delay_secs": 0,
    "reserve_streams_for_viewing": 1,
    "untracked_orphan_dry_run": True,
}
cfg, _ = PluginConfig.objects.get_or_create(key="vod_plex_bridge", defaults={"name": "VOD To Plex"})
cfg.enabled = True
cfg.ever_enabled = True
cfg.settings = settings
cfg.save()
print("plugin config saved", cfg.key, cfg.enabled)
