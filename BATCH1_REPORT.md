# 第一批交付报告：ProwlarrExtend v3

日期：2026-10-23。仓库基线：`main @ f20bf13`。

本轮补齐插件清单、替身单测和本报告。现有 v3 搜索实现通过新增的 10 个用例，未发现需要最小修复的插件代码 bug；`plugins.v3/`、`plugins.v2/`、已有测试文件及 `tests/conftest.py` 均未修改。本结果不等于真实 MoviePilot v3.1.9 宿主验收通过。

背景仅参考 `HOST_V3_FACTS.md` 与 `GPT_REVIEW.md` 第三、四节，未读取 `REVIEW_1017.txt` 正文。

## 实现结构与文件清单

| 文件 | 来源与职责 |
| --- | --- |
| `plugins.v3/prowlarrextend/__init__.py`（455 行） | 本轮开始前已有；插件生命周期、配置与界面、索引器快照、同步/异步搜索、TorrentInfo 构造、连接/状态 API。 |
| `plugins.v3/prowlarrextend/client.py`（249 行） | 本轮开始前已有；requests 客户端、聚合搜索参数、绝对截止时间、HTTP 错误分类及只读连接探测。 |
| `plugins.v3/prowlarrextend/mapping.py`（85 行） | 本轮开始前已有；发布时间、IMDb、分类与促销倍率纯函数。 |
| `plugins.v3/prowlarrextend/selection.py`（102 行） | 本轮开始前已有；多选 ID 规范化、目录与表单选项、选择过滤纯函数。 |
| `package.v3.json` | 本轮新增；只登记 ProwlarrExtend 3.0.0。 |
| `tests/test_prowlarrextend_v3.py` | 本轮新增；10 个 unittest 用例，pytest 也可收集。文件头明确标注“替身单测，非宿主契约测试”。 |
| `BATCH1_REPORT.md` | 本轮新增；交付范围、设计依据、实际验证结果与下一批衔接。 |

测试沿用 `conftest.py` 的 `ModuleType`、`SimpleNamespace`、宽松 TorrentInfo 替身风格，所有补充替身都位于新测试文件内。通过独立包别名加载 v3，并在类级清理中恢复导入替身，避免与已有 v2 的 `prowlarrextend` 导入冲突。HTTP 用局部替换的 Session 和假 Response，执行真实 v3 客户端逻辑，不访问网络。

## 与 v2 的语义对应

| v2 行为 | v3 对应行为 |
| --- | --- |
| 将索引器同步到 Site、SitesHelper 与 IndexerSites，以虚拟站点参与检索 | 插件自身作为资源源；宿主传 `site={}`，插件直接查询 Prowlarr 所选索引器，不进行站点 CRUD 或全局搜索范围写入。 |
| 根据所传虚拟站点定位单个索引器 | 从插件快照取得全部有效选择，发送一次多 `indexerIds` 聚合查询；非空 site 留给宿主独立的站点搜索路径。 |
| `selected_indexers` 多选，空选表示全部 | 保留该配置语义；只搜索启用、支持搜索且协议为 Torrent 的索引器。旧前缀形式的 ID 仍可规范化。 |
| 保存 indexer catalog，周期同步，支持立即运行一次 | 保留目录供表单展示；刷新一次发布“所选索引器、完整目录、有效性”快照，失败保留上次成功状态；正常初始化后台刷新，立即运行一次直接刷新。 |
| 站点元数据及 Prowlarr 字段映射 | 保留真实索引器名称、下载链接、大小、做种数、时间、IMDb、分类及促销倍率；不制造宿主 Site ID。 |
| 配置搜索超时，区分 HTTP 失败与空结果 | 配置仍为 5–120 秒，但按整轮绝对截止时间控制目录获取、聚合检索和映射；错误携带类别/状态码，成功空列表仍是有效结果。 |
| `/test`、`/status` API | 保留连接与状态查询；连接探测单独读取系统状态和索引器目录，不刷新或持久化插件状态。 |

这里对应的是可观察的功能语义，不承诺 v2 与 v3 宿主接口通用。v3 TorrentInfo 导入自 `app.schemas.context`，v2 使用 `app.core.context`。旧虚拟站点和 IndexerSites 历史记录不会因停止写入而自动清除；本轮没有执行历史数据迁移或清理。

## 关键设计决定

### 聚合请求与总超时

空 site 的逻辑扇出交由 Prowlarr 一次 `/api/v1/search` 请求完成，参数重复传入各个 `indexerIds`。插件合并响应中的有效条目，并再次核对索引器身份和选择范围；分页大小为 100，offset 为页码乘以 100。首次搜索需要读取目录时，该读取与后续搜索共用一个 `time.monotonic()` 绝对截止时间，索引器数量不会乘大等待预算。

去重键为“索引器 ID + guid”，guid 缺失时用下载链接。相同索引器的重复结果保留首条；不同索引器即使 guid 相同也保留各自的名称、下载链接和倍率。本轮用例覆盖站内重复、跨索引器同 guid、未选索引器返回条目的过滤，以及空选搜索全部有效索引器。

HTTP 读取在只读 daemon 工作线程中执行，主调用按截止时间等待；requests 的连接/读取超时也受剩余预算约束。到期不强制杀死工作线程，但取消其结果发布。映射期间也检查截止时间。这种设计限制调用者等待时间，并非保证到期时底层网络线程已退出。本轮验证目录与搜索共用截止时间、过期预算不发起 HTTP；慢响应和真实多索引器耗时未作集成验证。

### 错误分类

客户端先检查 `status_code`，不使用 Response 的真假判断是否存在响应。401/403 为 `authentication`，404 为 `not_found`，429 为 `rate_limit`，5xx 为 `server_error`，其他非 2xx 为 `http_error`；另区分 `configuration`、`network`、`timeout` 和 `bad_response`。错误正文只做尽力解码，429 保留 Retry-After，诊断文本替换 API Key。

本轮构造在布尔判断中为假的 401、429、500 Response，验证各自类别、HTTP 状态码和中文错误标签；401 另覆盖 JSON 解码失败后的正文回退，确认不变成“无响应”。成功空列表没有被当作 HTTP 错误。搜索入口捕获客户端错误后返回空列表，但客户端已记录分类日志；连接 API 则直接返回错误类别和状态码。

### 只读探测

`test_connection()` 复制当前客户端引用和选择列表，调用 `probe_connection()` 依次 GET `system/status` 与 `indexer`，在临时结果上计算可用/所选数量。它不调用目录刷新或 `update_config()`。即使系统状态成功，索引器目录失败也返回 `success=False`。

成功与失败用例均比较调用前后的全部插件实例状态，包括嵌套配置、目录快照、客户端属性、代际/锁身份和停止标志，并确认没有配置持久化调用。探测使用与缓存不同的目录内容，确保测试能发现误写快照的行为。

## package.v3.json 说明

根文件沿用 `package.v2.json` 的结构与 ProwlarrExtend 元数据键：`name`、`description`、`labels`、`version`、`icon`、`author`、`level`、`history`。唯一顶层项为 `ProwlarrExtend`，`version` 为 `3.0.0`，与插件 `plugin_version` 一致；新增单测核对唯一登记项与版本。

history 仅一项：`v3.0.0` → “v3 首版：采用插件资源源架构，聚合所选 Prowlarr 索引器，不再依赖站点桥接”。本轮未加入 SubtitleHunter，也未改变 `package.v2.json`。登记清单本身不代表插件已加入真实宿主的已安装列表。

## 测试运行结果原文摘要

环境：系统 Python 3.12.3，有系统 requests，无 pytest。按要求先创建隔离环境，再尝试安装依赖。

1. `python3 -m venv /tmp/v3venv`：退出码 0，无输出。
2. `/tmp/v3venv/bin/python -m pip install pytest requests`：退出码 1。网络权限阻止连接代理，实际输出关键原文如下：

```text
WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, status=None)) after connection broken by 'ProxyError('Cannot connect to proxy.', NewConnectionError('<pip._vendor.urllib3.connection.HTTPSConnection object at 0x788296db18e0>: Failed to establish a new connection: [Errno 1] Operation not permitted'))': /simple/pytest/
ERROR: Could not find a version that satisfies the requirement pytest (from versions: none)
ERROR: No matching distribution found for pytest
```

3. `/tmp/v3venv/bin/python -m pytest tests/ -q`：退出码 1，实际输出：

```text
/tmp/v3venv/bin/python: No module named pytest
```

4. 改用系统 Python 编译新测试及全部现有 v3 模块：

```bash
python3 -m py_compile tests/test_prowlarrextend_v3.py plugins.v3/prowlarrextend/__init__.py plugins.v3/prowlarrextend/client.py plugins.v3/prowlarrextend/mapping.py plugins.v3/prowlarrextend/selection.py
```

退出码 0，无输出。

5. 首次 unittest 运行：退出码 1，摘要原文：

```text
Ran 10 tests in 0.015s

FAILED (failures=2)
```

两处失败均为测试快照对实例上的 `update_config` MagicMock 深拷贝后产生不同对象，未证明插件状态被修改。将该方法替换移到插件类上，仍验证其未被调用，插件状态快照继续覆盖全部实例属性。只修正测试，未修改插件源码。

6. 修正后运行 `python3 -m unittest discover -s tests -p test_prowlarrextend_v3.py -v`：退出码 0，原文：

```text
test_connection_indexer_probe_failure_is_read_only_and_not_success (test_prowlarrextend_v3.ProwlarrExtendV3Tests.test_connection_indexer_probe_failure_is_read_only_and_not_success) ... ok
test_connection_success_is_read_only (test_prowlarrextend_v3.ProwlarrExtendV3Tests.test_connection_success_is_read_only) ... ok
test_empty_selection_searches_all_eligible_indexers (test_prowlarrextend_v3.ProwlarrExtendV3Tests.test_empty_selection_searches_all_eligible_indexers) ... ok
test_empty_site_aggregates_selected_indexers_and_deduplicates (test_prowlarrextend_v3.ProwlarrExtendV3Tests.test_empty_site_aggregates_selected_indexers_and_deduplicates) ... ok
test_expired_total_deadline_does_not_start_http_request (test_prowlarrextend_v3.ProwlarrExtendV3Tests.test_expired_total_deadline_does_not_start_http_request) ... ok
test_http_401_is_authentication_even_with_unreadable_json_body (test_prowlarrextend_v3.ProwlarrExtendV3Tests.test_http_401_is_authentication_even_with_unreadable_json_body) ... ok
test_http_429_is_rate_limit_and_preserves_retry_after (test_prowlarrextend_v3.ProwlarrExtendV3Tests.test_http_429_is_rate_limit_and_preserves_retry_after) ... ok
test_http_500_is_server_error (test_prowlarrextend_v3.ProwlarrExtendV3Tests.test_http_500_is_server_error) ... ok
test_package_v3_matches_plugin_version (test_prowlarrextend_v3.ProwlarrExtendV3Tests.test_package_v3_matches_plugin_version) ... ok
test_torrent_info_maps_indexer_download_size_and_seeders (test_prowlarrextend_v3.ProwlarrExtendV3Tests.test_torrent_info_maps_indexer_download_size_and_seeders) ... ok

----------------------------------------------------------------------
Ran 10 tests in 0.022s

OK
```

**实际通过的是新增的 10 个替身用例；全量 `tests/` pytest 未能运行，已有 v2/字幕测试没有本轮全量通过结论。** 401 正文回退及三种索引器名称来源使用 subTest，不另计为独立测试数量。新文件可以在依赖可用时直接参加 `python -m pytest tests/ -q`。

最终修正后的新测试与全部 v3 模块再次 py_compile，退出码 0、无输出；`git diff --check` 退出码 0、无输出。另用系统 Python 检查新增文件的 UTF-8、换行及行尾空白，解析两个包清单并以 AST 读取插件版本，同时核对已有 v2 代码/测试没有 diff，退出码 0，输出：

```text
新增文件 UTF-8/换行/空白检查通过；package.v3.json 结构和版本一致；已有 v2 代码与测试无改动。
```

## 未决事项：真实 v3.1.9 宿主验收

以下不能由宽松替身或 py_compile 证明。本轮没有安装或部署到 NAS。

| 验收项 | 需在真实宿主完成的检查 |
| --- | --- |
| TorrentInfo 字段契约 | 固定 v3.1.9 镜像摘要/源码版本，以真实 `app.schemas.context.TorrentInfo` 验证必填项、类型和默认值；从同步/异步插件搜索分派入口实际执行空 site，确认结果可序列化并显示真实索引器名称。替身仅接收 kwargs，不能证明 Pydantic 契约。 |
| 下载链 | 经真实宿主搜索、订阅和下载链至少完成一次下载；核对 enclosure 的 Prowlarr 下载地址/磁力链接、鉴权、代理、必要站点元数据与倍率。对真实 Prowlarr 验证多 `indexerIds` 参数、分页、身份回填、慢索引器与总超时行为。 |
| 已安装列表注册 | 从仓库根 `package.v3.json` 验证插件市场识别、安装目录解析以及已安装列表登记，确认重启后不再出现“不在加载列表中”的跳过日志；同时验证升级/回退。源码目录存在或版本相等不能代替注册成功。 |
| 历史桥接数据处置 | 在真实 v3.1.9 可用的会话/管理接口下先备份，再制定一次性、幂等、可回滚的旧托管 Site/IndexerSites 清理；核对 managed 标记，保留普通站点。该处置尚未实施。 |

## 第二批 SubtitleHunter v3 衔接

第一批的包清单暂只含 ProwlarrExtend。第二批可在 `plugins.v3/subtitlehunter/` 单独迁移，验收后再向 `package.v3.json` 添加 SubtitleHunter 项及独立版本/history，继续保留 v2 实现和既有测试基线。复用本轮的隔离导入与局部宿主替身方法，同时另设真实宿主契约测试，避免用替身冒充宿主兼容证据。

按 `GPT_REVIEW.md` 第四节第三步优先阻断字幕静默损坏：SRT 时轴规范化与异常块诊断、内部 cue 唯一编号、严格解码/BOM、forced 元数据判断、逐条译文校验，以及支持的 ASS 标签在翻译和压缩阶段的恢复校验；复杂不支持场景应报告失败并保留原字幕，失败不得替换已有文件。保留现有纯模块和已修正的 mixin MRO，不为迁移先整体重排结构。

随后补认证/参数错误快速失败、429/5xx/网络退避与总预算、取消贯穿和输出完整性校验。系统 LLM 入口、真实 TransferComplete 事件字段和粒度需在 v3.1.9 核对后确定；不能假设某个 helper 存在就能支持全部 provider。第一批仍待真实宿主验证的安装注册、字段契约与下载链可与第二批共享验收环境，但应分别记录两插件的通过证据。

## 真机验收补记（2026-10-23，NAS MoviePilot v3.1.9）

第一批已在 NAS 真机完成安装与验收（容器 moviepilot-v3，v3.1.9；插件经本地仓库 local-repo + `PLUGIN_LOCAL_REPO_PATHS` 注册，配置与 Prowlarr 地址/API key 直写 user.db）。

验收中抓到并修复一个真 bug（v3.0.1）：

- 现象：真实搜索宿主端 HTTP 500，`AttributeError: 'TorrentInfo' object has no attribute 'to_dict'`（`app/chain/search/execution.py:291`）。
- 根因：v3 有两个同名 `TorrentInfo`。搜索链契约是 `app/domain/context.py:1639` 的 dataclass 版（`chain/base.py` 从此导入），第一批误用了 `app/schemas/context.py:381` 的 pydantic 版。dataclass 版没有 `imdbid` 字段，IMDb 身份须以 `media_source=MediaSource.IMDb`（值 `"imdb"`）+ `media_id="tt…"` 表达。`HOST_V3_FACTS.md` 原先指错了类，已同步改正。
- 修复：`__init__.py` 改导入 `app.domain.context.TorrentInfo` 与 `app.schemas.types.MediaSource`，映射改 media_source/media_id；替身单测断言同步更新，10/10 全过。

修复后验收结果（TMDB 693134《沙丘2》）：

- 宿主搜索 HTTP 200、`success=true`，共 41 条：BeyondHD 23 条、PassThePopcorn 18 条（BroadcasTheNet 为剧集站、PTerClub 无匹配，均 0 条，属正常）。
- 41 条 enclosure 全部是 Prowlarr 下载地址（`192.168.1.220:7188/<indexer>/download?...`），下载链指向正确。
- 插件 `/status`：enabled=true、indexers=4、authoritative=true；`/test` 成功，Prowlarr 版本 2.6.5.5623。
- 已知特性：三关键词串行检索中，繁体标题一轮曾耗尽 30s 总预算返回 0 条（Prowlarr/索引器延迟波动）；英文标题一轮 41 条/11.7s。总预算默认值后续可按实测再议。

安装机制备忘：本地插件同步服务的文件监控触发条件未完全摸清，真机更新运行目录本次用 `docker cp` 直拷 + `POST /api/v1/plugin/reload/ProwlarrExtend`；local-repo 仍是持久来源，容器重建后应由启动同步恢复。
