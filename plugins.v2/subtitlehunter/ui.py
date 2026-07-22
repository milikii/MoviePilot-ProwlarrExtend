# _*_ coding: utf-8 _*_
"""UIMixin for SubtitleHunter — extracted for maintainability."""
from typing import Any, Dict, List, Tuple


class UIMixin:
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "auto_ensure", "label": "入库后自动确保中文"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "发送通知"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "schedule_cron",
                                        "label": "定时执行周期",
                                        "placeholder": "0 3 * * *",
                                        "hint": "5 位 cron；留空禁用定时；如 0 3 * * * 表示每天凌晨 3 点对媒体路径执行，多个路径会顺序处理。",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "cache_enabled",
                                        "label": "启用翻译缓存",
                                        "hint": "缓存保存在插件数据目录，用于失败后续跑",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "target_path",
                                        "label": "媒体目录或视频路径",
                                        "placeholder": "/media/Movies,/media/TV/Show.Name.2026",
                                        "hint": "支持目录或单个视频；多个路径用英文逗号分隔，会按顺序处理。也可通过 API 指定 path。",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "rename_existing", "label": "规范化外挂字幕命名"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "extract_chinese_embedded", "label": "提取内嵌中文字幕"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "overwrite", "label": "覆盖已有字幕"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "ai_enabled", "label": "启用 AI 翻译"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "model_source",
                                        "label": "模型来源",
                                        "items": [
                                            {"title": "复用系统智能助手", "value": "system"},
                                            {"title": "自定义 OpenAI API", "value": "custom"},
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_base_url",
                                        "label": "自定义 API Base URL",
                                        "placeholder": "https://api.openai.com/v1",
                                        "hint": "模型来源为自定义时生效",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_model",
                                        "label": "自定义模型名",
                                        "hint": "模型来源为自定义时生效",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_timeout",
                                        "label": "API 超时秒数",
                                        "type": "number",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "api_key",
                                        "label": "自定义 API Key",
                                        "type": "password",
                                        "hint": "模型来源为自定义时生效；复用系统智能助手时不会读取这里",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "api_use_proxy",
                                        "label": "自定义 API 使用代理",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "target_language",
                                        "label": "中文字幕语言标记",
                                        "placeholder": "zh-Hans",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "translation_suffix",
                                        "label": "翻译字幕后缀",
                                        "placeholder": "ai",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "translation_profile",
                                        "label": "翻译模式",
                                        "items": [
                                            {"title": "影院质量：三段", "value": "quality"},
                                            {"title": "标准速度：两段", "value": "standard"},
                                            {"title": "最快速度：一段", "value": "fast"},
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "parallel_batches",
                                        "label": "并发批次数",
                                        "type": "number",
                                        "hint": "建议 3-6；网关稳定且额度充足时再提高",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "batch_size",
                                        "label": "每批字幕条数",
                                        "type": "number",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "batch_chars",
                                        "label": "每批最大字符",
                                        "type": "number",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "enable_line_check",
                                        "label": "启用行长度校验",
                                        "hint": "按 Netflix 行长和 CPS 标准压缩超长译文",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "glossary",
                                        "label": "术语表（可选）",
                                        "placeholder": "不维护术语表可以留空；例如：Stark=史塔克",
                                        "rows": 4,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": "工作流：检测中文字幕；没有则提取内嵌英文文本字幕；再按所选模式并发翻译为同目录外挂中文字幕。插件只写外挂字幕，不修改视频文件。",
                                    },
                                }],
                            },
                        ],
                    },
                ],
            }
        ], self._current_config(onlyonce=False)

    def get_page(self) -> List[dict]:
        status = self._runtime_snapshot()
        alert_type = {
            "完成": "success",
            "已有中文": "success",
            "已生成中文": "success",
            "失败": "error",
            "部分失败": "warning",
            "配置错误": "warning",
            "已跳过": "info",
            "运行中": "info",
        }.get(status.get("status"), "info")

        detail_rows = self._build_table_rows([
            ("插件开关", "已启用" if status.get("enabled") else "未启用"),
            ("AI 翻译", "已启用" if status.get("ai_enabled") else "未启用"),
            ("运行状态", status.get("status", "-")),
            ("最近开始", status.get("started_at", "-")),
            ("最近结束", status.get("finished_at", "-")),
            ("耗时", status.get("duration", "-")),
            ("来源", status.get("source", "-")),
            ("媒体", status.get("media", "-")),
            ("目标", status.get("target_path", "-")),
            ("翻译模式", status.get("translation_profile", "-")),
            ("并发批次", status.get("parallel_batches", "-")),
            ("批次大小", f"{status.get('batch_size', '-')}/{status.get('batch_chars', '-')} chars"),
            ("批次进度", f"{status.get('translation_batches_done', 0)}/{status.get('translation_batches_total', 0)}"),
            ("视频数", status.get("videos", 0)),
            ("字幕数", status.get("subtitles", 0)),
            ("已处理", status.get("processed", 0)),
            ("已跳过", status.get("skipped", 0)),
            ("已提取", status.get("extracted", 0)),
            ("已翻译", status.get("translated", 0)),
            ("已重命名", status.get("renamed", 0)),
            ("失败", status.get("failed", 0)),
            ("最近视频", status.get("last_video", "-")),
            ("错误", status.get("error", "")),
        ])

        history_rows = []
        for item in status.get("history", []):
            history_rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": item.get("finished_at", "-")},
                    {"component": "td", "text": item.get("status", "-")},
                    {"component": "td", "text": item.get("source", "-")},
                    {"component": "td", "text": item.get("media", "-")},
                    {"component": "td", "text": item.get("message", "-")},
                ],
            })
        if not history_rows:
            history_rows = [{
                "component": "tr",
                "content": [
                    {
                        "component": "td",
                        "props": {"colspan": 5},
                        "text": "暂无运行记录",
                    }
                ],
            }]

        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{
                            "component": "VAlert",
                            "props": {
                                "type": alert_type,
                                "variant": "tonal",
                                "text": f"{status.get('status', '未运行')}：{status.get('message', '')}",
                            },
                        }],
                    }
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{
                            "component": "VTable",
                            "props": {"hover": True},
                            "content": [{"component": "tbody", "content": detail_rows}],
                        }],
                    }
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "text-h6 mb-2"},
                                "text": "最近记录",
                            },
                            {
                                "component": "VTable",
                                "props": {"hover": True},
                                "content": [
                                    {
                                        "component": "thead",
                                        "content": [{
                                            "component": "tr",
                                            "content": [
                                                {"component": "th", "text": "时间"},
                                                {"component": "th", "text": "状态"},
                                                {"component": "th", "text": "来源"},
                                                {"component": "th", "text": "媒体"},
                                                {"component": "th", "text": "消息"},
                                            ],
                                        }],
                                    },
                                    {"component": "tbody", "content": history_rows},
                                ],
                            },
                        ],
                    }
                ],
            },
        ]

    @staticmethod
    def _build_table_rows(items: List[Tuple[str, Any]]) -> List[dict]:
        rows = []
        for label, value in items:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "text-subtitle-2 text-no-wrap"}, "text": label},
                    {"component": "td", "text": str(value) if value is not None else ""},
                ],
            })
        return rows

