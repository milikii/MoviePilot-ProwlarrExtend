import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).parent / "fixtures"


def _plugin(plugin_modules):
    plugin = plugin_modules.prowlarr.ProwlarrExtend()
    plugin._host = "http://prowlarr"
    plugin._api_key = "secret"
    plugin._proxy = False
    plugin._enabled = True
    plugin._indexers = []
    plugin._selected_indexers = []
    plugin._indexer_catalog = []
    plugin._indexers_authoritative = False
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
        site={
            "id": 33,
            "name": "ProwlarrExtend-Example",
            "domain": "prowlarr-3.extend",
            "proxy": True,
            "pri": 7,
            "downloader": "qb",
            "ua": "test-ua",
            "cookie": "a=b",
        },
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
    assert result.site == 33
    assert result.site_name == "Example"
    assert result.site_proxy is True
    assert result.site_order == 7
    assert result.site_downloader == "qb"
    assert result.site_ua == "test-ua"
    assert result.site_cookie == "a=b"


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
    assert indexer["parser"] == "ProwlarrExtend"
    assert indexer["plugin"] == "ProwlarrExtend"
    assert indexer["search"]["paths"] == []


def test_managed_site_detection_uses_domain_and_parser(plugin_modules):
    plugin = _plugin(plugin_modules)
    assert plugin._is_managed_site({"domain": "prowlarr-9.extend", "name": "other"}) is True
    assert plugin._is_managed_site({"parser": "ProwlarrExtend", "name": "x"}) is True
    assert plugin._is_managed_site({"name": "ProwlarrExtend-Foo"}) is True
    assert plugin._is_managed_site({"name": "NormalSite", "domain": "example.com"}) is False


def test_connection_requires_config(plugin_modules):
    plugin = _plugin(plugin_modules)
    plugin._host = ""
    plugin._api_key = ""
    result = plugin.test_connection()
    assert result["success"] is False
    assert "未配置" in result["message"]


def test_indexer_config_filters_disabled_and_non_torrent(plugin_modules):
    plugin = _plugin(plugin_modules)
    payload = json.loads((FIXTURES / "prowlarr_indexers_sample.json").read_text(encoding="utf-8"))
    plugin._ProwlarrExtend__request_json = lambda _url: payload

    indexers = plugin._ProwlarrExtend__get_indexers_from_config()
    names = {item["name"] for item in indexers}

    assert names == {
        "ProwlarrExtend-EnabledTorrent",
        "ProwlarrExtend-PublicTorrent",
        "ProwlarrExtend-NamelessFallback",
    }
    public = {item["name"]: item["public"] for item in indexers}
    assert public["ProwlarrExtend-PublicTorrent"] is True
    assert public["ProwlarrExtend-EnabledTorrent"] is False
    assert plugin.get_indexers() and plugin._indexers_authoritative is True


def test_authoritative_snapshot_deletes_missing_managed_sites(plugin_modules, monkeypatch):
    module = plugin_modules.prowlarr
    plugin = _plugin(plugin_modules)
    plugin._indexers = [{
        "id": "ProwlarrExtend-3",
        "name": "ProwlarrExtend-current",
        "domain": "prowlarr-3.extend",
        "public": False,
        "url": "http://prowlarr/api/v1/indexer/3",
    }]
    plugin._indexers_authoritative = True
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
    unrelated = SimpleNamespace(id=99, domain="example.com")

    class Site:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def create(self, _db):
            return None

        @classmethod
        def get_by_domain(cls, _db, domain):
            if domain == current.domain:
                return current
            return None

        @classmethod
        def list_order_by_pri(cls, _db):
            return [current, missing, unrelated]

        @classmethod
        def delete(cls, _db, site_id):
            deleted.append(site_id)

    monkeypatch.setattr(module, "Site", Site)
    site_ids, removed = plugin._ProwlarrExtend__sync_site_records()

    assert site_ids == [3]
    assert deleted == [4]
    assert removed == [4]


def test_indexer_id_supports_new_and_legacy_domains(plugin_modules):
    plugin = _plugin(plugin_modules)
    get_id = plugin._ProwlarrExtend__get_indexer_id

    assert get_id({"domain": "prowlarr-15.extend"}) == "15"
    assert get_id({"domain": "https://prowlarr-15.extend/"}) == "15"
    assert get_id({"domain": "15.prowlarr.extend"}) == "15"
    assert get_id({"id": "ProwlarrExtend-7", "domain": ""}) == "7"
    assert get_id({"url": "http://prowlarr/api/v1/indexer/9", "domain": ""}) == "9"
    assert get_id({"domain": "example.com", "id": "x"}) == ""


def test_sync_search_sites_preserves_user_sites(plugin_modules):
    plugin = _plugin(plugin_modules)
    stored = []
    plugin.systemconfig = SimpleNamespace(
        get=lambda _key: ["user-site", 10, "ProwlarrExtend-stale", 3, 4],
        set=lambda _key, value: stored.append(value),
    )

    plugin._ProwlarrExtend__sync_search_sites(site_ids=[3, 5], removed_site_ids=[4])

    assert stored == [["user-site", 10, 3, 5]]


def test_empty_runtime_config_does_not_wipe_saved_credentials(plugin_modules):
    plugin = _plugin(plugin_modules)
    plugin._host = ""
    plugin._api_key = ""
    plugin._cron = "0 0 * * *"
    plugin._enabled = True
    plugin._proxy = False
    plugin._selected_indexers = ["3"]
    plugin._indexer_catalog = [{"id": "3", "name": "Example"}]
    updates = []
    plugin.get_config = lambda: {"host": "http://saved", "api_key": "saved-key"}
    plugin.update_config = updates.append

    plugin._ProwlarrExtend__update_config()

    assert updates[-1]["host"] == "http://saved"
    assert updates[-1]["api_key"] == "saved-key"
    assert updates[-1]["selected_indexers"] == ["3"]
    assert updates[-1]["indexer_catalog"] == [{"id": "3", "name": "Example"}]


def test_selected_indexers_filters_bridged_list(plugin_modules):
    plugin = _plugin(plugin_modules)
    payload = json.loads((FIXTURES / "prowlarr_indexers_sample.json").read_text(encoding="utf-8"))
    plugin._ProwlarrExtend__request_json = lambda _url: payload
    plugin._selected_indexers = ["1", "5"]

    indexers = plugin.get_indexers()
    ids = {plugin._ProwlarrExtend__get_indexer_id(item) for item in indexers}

    assert ids == {"1", "5"}
    assert plugin._indexers_authoritative is True
    catalog_ids = {item["id"] for item in plugin._indexer_catalog}
    assert {"1", "5", "6"}.issubset(catalog_ids)


def test_empty_selected_indexers_keeps_all(plugin_modules):
    plugin = _plugin(plugin_modules)
    payload = json.loads((FIXTURES / "prowlarr_indexers_sample.json").read_text(encoding="utf-8"))
    plugin._ProwlarrExtend__request_json = lambda _url: payload
    plugin._selected_indexers = []

    indexers = plugin.get_indexers()
    ids = {plugin._ProwlarrExtend__get_indexer_id(item) for item in indexers}
    assert ids == {"1", "5", "6"}


def test_normalize_selected_indexers_accepts_variants(plugin_modules):
    plugin = _plugin(plugin_modules)
    normalize = plugin._ProwlarrExtend__normalize_selected_indexers

    assert normalize(None) == []
    assert normalize("1, 5，prowlarr-9.extend") == ["1", "5", "9"]
    assert normalize(["ProwlarrExtend-3", 3, "x", "7"]) == ["3", "7"]


def test_catalog_survives_form_save_without_catalog_field(plugin_modules):
    plugin = _plugin(plugin_modules)
    saved = {
        "host": "http://saved",
        "api_key": "saved-key",
        "selected_indexers": ["1"],
        "indexer_catalog": [{"id": "1", "name": "Alpha"}, {"id": "2", "name": "Beta"}],
    }
    plugin.get_config = lambda: saved
    plugin.sync_indexers = lambda: True

    # Simulate UI save that only posts visible form models.
    plugin.init_plugin({
        "host": "http://prowlarr",
        "api_key": "secret",
        "enabled": True,
        "selected_indexers": ["1", "2"],
    })

    assert plugin._selected_indexers == ["1", "2"]
    assert plugin._indexer_catalog == [
        {"id": "1", "name": "Alpha"},
        {"id": "2", "name": "Beta"},
    ]


def test_persist_indexer_metadata_writes_catalog(plugin_modules):
    plugin = _plugin(plugin_modules)
    updates = []
    plugin.get_config = lambda: {"host": "http://prowlarr", "api_key": "secret"}
    plugin.update_config = updates.append
    plugin._indexer_catalog = [{"id": "1", "name": "Alpha"}]
    plugin._selected_indexers = ["1"]
    plugin._cron = "0 0 * * *"

    plugin._ProwlarrExtend__persist_indexer_metadata()

    assert updates
    assert updates[-1]["indexer_catalog"] == [{"id": "1", "name": "Alpha"}]
    assert updates[-1]["selected_indexers"] == ["1"]


def test_form_exposes_indexer_multi_select(plugin_modules):
    plugin = _plugin(plugin_modules)
    plugin._indexer_catalog = [
        {"id": "1", "name": "Alpha"},
        {"id": "2", "name": "Beta"},
    ]
    form, defaults = plugin.get_form()

    assert defaults["selected_indexers"] == []
    assert "indexer_catalog" in defaults

    def find_select(node):
        if isinstance(node, dict):
            if node.get("component") == "VSelect" and node.get("props", {}).get("model") == "selected_indexers":
                return node
            for child in node.get("content") or []:
                found = find_select(child)
                if found:
                    return found
        elif isinstance(node, list):
            for child in node:
                found = find_select(child)
                if found:
                    return found
        return None

    select = find_select(form)
    assert select is not None
    assert select["props"]["multiple"] is True
    assert select["props"]["items"] == [
        {"title": "Alpha (#1)", "value": "1"},
        {"title": "Beta (#2)", "value": "2"},
    ]
