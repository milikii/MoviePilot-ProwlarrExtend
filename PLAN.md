# 升级计划：v1.3 → v2.0

目标：适配 MoviePilot v2.14.0，修复已知缺陷，保持向后兼容。

## Phase 1：核心兼容性修复

### 1.1 TorrentInfo 字段补全

Prowlarr Search API 返回的字段远比当前取用的多。对照 v2.14.0 的 TorrentInfo dataclass，需要补充映射：

| TorrentInfo 字段 | Prowlarr API 字段 | 说明 |
|-----------------|------------------|------|
| peers | `leechers` | 下载者数 |
| grabs | `grabs` | 完成数 |
| downloadvolumefactor | — | 根据 categories 推断，默认 1.0 |
| uploadvolumefactor | — | 默认 1.0 |
| labels | `categories[].name` | 分类标签 |
| category | 按 categories 推断 | "电影" / "电视剧" |
| site_name | — | 填入索引器名称 |

### 1.2 代理真正生效

```python
# 旧代码
RequestUtils(headers=headers).get_res(url)

# 新代码
RequestUtils(headers=headers, proxies=settings.PROXY if self._proxy else None).get_res(url)
```

### 1.3 用 get_service() 替代手动 Scheduler

新版 MoviePilot 推荐通过 `get_service()` 注册定时任务，框架统一管理生命周期：

```python
def get_service(self) -> List[Dict[str, Any]]:
    if self._enabled and self._cron:
        return [{
            "id": "ProwlarrExtendRefresh",
            "name": "Prowlarr 索引刷新",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.get_status,
            "kwargs": {}
        }]
    return []
```

移除 `init_plugin` 中的 BackgroundScheduler 手动管理代码。

### 1.4 get_api() 返回空列表而非 None

```python
def get_api(self) -> List[Dict[str, Any]]:
    return []
```

## Phase 2：功能增强

### 2.1 索引器选择性启用

当前行为：Prowlarr 中所有索引器全部桥接。  
改进：在配置页面增加多选框，允许用户选择只桥接哪些索引器。

### 2.2 搜索结果质量提升

- 使用 Prowlarr `/api/v1/indexer` 接口获取索引器详细信息（名称、协议、是否启用）
- 过滤掉 Prowlarr 中已禁用的索引器
- 在搜索结果中填充 `site_name` 便于在 UI 上区分来源

### 2.3 连通性测试 API

提供一个测试接口，用户配置保存后可以点击"测试"确认 Prowlarr 连接正常：

```python
def get_api(self) -> List[Dict[str, Any]]:
    return [{
        "path": "/test",
        "endpoint": self.test_connection,
        "methods": ["GET"],
        "auth": "apikey",
        "summary": "测试 Prowlarr 连接"
    }]
```

## Phase 3：代码质量

### 3.1 修复类属性可变默认值

```python
# 旧
_indexers = []

# 新：移到 __init__ 或 init_plugin 中
def init_plugin(self, config: dict = None):
    self._indexers = []
    ...
```

### 3.2 版本号和元数据更新

- plugin_version → "2.0"
- plugin_author → 新维护者
- author_url → 新仓库地址
- package.v2.json 同步更新 history

## 实施顺序

1. Phase 1 全部完成 → 发布 v2.0-rc1，在容器中实测
2. Phase 2 按需逐步实施 → v2.1, v2.2 ...
3. Phase 3 随时可做，不影响功能
