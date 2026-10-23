# MoviePilot v3.1.9 宿主实测事实（2026-10-23，取自 NAS 上真实运行的 v3.1.9 容器）

来源：NAS 容器 `moviepilot-v3`（jxxghp/moviepilot-v3:latest，APP_VERSION = 'v3.1.9'，2026-10-23 已从 v3.0.8 升级到此版本）。以下均为容器内源码/日志实证，不是文档推测。

## P0-1 三处断点：在真实 v3.1.9 上全部证实

- **A（add_indexer）**：`/app/app/helper/` 目录在 v3.1.9 中不存在；全仓 `grep -rn 'class SitesHelper' /app/app` 与 `grep -rln 'def add_indexer' /app/app` 均无结果。插件 v2 代码的 `from app.helper.sites import SitesHelper` 在 v3.1.9 必然 ImportError。
- **B（Site db=None）**：`/app/app/db/models/site.py:66-70`：`get_by_domain(cls, db: Session, domain)` 直接执行 `db.execute(select(...))`，无 None 兼容层；其余类方法（get_actives/list_order_by_pri/get_domains_by_ids/reset 及 async 版本）签名相同。传 None 必然 AttributeError。
- **C（site={}）**：`/app/app/chain/base.py:659` `search_plugin_torrents(keyword, mtype, page)` 通过 `execute_plugin_modules("search_torrents", None, site={}, keyword=..., mtype=..., page=...)` 分派给插件模块；异步版 `async_search_plugin_torrents` 在 `:726`、以 `site={}` 分派 `async_search_torrents`（`:737`）。站点搜索是独立路径 `search_site_torrents`（系统模块、传真实 site）。即：v3 里插件的 `search_torrents` 收到空 site 是**设计语义**（插件自带资源源、自行扇出），不是异常。

## v3 插件体系

- 目录布局：`plugins.v3/<小写插件id>/`；本地安装根：`/config/local-plugins/plugins.v3/<id>/`（含 dist 前端产物）。宿主目录解析顺序见 `/app/app/runtime/extensions/plugin/paths.py:131`（plugins、plugins.v2、plugins.v3）。
- 目录文件：`package.v3.json`（仓库根；插件类注释惯例：「插件版本，需与 package.v3.json 中保持一致」，见内置 napcatmsg）。NAS 上 v3.0.8/v3.1.9 日志显示宿主拉取插件库时依次请求 `package.json`、`package.v3.json`、`package.v2.json`。
- 加载器 `/app/app/runtime/extensions/plugin/loader.py`：只加载「已安装列表」中的插件；本仓两个插件当前在 `/config/plugins` 下有副本，但 v3.1.9 启动日志为「跳过插件目录：prowlarrextend / subtitlehunter（不在加载列表中）」——即现在根本没有被加载。
- 宿主自带契约测试脚手架：`/app/app/testing/bootstrap.py:296` 可用插件仓的 `plugins.v3` 子目录暴露插件源码做测试。

## v3 插件类骨架（取自内置 subtitleassistant，102 行）

- `from app.plugins import _PluginBase`；类属性：plugin_name / plugin_desc / plugin_icon / plugin_version / plugin_author / plugin_label / plugin_config_prefix / plugin_order / auth_level。
- 方法面：`init_plugin(config: Mapping | None)`、`get_state()`、`get_command()`（静态）、`get_render_mode()`（如 `("vue", "frontend/dist/assets")`）、`get_sidebar_nav()`、`get_api()`、`get_form() -> (list, dict)`、`get_page()`、`stop_service()`、`reset_data_sync()`。
- 事件：`from app.core.event import Event, eventmanager` + `from app.schemas.types import EventType`，`@eventmanager.register(EventType.TransferComplete)`，handler 可为 async。
- TorrentInfo：搜索链的契约类型是 `/app/app/domain/context.py:1639` 的 `@dataclass TorrentInfo`（`app/chain/base.py:21` 即从此导入，带 `to_dict()`）；`/app/app/schemas/context.py:381` 的同名 pydantic 模型不是搜索链用的类型，插件返回它会在宿主 `_completion_events` 处 `AttributeError: 'TorrentInfo' object has no attribute 'to_dict'`（2026-10-23 真机验收实测）。该 dataclass 无 `imdbid` 字段，IMDb 身份用 `media_source=MediaSource.IMDb`（`app/schemas/types.py:114`，值 `"imdb"`）+ `media_id="tt…"`。

## 对实现的直接含义

1. ProwlarrExtend 的 v3 版应是「插件资源源」：`search_torrents(site={}, ...)` 时扇出到所选 Prowlarr 索引器（优先单个带多 indexerIds 的聚合请求），结果 TorrentInfo 保留真实索引器身份（标题/下载链接/大小/做种数等按 TorrentInfo 契约填），彻底删除 SitesHelper / Site CRUD / IndexerSites / 假域名一整套。
2. SubtitleHunter 与宿主耦合较浅（事件 + 文件/媒体处理），可机械移植到 plugins.v3 并顺带修已证实的正确性问题（见 GPT_REVIEW.md 第三步清单）。
3. 本仓现有用户的实际环境 = NAS v3.1.9 + NAS 上的 Prowlarr 容器（lscr.io/linuxserver/prowlarr），迁移验收最终要在这个真实环境做。

## 第二批（SubtitleHunter）宿主契约实测（2026-10-23，NAS v3.1.9 双路核对）

- v2 SubtitleHunter 的全部宿主 import 在 v3.1.9 容器内逐条实测可导入：`app.log.logger`、`app.plugins._PluginBase`、`app.core.context.MediaInfo`、`app.core.event.eventmanager/Event`、`app.core.config.settings`、`app.schemas.types.NotificationType/EventType`（兼容层健在）。
- `_PluginBase` 实现在 `/app/app/sdk/plugin/base.py`：`update_config`(:320)、`get_config`(:334)、`get_data_path`(:343)、`save_data`(:366)、`get_data`(:391)、`post_message`(:425) 均在；内置 v3 插件 autosubv2 实际以 `post_message(mtype=NotificationType.Plugin, ...)` 与 `get_data_path()` 使用之。
- 事件契约：`EventType.TransferComplete = "transfer.complete"`（`/app/app/schemas/types.py:280`）；事件 `event_data` 含 `mediainfo` 与 `transferinfo` 两键（v2 处理器与 autosubv2 的 v3 处理器读法一致，`:291` `@eventmanager.register(EventType.TransferComplete)`）；`transferinfo.target_path` 存在（`/app/app/schemas/download.py:108`），另有 `file_list_new`（autosubv2 实际使用）。
- 系统 LLM 默认值：宿主 chatgpt 插件以 `getattr(settings, "LLM_PROVIDER"/"LLM_API_KEY", None)` 读取，与 v2 SubtitleHunter 的读法一致，v3.1.9 仍有效。翻译本身是插件自带 OpenAI 兼容 HTTP 客户端（用户配置的 base_url/api_key），不依赖宿主 LLM 通道。
- 注意（第一批教训）：`MediaInfo` 同样有 `app/domain/context.py:1907` 的 dataclass 版与 `app/schemas/context.py:280` 的 pydantic 版两个同名类；SubtitleHunter 只以 getattr 读 mediainfo 属性（title_year/title/original_language 等），属鸭子类型，**不要构造 MediaInfo、不要 isinstance 判断**。
