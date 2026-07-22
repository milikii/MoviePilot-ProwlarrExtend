# 长期维护策略

## 兼容性跟踪

MoviePilot 更新频繁（约每周 1-2 个版本）。本插件依赖的内部 API：

| 依赖项 | 当前状态 | 风险 |
|--------|---------|------|
| `_PluginBase` 基类 | 稳定，极少破坏性变更 | 低 |
| `get_module()` → `search_torrents` | 核心搜索机制，不太可能移除 | 低 |
| `SitesHelper.add_indexer()` | 官方 customindexer 插件也在用 | 低 |
| `TorrentInfo` dataclass | 只会新增字段，旧字段不会删除 | 低 |
| `RequestUtils` | 内部工具类，签名可能变化 | 中 |
| `get_service()` 注册机制 | 新版推荐方式，将持续存在 | 低 |

## 版本策略

- 主版本号跟随 MoviePilot 大版本（v2.x 对应 MP v2.x）
- 次版本号表示功能变更
- package.v2.json 中的 version 字段必须与代码中的 `plugin_version` 一致

## 测试方法

插件无法脱离 MoviePilot 运行（依赖其内部模块）。测试策略：

1. **本地/CI 单测**：`python3 -m pytest tests/ -q` + `ruff check plugins.v2/ tests/`（GitHub Actions 见 `.github/workflows/ci.yml`）
2. **契约测试**：`plugin_version` 必须与 `package.v2.json` 的 version 一致；Prowlarr 字段映射与字幕解析有 fixture/契约用例
3. **集成测试**：在实际 MoviePilot 容器中加载插件，验证索引器列表同步和搜索功能
4. **版本升级测试**：MoviePilot 每次升级后，检查插件日志是否有 import 错误或方法签名不匹配

## 发布流程

1. 修改代码 + 更新 `plugin_version` 和 `package.v2.json`
2. 提交到 main 分支
3. MoviePilot 插件系统直接从 GitHub 仓库的 `package.v2.json` 和 `plugins.v2/` 目录加载，无需额外发布步骤

## 上游同步

原仓库 `jtcymc/MoviePilot-PluginsV2` 如有更新，评估是否 cherry-pick。但鉴于原仓库已停止维护，预期不会有新提交。

## CLAUDE.md 约定

本项目在 `.claude/` 目录下维护 CLAUDE.md，供后续 Claude Code 会话使用。包含：
- 项目结构说明
- MoviePilot 插件 API 约定
- 测试验证步骤
