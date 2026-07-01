# MoviePilot-ProwlarrExtend

## 项目概述

MoviePilot V2 第三方插件，将 Prowlarr 索引器桥接到 MoviePilot 的搜索系统，使其支持 PTP/BTN/BHD 等国际私有站。

## 目录结构

```
├── package.v2.json              # 插件元数据（版本、描述、历史）
├── plugins.v2/
│   └── prowlarrextend/
│       └── __init__.py          # 插件主代码
├── README.md                    # 项目说明
├── PLAN.md                      # 升级计划
├── MAINTENANCE.md               # 维护策略
└── CLAUDE.md                    # 本文件
```

## MoviePilot 插件 API 关键点

- 继承 `app.plugins._PluginBase`
- `get_module()` 返回 `{"search_torrents": self.method}` 来挂载搜索
- `search_torrents(site, keyword, mtype, page)` 返回 `List[TorrentInfo]`
- `SitesHelper().add_indexer(domain, info_dict)` 注册虚拟站点
- `get_service()` 注册定时任务（推荐方式）
- `get_form()` 返回 Vuetify 组件配置 + 默认数据结构
- `get_page()` 返回插件详情页组件配置

## 验证步骤

1. 确保 MoviePilot 容器能访问本仓库（添加为第三方插件源）
2. 安装插件后在日志中确认无 import 错误
3. 配置 Prowlarr 地址和 API Key
4. 在 MoviePilot 搜索界面搜索关键词，确认结果包含 Prowlarr 索引器的数据
5. 检查日志中搜索请求和响应是否正常

## 开发注意事项

- SitesHelper 是 .so 编译文件，无法直接阅读源码，只能通过官方插件用法推断接口
- RequestUtils 的 proxy 参数需传入 `settings.PROXY`（dict 格式）
- TorrentInfo 是 dataclass，构造时使用关键字参数
- 插件代码热加载：MoviePilot 重启后自动从 GitHub 拉取最新代码
