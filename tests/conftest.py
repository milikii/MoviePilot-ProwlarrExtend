import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _EventManager:
    def register(self, _event_type):
        return lambda func: func

    def send_event(self, *args, **kwargs):
        return None


class _PluginBase:
    def __init__(self):
        self.systemconfig = SimpleNamespace(get=lambda *_: [], set=lambda *_: None)
        self.eventmanager = _EventManager()

    def get_config(self, *args, **kwargs):
        return {}

    def update_config(self, *args, **kwargs):
        return None

    def get_data_path(self, *args, **kwargs):
        path = Path("/tmp/moviepilot-plugin-tests") / self.__class__.__name__
        path.mkdir(parents=True, exist_ok=True)
        return path

    def post_message(self, *args, **kwargs):
        return None


class _TorrentInfo:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _MediaType(Enum):
    MOVIE = "电影"
    TV = "电视剧"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def _install_import_stubs():
    cron_module = _install_module("apscheduler.triggers.cron")

    class CronTrigger:
        @classmethod
        def from_crontab(cls, expression):
            return expression

    cron_module.CronTrigger = CronTrigger
    _install_module("apscheduler")
    _install_module("apscheduler.triggers")

    async def run_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    _install_module("fastapi")
    _install_module("fastapi.concurrency", run_in_threadpool=run_in_threadpool)

    _install_module("app")
    _install_module("app.helper")
    _install_module(
        "app.helper.sites",
        SitesHelper=type(
            "SitesHelper",
            (),
            {
                "get_indexer": lambda self, domain: None,
                "add_indexer": lambda self, domain, value: None,
            },
        ),
    )
    _install_module("app.core")
    settings = SimpleNamespace(
        USER_AGENT="pytest",
        PROXY=None,
        LLM_PROVIDER="openai",
        LLM_MODEL="test-model",
        LLM_API_KEY="test-key",
        LLM_BASE_URL="https://example.invalid/v1",
        LLM_USER_AGENT=None,
        LLM_USE_PROXY=False,
    )
    _install_module("app.core.config", settings=settings)
    _install_module("app.core.context", TorrentInfo=_TorrentInfo, MediaInfo=object)
    eventmanager = _EventManager()

    class Event:
        def __init__(self, event_data=None):
            self.event_data = event_data or {}

    _install_module("app.core.event", eventmanager=eventmanager, Event=Event)
    _install_module("app.db")
    _install_module("app.db.models")
    _install_module("app.db.models.site", Site=type("Site", (), {}))
    _install_module("app.plugins", _PluginBase=_PluginBase)
    _install_module("app.schemas", MediaType=_MediaType)

    class EventType:
        SiteUpdated = "site.updated"
        TransferComplete = "transfer.complete"

    class SystemConfigKey:
        IndexerSites = "IndexerSites"

    class NotificationType:
        Plugin = "plugin"

    _install_module(
        "app.schemas.types",
        EventType=EventType,
        SystemConfigKey=SystemConfigKey,
        NotificationType=NotificationType,
    )
    _install_module("app.utils")

    class RequestUtils:
        def __init__(self, *args, **kwargs):
            pass

        def get_res(self, *args, **kwargs):
            return None

    _install_module("app.utils.http", RequestUtils=RequestUtils)
    _install_module("app.log", logger=_Logger())


def _load_module(name, relative_path):
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def plugin_modules():
    _install_import_stubs()
    return SimpleNamespace(
        prowlarr=_load_module(
            "test_prowlarrextend_plugin",
            "plugins.v2/prowlarrextend/__init__.py",
        ),
        subtitle=_load_module(
            "test_subtitlehunter_plugin",
            "plugins.v2/subtitlehunter/__init__.py",
        ),
    )
