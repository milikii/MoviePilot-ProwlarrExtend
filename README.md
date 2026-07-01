# MoviePilot-ProwlarrExtend

MoviePilot V2 插件：通过 Prowlarr 扩展 MoviePilot 的索引搜索能力，支持 PTP、BTN、BHD 等国际私有站点。

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
| 更新周期 | 索引器列表同步周期，cron 表达式，默认每 24 小时 |

## 当前版本状态

v1.3 — 原始版本，适配 MoviePilot v2.6.8。存在以下已知问题：

1. TorrentInfo 字段不完整（缺少 peers/grabs/downloadvolumefactor/uploadvolumefactor/labels/category）
2. 手动管理 BackgroundScheduler，未使用新版推荐的 `get_service()` 机制
3. 代理开关有 UI 但 RequestUtils 调用时未传入 proxy 参数（代理实际不生效）
4. `get_api()` 缺少新版要求的 `auth` 字段
5. 类属性 `_indexers` 作为可变列表在类级别定义，多实例场景可能共享状态

## 升级计划 → v2.0

详见 [PLAN.md](./PLAN.md)

## 长期维护策略

详见 [MAINTENANCE.md](./MAINTENANCE.md)

## License

MIT
