# 替身单测，非宿主契约测试。
"""v3 行为回归；pytest 可收集，也可在离线环境直接用 unittest 运行。"""

import copy
import importlib.util
import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import requests


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "plugins.v3" / "prowlarrextend"


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _PluginBase:
    def get_config(self, *args, **kwargs):
        return {}

    def update_config(self, *args, **kwargs):
        return None


class _TorrentInfo:
    # 与 conftest 一样仅接收字段，不模拟真实宿主的 dataclass 校验。
    # 真实契约是 app.domain.context.TorrentInfo（带 to_dict），不是 schemas 版。
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _MediaSource:
    IMDb = "imdb"
    TMDB = "themoviedb"


def _import_stubs():
    """沿用 conftest 的 ModuleType/SimpleNamespace 替身风格，局部安装。"""
    modules = {}

    def module(name, **attrs):
        stub = types.ModuleType(name)
        stub.__dict__.update(attrs)
        modules[name] = stub

    class CronTrigger:
        @classmethod
        def from_crontab(cls, expression):
            return expression

    async def run_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    module("apscheduler")
    module("apscheduler.triggers")
    module("apscheduler.triggers.cron", CronTrigger=CronTrigger)
    module("fastapi")
    module("fastapi.concurrency", run_in_threadpool=run_in_threadpool)
    module("app")
    module("app.core")
    module("app.core.config", settings=types.SimpleNamespace(USER_AGENT="unittest", PROXY=None))
    module("app.log", logger=_Logger())
    module("app.plugins", _PluginBase=_PluginBase)
    module("app.domain")
    module("app.domain.context", TorrentInfo=_TorrentInfo)
    module("app.schemas")
    module("app.schemas.types", MediaSource=_MediaSource)
    return modules


class _Response:
    """模拟 requests.Response：4xx/5xx 在布尔判断中为假。"""
    def __init__(self, payload, status_code=200, headers=None, json_error=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(payload, ensure_ascii=False)
        self.json_error = json_error

    def __bool__(self):
        return self.status_code < 400

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return copy.deepcopy(self.payload)


class ProwlarrExtendV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 用别名加载 v3，避免与 conftest 的 v2 prowlarrextend 包冲突。
        imports = patch.dict(sys.modules, _import_stubs())
        imports.start()
        cls.addClassCleanup(imports.stop)
        spec = importlib.util.spec_from_file_location(
            "_prowlarrextend_v3_test",
            PLUGIN_PATH / "__init__.py",
            submodule_search_locations=[str(PLUGIN_PATH)],
        )
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.calls = []
        self.responses = {}
        test = self

        class Session:
            def __init__(self):
                self.trust_env = True
                self.proxies = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, **kwargs):
                path = url.split("/api/v1/", 1)[1]
                test.calls.append({"path": path, **kwargs})
                return test.responses[path]

        session_patch = patch.object(requests, "Session", Session)
        session_patch.start()
        self.addCleanup(session_patch.stop)

    def _plugin(self, selected=None):
        plugin = self.module.ProwlarrExtend()
        # 空地址初始化不启动后台线程；随后接入真实客户端与替身 HTTP。
        plugin.init_plugin({"enabled": True, "selected_indexers": selected or []})
        plugin._config.update(host="http://prowlarr", api_key="test-secret")
        plugin._client = self.module.ProwlarrClient(
            "http://prowlarr", "test-secret", timeout=30
        )
        self.addCleanup(plugin.stop_service)
        return plugin

    @staticmethod
    def _indexers():
        return [
            {"id": 1, "name": "Alpha", "enable": True, "protocol": "torrent"},
            {"id": 2, "name": "Beta", "enable": True, "protocol": "torrent"},
            {"id": 3, "name": "Gamma", "enable": True, "protocol": "torrent"},
            {"id": 4, "name": "Disabled", "enable": False, "protocol": "torrent"},
            {"id": 5, "name": "Usenet", "enable": True, "protocol": "usenet"},
            {"id": 6, "name": "NoSearch", "supportsSearch": False, "protocol": "torrent"},
        ]

    @staticmethod
    def _state_snapshot(plugin):
        # 覆盖全部实例属性，以及客户端的嵌套配置和停止标志；锁/代际按身份记录。
        state = {}
        for name, value in vars(plugin).items():
            if name in ("_lock", "_generation"):
                state[name] = id(value)
            elif name == "_stop_event":
                state[name] = (id(value), value.is_set())
            elif name == "_client":
                state[name] = (id(value), copy.deepcopy(vars(value)))
            else:
                state[name] = copy.deepcopy(value)
        return state

    def test_package_v3_matches_plugin_version(self):
        package = json.loads((ROOT / "package.v3.json").read_text(encoding="utf-8"))
        self.assertEqual(set(package), {"ProwlarrExtend", "SubtitleHunter"})
        self.assertEqual(package["ProwlarrExtend"]["version"], self.module.ProwlarrExtend.plugin_version)

    def test_empty_site_aggregates_selected_indexers_and_deduplicates(self):
        plugin = self._plugin(selected=["1", "2", "4", "5", "6"])
        first = {
            "indexerId": 1, "title": "Shared", "guid": "urn:shared",
            "downloadUrl": "https://alpha.invalid/download/shared", "seeders": 10,
        }
        second = {
            **first, "indexerId": 2,
            "downloadUrl": "https://beta.invalid/download/shared", "seeders": 20,
        }
        unique = {
            "indexerId": 2, "title": "Unique", "guid": "urn:unique",
            "downloadUrl": "https://beta.invalid/download/unique",
        }
        outside = {**first, "indexerId": 3, "title": "Outside selection"}
        self.responses.update(
            indexer=_Response(self._indexers()),
            search=_Response([first, {**first, "seeders": 99}, second, unique, outside]),
        )

        with patch.object(plugin._client, "get_indexers", wraps=plugin._client.get_indexers) as catalog, \
                patch.object(plugin._client, "search", wraps=plugin._client.search) as search:
            results = plugin.search_torrents(site={}, keyword="Shared", page=1)

        self.assertEqual([(r.site_name, r.title) for r in results], [
            ("Alpha", "Shared"), ("Beta", "Shared"), ("Beta", "Unique"),
        ])
        self.assertEqual([r.seeders for r in results[:2]], [10, 20])
        self.assertEqual([call["path"] for call in self.calls], ["indexer", "search"])
        params = self.calls[-1]["params"]
        self.assertEqual([value for key, value in params if key == "indexerIds"], ["1", "2"])
        self.assertIn(("query", "Shared"), params)
        self.assertIn(("limit", 100), params)
        self.assertIn(("offset", 100), params)
        # 首次目录获取与单次聚合检索使用同一个绝对截止时间。
        self.assertEqual(catalog.call_args.kwargs["deadline"], search.call_args.kwargs["deadline"])

    def test_empty_selection_searches_all_eligible_indexers(self):
        plugin = self._plugin()
        self.responses.update(indexer=_Response(self._indexers()), search=_Response([]))

        self.assertEqual(plugin.search_torrents(site={}, keyword="Movie"), [])

        params = self.calls[-1]["params"]
        self.assertEqual([value for key, value in params if key == "indexerIds"], ["1", "2", "3"])

    def test_torrent_info_maps_indexer_download_size_and_seeders(self):
        for source in ("indexer", "indexerName", None):
            with self.subTest(name_source=source):
                plugin = self._plugin(selected=["2"])
                entry = {
                    "indexerId": 2, "title": "Movie", "size": 5 * 1024 ** 3,
                    "seeders": 42, "leechers": 7,
                    "downloadUrl": "http://prowlarr/download/2/movie?token=example",
                    "magnetUrl": "magnet:?xt=urn:btih:fallback",
                    "publishDate": "2026-10-23T05:00:00Z", "imdbId": 1234567,
                    "categories": [{"id": 2000, "name": "Movies"}],
                    "indexerFlags": ["freeleech", "doubleupload"],
                }
                if source:
                    entry[source] = "Beta"
                self.responses.update(indexer=_Response(self._indexers()), search=_Response([entry]))

                results = plugin.search_torrents(site={}, keyword="Movie")

                self.assertEqual(len(results), 1)
                torrent = results[0]
                self.assertIsInstance(torrent, _TorrentInfo)
                self.assertEqual(torrent.site_name, "Beta")
                self.assertEqual(torrent.enclosure, entry["downloadUrl"])
                self.assertEqual(torrent.size, 5 * 1024 ** 3)
                self.assertEqual(torrent.seeders, 42)
                self.assertEqual(torrent.peers, 7)
                self.assertEqual(torrent.media_id, "tt1234567")
                self.assertEqual(torrent.media_source, "imdb")
                self.assertEqual(torrent.category, "电影")
                self.assertEqual(torrent.labels, ["Movies"])
                self.assertEqual((torrent.downloadvolumefactor, torrent.uploadvolumefactor), (0.0, 2.0))

    def _assert_http_error(self, response, kind):
        self.responses["indexer"] = response
        self.assertFalse(bool(response))
        client = self.module.ProwlarrClient("http://prowlarr", "test-secret")

        with self.assertRaises(self.module.ProwlarrError) as raised:
            client.get_indexers()

        error = raised.exception
        self.assertEqual(error.kind, kind)
        self.assertEqual(error.status_code, response.status_code)
        self.assertIn(f"HTTP {response.status_code}", str(error))
        self.assertNotIn("无响应", str(error))
        self.assertNotIn("test-secret", str(error))
        return error

    def test_http_401_is_authentication_even_with_unreadable_json_body(self):
        for json_error in (None, ValueError("not JSON")):
            with self.subTest(json_error=json_error):
                error = self._assert_http_error(
                    _Response({"message": "invalid test-secret"}, 401, json_error=json_error),
                    "authentication",
                )
                self.assertIn("认证错误", str(error))

    def test_http_429_is_rate_limit_and_preserves_retry_after(self):
        error = self._assert_http_error(
            _Response({"message": "too many requests"}, 429, {"Retry-After": "7"}),
            "rate_limit",
        )
        self.assertIn("请求限流", str(error))
        self.assertIn("Retry-After=7", str(error))

    def test_http_500_is_server_error(self):
        error = self._assert_http_error(
            _Response({"message": "indexer unavailable"}, 500), "server_error"
        )
        self.assertIn("服务端错误", str(error))
        self.assertIn("indexer unavailable", str(error))

    def test_expired_total_deadline_does_not_start_http_request(self):
        client = self.module.ProwlarrClient("http://prowlarr", "test-secret")

        with self.assertRaises(self.module.ProwlarrError) as raised:
            client.get_indexers(deadline=time.monotonic() - 1)

        self.assertEqual(raised.exception.kind, "timeout")
        self.assertEqual(self.calls, [])

    def _probe_plugin(self):
        plugin = self._plugin(selected=["2"])
        cached = {"id": "99", "name": "Cached", "privacy": "private"}
        plugin._snapshot = ((cached,), ({"id": "99", "name": "Cached"},), True)
        plugin._config["indexer_catalog"] = [{"id": "99", "name": "Cached"}]
        self.responses["system/status"] = _Response({"version": "1.32.0", "appName": "Prowlarr"})
        return plugin

    def test_connection_success_is_read_only(self):
        plugin = self._probe_plugin()
        self.responses["indexer"] = _Response(self._indexers())
        with patch.object(self.module.ProwlarrExtend, "update_config") as persist:
            before = self._state_snapshot(plugin)

            result = plugin.test_connection()

            self.assertEqual(self._state_snapshot(plugin), before)
            persist.assert_not_called()
        self.assertIs(result["success"], True)
        self.assertEqual(result["version"], "1.32.0")
        self.assertEqual(result["available"], 3)
        self.assertEqual(result["indexers"], 1)
        self.assertEqual(result["selected"], ["2"])
        self.assertEqual([call["path"] for call in self.calls], ["system/status", "indexer"])

    def test_connection_indexer_probe_failure_is_read_only_and_not_success(self):
        plugin = self._probe_plugin()
        self.responses["indexer"] = _Response({"message": "indexer unavailable"}, 500)
        with patch.object(self.module.ProwlarrExtend, "update_config") as persist:
            before = self._state_snapshot(plugin)

            result = plugin.test_connection()

            self.assertEqual(self._state_snapshot(plugin), before)
            persist.assert_not_called()
        self.assertIs(result["success"], False)
        self.assertEqual(result["error"], "server_error")
        self.assertEqual(result["status_code"], 500)
        self.assertIn("连接失败", result["message"])
        self.assertEqual([call["path"] for call in self.calls], ["system/status", "indexer"])


if __name__ == "__main__":
    unittest.main()
