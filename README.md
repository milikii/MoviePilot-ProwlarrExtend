# MoviePilot-ProwlarrExtend

MoviePilot V2 插件：通过 Prowlarr 扩展 MoviePilot 的索引搜索能力，支持 PTP、BTN、BHD 等国际私有站点。

本仓库同时包含 **SubtitleHunter**，用于入库后自动检测、提取、翻译并规范化字幕。

## SubtitleHunter

SubtitleHunter v2.3 已从“全站搜索中文字幕”重构为入库后的字幕处理工作流：

| 功能 | 说明 |
|------|------|
| 提取字幕 | 使用 `ffprobe` 探测视频内嵌字幕流，使用 `ffmpeg` 将文本字幕提取为外挂 `.srt` / `.ass` |
| 翻译字幕 | 默认复用 MoviePilot 系统智能助手 LLM，也可使用自定义 OpenAI 兼容 Chat Completions API；支持批次并发和三种模式：一段快速、两段标准、三段影院质量 |
| 列出字幕 | API 扫描指定目录或视频，返回外挂字幕和内嵌字幕流 |
| 重命名字幕 | 按 Jellyfin / Emby 常见命名规范生成 `.zh-Hans.srt`、`.en.forced.srt`、`.zh-Hans.ai.srt` |
| 确保中文字幕 | 入库完成后检测中文；没有中文则提取英文文本字幕，再翻译为同目录外挂中文字幕 |

### SubtitleHunter 配置

| 参数 | 说明 |
|------|------|
| 启用插件 | 是否监听 MoviePilot 入库完成事件 |
| 入库后自动确保中文 | 开启后在 `transfer.complete` 事件后处理硬链接后的媒体库路径 |
| 规范化外挂字幕命名 | 将同目录字幕改为媒体服务器更容易识别的命名 |
| 提取内嵌中文字幕 | 如果视频已有内嵌中文字幕，提取为外挂字幕后跳过翻译 |
| 启用 AI 翻译 | 没有中文字幕时，调用 OpenAI 兼容 API 翻译英文文本字幕 |
| 模型来源 | 默认复用 MoviePilot 系统智能助手配置；也可以切换为自定义 OpenAI 兼容 API |
| 自定义 API Base URL / Key / 模型名 | 仅模型来源为自定义时生效，默认 Base URL 为 `https://api.openai.com/v1` |
| 自定义 API 使用代理 | 自定义模型来源时是否使用 MoviePilot 的代理配置；系统智能助手来源会跟随系统 `LLM_USE_PROXY` |
| API 重试次数 | 每个翻译阶段失败后自动重试，默认 3 次 |
| 翻译模式 | 默认 `影院质量：三段`；可切换为 `标准速度：两段` 或 `最快速度：一段` |
| 并发批次数 | 同时翻译多少个字幕批次，默认 5；建议 3-6，确认网关稳定后再提高 |
| 每批字幕条数 / 每批最大字符 | 控制单次模型请求的字幕量，默认 60 条 / 9000 字符 |
| 启用翻译缓存 | 按模型、影片上下文、术语表、字幕批次、翻译阶段和模式缓存结果，失败后可续跑 |
| 术语表 | 可选；每行一个术语映射，例如 `The Force=原力`，不维护术语库可以留空 |

### SubtitleHunter 速度建议

- 电影默认建议：`影院质量：三段`、并发 5、每批 60 条 / 9000 字符。相比旧版 20 条串行，请求数更少且会并发执行。
- 剧集批量处理建议：`标准速度：两段`、并发 5-8、每批 60-80 条 / 9000-12000 字符。
- 极速补档建议：`最快速度：一段`，适合先快速得到可看的中文字幕；质量低于三段模式。
- 不建议一开始设置 10-20 并发。多数 OpenAI 兼容网关会受 RPM / TPM / 连接稳定性限制，过高并发可能带来 429、503、断连和 JSON 损坏，最终反而更慢。

### SubtitleHunter 注意事项

- 插件只在 MoviePilot 整理后的目标目录写外挂字幕，不修改 MKV / MP4 本体，避免破坏 PT 做种校验。
- 当前只支持文本字幕翻译：`.srt`、`.ass`、`.ssa`。PGS、VobSub、DVD subtitle 等图形字幕可以被识别，但不能直接翻译，后续需要 OCR 才能处理。
- 运行环境必须能调用 `ffprobe` 和 `ffmpeg`。
- 翻译 API 使用 `/chat/completions` 兼容接口，返回内容必须可解析为 JSON 数组；插件会按所选模式对批次并发调用模型。
- 翻译缓存保存在 MoviePilot 插件数据目录的 `SubtitleHunter/translation_cache/` 下，不写入媒体库目录，不缓存 API Key。

### SubtitleHunter API

| API | 方法 | 说明 |
|-----|------|------|
| `/status` | GET | 查看最近运行状态 |
| `/list?path=/media/movie` | GET | 列出指定目录或视频的字幕 |
| `/ensure` | POST | 异步执行确保中文字幕，body: `{"path": "/media/movie"}` |
| `/extract` | POST | 提取指定视频内嵌文本字幕，body: `{"path": "/media/movie.mkv", "stream_index": 2}` |

## 来源

Fork 自 [jtcymc/MoviePilot-PluginsV2](https://github.com/jtcymc/MoviePilot-PluginsV2)（最后更新于 2025 年中，适配 MoviePilot v2.6.8）。原仓库已超过一年未维护。

## 原理

MoviePilot 内置索引器仅支持 NexusPHP 系国内站点。本插件通过 `get_module()` 挂载 `search_torrents` 方法，将 Prowlarr 已配置的索引器桥接为 MoviePilot 的"站点"，使 MoviePilot 的搜索、订阅功能可以覆盖 Prowlarr 管理的所有 Tracker（包括 Gazelle/UNIT3D/Luminance 等架构的国际站）。

## 安装

在 MoviePilot 设置 → 插件 → 第三方插件仓库中添加本仓库地址，然后在插件市场安装 **ProwlarrExtend**。

## 配置

| 参数 | 说明 |
|------|------|
| Prowlarr 地址 | 如 `http://prowlarr:9696` |
| API Key | Prowlarr → Settings → General → Security → API Key |
| 使用代理 | 是否通过 MoviePilot 配置的代理访问 Prowlarr |
| 更新周期 | 索引器列表同步周期，cron 表达式，默认每 24 小时；会同步 Prowlarr 当前启用的 Torrent 索引器，并清理 MoviePilot 中不再启用的 Prowlarr 站点 |

## 当前版本状态

当前仓库包含两个 MoviePilot V2 插件：

| 插件 | 版本 | 状态 |
|------|------|------|
| ProwlarrExtend | v2.3 | 已迁移到 `get_service()` 定时任务；会按 Prowlarr 当前启用状态同步 MoviePilot 站点和搜索范围 |
| SubtitleHunter | v2.3 | 已重构为入库后字幕处理工作流，支持字幕探测、提取、规范命名、批次并发翻译和翻译缓存 |

## 升级计划

详见 [PLAN.md](./PLAN.md)

## 长期维护策略

详见 [MAINTENANCE.md](./MAINTENANCE.md)

## License

MIT
