from datetime import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse


def _plugin(plugin_modules):
    plugin = plugin_modules.prowlarr.ProwlarrExtend()
    plugin._host = "http://prowlarr"
    plugin._api_key = "secret"
    plugin._proxy = False
    plugin._enabled = True
    plugin._indexers = []
    return plugin


def test_legacy_cron_is_migrated(plugin_modules):
    plugin = _plugin(plugin_modules)
    updates = []
    plugin.sync_indexers = lambda: True
    plugin.get_config = lambda: {}
    plugin.update_config = updates.append

    plugin.init_plugin({
        "host": "http://prowlarr",
        "api_key": "secret",
        "enabled": True,
        "cron": "0 0 */24 * *",
    })

    assert plugin._cron == "0 0 * * *"
    assert updates[-1]["cron"] == "0 0 * * *"


def test_search_maps_time_flags_and_uses_real_page_size(plugin_modules, monkeypatch):
    module = plugin_modules.prowlarr
    plugin = _plugin(plugin_modules)
    requested_urls = []

    class Response:
        def __bool__(self):
            return True

        @staticmethod
        def json():
            return [{
                "title": "Example",
                "downloadUrl": "https://example.invalid/download",
                "publishDate": "2026-07-10T05:00:00Z",
                "indexerFlags": ["freeleech", "doubleupload"],
                "imdbId": 1234567,
                "categories": [{"id": 2000, "name": "Movies"}],
            }]

    class RequestUtils:
        def __init__(self, *args, **kwargs):
            pass

        def get_res(self, url):
            requested_urls.append(url)
            return Response()

    monkeypatch.setattr(module, "RequestUtils", RequestUtils)
    results = plugin.search_torrents(
        site={"name": "ProwlarrExtend-Example", "domain": "prowlarr-3.extend"},
        keyword="movie",
        page=1,
    )

    query = parse_qs(urlparse(requested_urls[0]).query)
    assert query["limit"] == ["100"]
    assert query["offset"] == ["100"]
    assert len(results) == 1
    result = results[0]
    assert "T" not in result.pubdate and not result.pubdate.endswith("Z")
    datetime.strptime(result.pubdate, "%Y-%m-%d %H:%M:%S")
    assert result.downloadvolumefactor == 0.0
    assert result.uploadvolumefactor == 2.0
    assert result.imdbid == "tt1234567"


def test_explicit_volume_factors_take_priority(plugin_modules):
    plugin_class = plugin_modules.prowlarr.ProwlarrExtend
    assert plugin_class._volume_factors({
        "downloadVolumeFactor": 0.3,
        "uploadVolumeFactor": 4,
        "indexerFlags": ["freeleech"],
    }) == (0.3, 4.0)
    assert plugin_class._volume_factors({"indexerFlags": ["halfleech"]}) == (0.5, 1.0)
    assert plugin_class._volume_factors({"indexerFlags": ["neutralleech"]}) == (0.0, 0.0)


def test_stats_fallback_is_marked_non_authoritative(plugin_modules):
    plugin = _plugin(plugin_modules)
    plugin._ProwlarrExtend__get_indexers_from_config = lambda: None
    plugin._ProwlarrExtend__get_indexers_from_stats = lambda: [{"domain": "prowlarr-3.extend"}]

    assert plugin.get_indexers() == [{"domain": "prowlarr-3.extend"}]
    assert plugin._indexers_authoritative is False


def test_non_authoritative_snapshot_does_not_delete_missing_sites(plugin_modules, monkeypatch):
    module = plugin_modules.prowlarr
    plugin = _plugin(plugin_modules)
    plugin._indexers = [{
        "id": "ProwlarrExtend-3",
        "name": "ProwlarrExtend-current",
        "domain": "prowlarr-3.extend",
        "public": False,
        "url": "http://prowlarr/api/v1/indexer/3",
    }]
    plugin._indexers_authoritative = False
    deleted = []

    current = SimpleNamespace(
        id=3,
        name="ProwlarrExtend-current",
        domain="prowlarr-3.extend",
        url="https://prowlarr-3.extend/",
        pri=0,
        public=0,
        proxy=0,
        render=0,
        timeout=15,
        is_active=True,
        update=lambda *_: None,
    )
    missing = SimpleNamespace(id=4, domain="prowlarr-4.extend")

    class Site:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def create(self, _db):
            return None

        @classmethod
        def get_by_domain(cls, _db, domain):
            return current if domain == current.domain else None

        @classmethod
        def list_order_by_pri(cls, _db):
            return [current, missing]

        @classmethod
        def delete(cls, _db, site_id):
            deleted.append(site_id)

    monkeypatch.setattr(module, "Site", Site)
    plugin._ProwlarrExtend__sync_site_records()

    assert deleted == []


def test_private_indexer_is_not_registered_as_public(plugin_modules):
    plugin = _plugin(plugin_modules)
    indexer = plugin._ProwlarrExtend__build_indexer(3, "Private", privacy="private")
    assert indexer["public"] is False
