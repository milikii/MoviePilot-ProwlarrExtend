# MoviePilot-ProwlarrExtend & SubtitleHunter

一组 MoviePilot V2 第三方插件。

- **ProwlarrExtend** —— 把 Prowlarr 里的索引器桥接进 MoviePilot 的搜索系统，让搜索和订阅能覆盖 PTP / BTN / BHD 这类国际私有站。
- **SubtitleHunter** —— 媒体入库后自动处理字幕：探测、提取、规范命名、AI 翻译，保证媒体库里始终有可用的中文字幕。

两个插件都在本仓库的 `plugins.v2/` 目录下，通过 MoviePilot 第三方插件仓库直接安装，无需单独发布。

---

## ProwlarrExtend

### 为什么需要它

MoviePilot 内置的索引器只认 NexusPHP 系列的国内站点。而 Gazelle、UNIT3D、Luminance 这些架构的国际站全都不在内置支持范围里——而它们恰好是 Prowlarr 擅长接管的部分。

本插件做的事很直接：调用 Prowlarr 的 `/api/v1/indexer` 拉取索引器列表，把每一个启用的 Torrent 索引器以虚拟站点的形式注册进 MoviePilot 的 `SitesHelper`，再挂上 `search_torrents` 钩子。MoviePilot 一次搜索，命中 Prowlarr 的索引器时由本插件代为向 Prowlarr 的 Newznab/Capabilities 接口发起查询，再把结果转回 `TorrentInfo`。

最终效果：Prowlarr 中配好的索引器，在 MoviePilot 里直接当站点用。

### 同步逻辑

插件用 `get_service()` 注册一个定时任务，按 cron 周期做**完整同步**：

1. 从 Prowlarr 拉取索引器，过滤掉禁用的、不支持搜索的、非 Torrent 协议的；
2. 在 `SitesHelper` 注册或更新虚拟站点；
3. 在 MoviePilot 站点表里建/改对应记录；
4. 同步 MoviePilot 的搜索范围（收视率、包含站点等），并清理 Prowlarr 里已经不再启用的站点。

Prowlarr 那边什么时候增删索引器，下一次 cron 一跑，MoviePilot 这边就会跟上。

配置项：

| 参数 | 说明 |
|------|------|
| 启用插件 | 总开关 |
| 使用代理服务器 | 走 MoviePilot 配置的代理访问 Prowlarr |
| 立即运行一次 | 打开即触发一次同步，不等 cron |
| 更新周期 | 5 位 cron 表达式，默认 `0 0 */24 * *`（每 24 小时） |
| Prowlarr 地址 | 如 `http://prowlarr:9696` |
| Api Key | 在 Prowlarr → Settings → General → Security 里取 |

前置条件：Prowlarr 里的索引器要先配好、能正常搜。剩下的只是这里填地址和 Key 而已。

---

## SubtitleHunter

### 它解决什么问题

字幕这事通常没人愿意手动管。要么媒体库里早就挂着正确的中文字幕，要么是英文资源进来之后什么也没有，得现找现压。

SubtitleHunter 接管入库完成（`transfer.complete`）那一刻，对整理后的目标目录跑一遍字幕工作流：先看有没有中文字幕，没有就抽英文文本字幕、再翻译成中文写到同目录。整个过程只在外挂文件上动手，不动 MKV / MP4 本体，不影响 PT 做种校验。

### 能力一览

| 能力 | 做的事 |
|------|--------|
| 探测 | 用 `ffprobe` 列出外挂字幕和内嵌字幕流 |
| 提取 | 用 `ffmpeg` 把内嵌文本字幕抽成外挂 `.srt` / `.ass` |
| 规范命名 | 按 Jellyfin / Emby 习惯改名，如 `.zh-Hans.srt`、`.en.forced.srt`、`.zh-Hans.ai.srt` |
| AI 翻译 | 没有 Chinese 字幕时，取英文文本字幕翻译成中文 |
| 确保中文 | 上面几步的编排入口，确保每个视频旁都有一个可用的中文字幕 |

翻译部分细节：

- **模型来源二选一**。默认复用 MoviePilot 系统智能助手配置，也支持自定义 OpenAI 兼容 `/chat/completions` 接口（Base URL / Key / 模型名 / 超时 / 代理）。
- **三种档位**：`fast`（一段，最快）、`standard`（两段，均衡）、`quality`（三段，默认，质量最高）。
- **批次并发**：默认每批 60 条 / 9000 字符，并发 5 批。并发别一上来就拉满——多数 OpenAI 兼容网关扛不住高 RPM，429 / 503 / JSON 损坏只会更慢。建议先 3–6，稳定了再往上加。
- **翻译缓存**：按模型、影片上下文、术语表、批次、阶段、档位联合 key 存，中断后续跑能接着上次的进度。缓存放插件数据目录的 `SubtitleHunter/translation_cache/`，不写进媒体库，不缓存 API Key。
- **术语表**：可选，每行一条 `原文=译文`，不维护就留空。

约束：

- 只翻译文本字幕：`.srt` / `.ass` / `.ssa`。PGS / VobSub 等**图形字幕**能识别但没法直接翻，要 OCR。
- 环境里要能跑 `ffprobe` 和 `ffmpeg`。
- 翻译接口返回内容必须可解析为 JSON 数组，插件按所选档位并发调用模型。

### 配置项

| 参数 | 说明 |
|------|------|
| 启用插件 | 总开关 |
| 入库后自动确保中文 | 监听 `transfer.complete`，对整理后的媒体库路径跑工作流 |
| 规范化外挂字幕命名 | 把同目录字幕改成媒体服务器更友好的命名 |
| 提取内嵌中文字幕 | 已有内嵌中文就抽成外挂并跳过翻译 |
| 启用 AI 翻译 | 没中文时调模型翻英文文本字幕 |
| 模型来源 | 系统智能助手（默认）或自定义 OpenAI 兼容 API |
| 自定义 Base URL / Key / 模型 | 仅自定义来源生效，默认 `https://api.openai.com/v1` |
| 自定义 API 使用代理 | 自定义来源是否走 MoviePilot 代理；系统来源按系统 `LLM_USE_PROXY` |
| API 重试次数 | 默认 3 |
| 翻译档位 | `fast` / `standard` / `quality`，默认 `quality` |
| 并发批次数 | 默认 5 |
| 每批字幕条数 / 每批最大字符 | 默认 60 / 9000 |
| 启用翻译缓存 | 默认开 |
| 术语表 | 可选，每行一条 `原文=译文` |

### HTTP API

| 路径 | 方法 | 作用 |
|------|------|------|
| `/status` | GET | 看最近一次运行状态 |
| `/list?path=<dir or video>` | GET | 列出该目录或视频的字幕 |
| `/ensure` | POST | 异步跑"确保中文"工作流，`{"path": "<dir or video>"}` |
| `/extract` | POST | 提取指定视频的内嵌文本字幕，`{"path": "<video>", "stream_index": <n>}` |

### 速度建议

- **电影**：`quality`、并发 5、每批 60 / 9000 字符。比旧版 20 条串行请求数更少，还会并发跑。
- **剧集批量**：`standard`、并发 5–8、每批 60–80 / 9000–12000 字符。
- **先快速补档再看**：`fast`，先有可看的中文字幕，质量略低于三段但快。

---

## 安装

在 MoviePilot 设置 → 插件 → 第三方插件仓库里加上本仓库地址，然后到插件市场里装 **ProwlarrExtend** 和/或 **SubtitleHunter**。

升级走 MoviePilot 的热加载：重启容器时会从 GitHub 拉最新代码，无需手动操作。

## 版本

当前两个插件都是 v2.3。完整历史见 [`package.v2.json`](./package.v2.json)。

## 来源

Fork 自 [jtcymc/MoviePilot-PluginsV2](https://github.com/jtcymc/MoviePilot-PluginsV2)（上游已停止维护）。本仓库在原 ProwlarrExtend 的基础上继续维护，并新增了 SubtitleHunter；ProwlarrExtend 的定时任务、站点同步、启用状态同步、搜索字段映射等核心逻辑都重写过。

## 开发文档

- [升级计划](./PLAN.md)
- [长期维护策略](./MAINTENANCE.md)

## License

MIT