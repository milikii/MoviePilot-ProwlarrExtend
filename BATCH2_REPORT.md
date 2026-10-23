# 第二批报告：SubtitleHunter v3 迁移 + 字幕正确性修复

日期：2026-10-23。任务书：`BATCH2_BRIEF.md`；宿主契约：`HOST_V3_FACTS.md` 第二批一节（NAS MoviePilot v3.1.9 双路实测）。

分工说明：机械移植（v2 整体复制、版本 3.0.0、package 条目）与终审收尾由助手完成；7 项正确性修复与 v3 新测试主体由 Codex（AgentRouter gpt-6.1-sol）完成——其运行两次被上游流断开/加密内容校验错误打断，最终 4 处缺陷由助手在终审中定位并修复（见「终审修复」）。本报告由助手撰写。

## 移植

- `plugins.v3/subtitlehunter/`：v2 的 15 个模块整体移植，模块划分与 mixin MRO 不变（记录 MRO 的注释保留）；`plugin_version = "3.0.0"`。
- 用户面保持不变：类名、`plugin_config_prefix = "subtitle_hunter_"`、全部配置键、插件 API、计划任务、通知行为。新增一个配置键 `api_stage_budget`（每阶段总预算秒数，默认 120，范围 1–300，配置页有对应字段）。
- `package.v3.json` 增加 SubtitleHunter 3.0.0 条目（含 history）。v2 代码、`package.v2.json` 一行未动。

## 7 项正确性修复（对应任务书）

1. **SRT 解析不再静默丢条**（formats.py）：时轴格式规范化；坏块进 diagnostics（块号、原文片段、原因）并使解析失败而非跳过；解码链 BOM→UTF-8 严格→回退编码并记录实际编码。
2. **cue 唯一 ID**（formats.py/models.py/quality.py）：解析即分配 `cue_id`（与印刷序号分离），翻译映射、缓存、校验全用 cue_id；重复印刷序号的两条 cue 独立翻译、独立回填（有专项测试）。
3. **逐条覆盖校验**（quality.py `validate_translated_cues`）：删除 50% 放行；每条源 cue 必须有译文或显式豁免归类（sound_effect / symbols_or_music / empty / already_chinese，均计数并写入 diagnostics），未归类缺失即整文件失败；同时校验时轴未被改动、cue ID 集合与顺序。
4. **forced 收紧**（media_ops.py `_forced_modifier`）：只看字幕文件名相对视频名的修饰段与 ffprobe disposition，不再对完整路径/片名任意子串匹配。
5. **标签保全**（formats.py `mask_tags`/`restore_tags`）：翻译/压缩全程用 `[[SH_TAG_nnn]]` 占位符；回填时数量/顺序严格校验，占位符全丢时仅允许首尾标签按位置恢复，中途标签丢失直接失败；成品落盘前还会回解析复验（含标签与时轴）。
6. **失败原子性**（translate_ops.py）：成品先写同目录临时文件、fsync、回解析复验通过后才发布；任何失败保留原文件与既有输出不变（有专项测试比对字节）。
7. **重试分类与总预算**（translate_ops.py）：401/403/400 立即失败；429 尊重 Retry-After；5xx/网络指数退避；每阶段总截止 `api_stage_budget`（默认 120s）贯穿请求与退避等待，替代原最坏约 58 分钟/阶段；错误信息携带 HTTP 状态与响应摘要，不再吞状态码。取消标志贯穿各阶段（含压缩与回验前）。

## 终审修复（助手，Codex 产出后）

1. formats.py 块分割正则 `\n[ \t]*\n` 在「空文本 cue」后会把下一块的首换行留在块内，导致下一条被误报坏块——改为 `(?:\n[ \t]*){2,}` 整段消费分隔空行（由豁免分类测试暴露）。
2. v3 测试 `test_lost_middle_tags_in_compression_fail` 的夹具译文实际只有 14 个汉字，未达 16 字/行与 CPS 阈值，压缩阶段根本不触发——夹具改成 21 字使压缩真实发生（插件侧 restore_tags 对中途标签丢失本就正确抛错）。
3. v3 测试误用不存在的 `_config_dict()`——改用真实 API `_current_config()`；并修正 get_form 返回的是当前配置而非出厂默认的断言（另以 init 空配置断言出厂默认 120）。
4. 第一批测试 `test_package_v3_matches_plugin_version` 的包内容断言按第二批计划更新为 {ProwlarrExtend, SubtitleHunter}。

## 测试

`python3 -m unittest discover -s tests`：共 94 个用例，全部真实用例通过。其中 v3 新测试 486 行（SRT 变体/坏块诊断、重复序号隔离、覆盖失败保原文件、forced 正反例、标签保留与丢失失败、重试分类与总预算假时钟、取消贯穿、MRO/配置/表单契约等），并通过 LegacyV2Tests 适配器在 unittest 下实际执行 v2 原测试函数的断言。

预存环境问题（与本批无关）：`tests/test_subtitlehunter.py` 与 `tests/test_contracts.py` 在模块顶层 `import pytest`，本机无 pytest，这两个模块在 discover 时报加载错误；两文件未被本批改动，v2 的断言已由上述适配器覆盖执行。另新增 `tests/__init__.py` 以支持模块方式运行。

## 真机验证结果（2026-10-23，NAS MoviePilot v3.1.9）

- **加载与注册**：经 local-repo 安装 + UserInstalledPlugins 注册后加载成功，6 条 API 路由（status/list/ensure/extract/test/cancel）注册，事件订阅 transfer.complete 生效。user.db 中无 v2 遗留配置（全新写入 enabled）。
- **全流程跑通**：NAS 本机起 mock OpenAI 兼容服务（假 LLM），ffmpeg 生成 3 秒测试片 + 外挂 en.srt（3 条：普通台词、带 `<i>` 标签台词、`[music]`），经 /ensure 触发任务：状态「完成」，translated=1、failed=0。产物 `Test Movie (2024).zh-Hans.ai.srt` 内容正确：`Hello World→你好 世界`；`This is a <i>test</i>→这是 <i>测试</i>`（标签保全）；`[music]` 原样保留；分类计数 `{needs_translation: 2, sound_effect: 1}`；解码识别 utf-8。
- **失败路径**：先行误写的 0 字节字幕触发同一任务时，状态报「部分失败」并优雅收尾（未崩溃、未产出坏文件），与失败原子性设计一致。
- **容器重启存活**：`docker restart` 后两插件均恢复——ProwlarrExtend 3.0.1、SubtitleHunter 3.0.0，state=True。注意：启动插件同步耗时约 57 秒，同步未完成时插件列表会短暂显示旧目录条目（曾观测到 SubtitleHunter 2.19 / ProwlarrExtend 3.0.0、state=False），属中间态，勿据此误判版本回退。
- **运维事实**：运行中改插件配置必须走 `PUT /api/v1/plugin/{plugin_id}`（直改 user.db 后 reload 不重读配置，运行实例保持旧值）；第一批 ProwlarrExtend 的直写 DB 之所以生效，是因为写在首次加载之前。
- **兼容提示**：宿主对 `app.schemas.types.NotificationType` 旧路径给出兼容映射警告（映射到 MessageType）；宿主自带 v3 插件 autosubv2 同样使用该路径，功能无碍，本批未改。
- 验收后已恢复配置为 enabled-only、删除测试媒体、停掉 mock 服务。
