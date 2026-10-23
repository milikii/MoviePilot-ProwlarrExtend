# _*_ coding: utf-8 _*_
"""Pure helpers for Prowlarr indexer multi-select and catalog."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


def normalize_selected_indexers(value: Any, plugin_name: str = "ProwlarrExtend") -> List[str]:
    """Normalize multi-select config into digit indexer id strings."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw_items = [part.strip() for part in value.replace("，", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]

    prefix = f"{plugin_name}-"
    selected: List[str] = []
    seen = set()
    for item in raw_items:
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        if text.startswith(prefix):
            text = text[len(prefix):]
        if text.startswith("prowlarr-"):
            text = text.replace("prowlarr-", "", 1).split(".", 1)[0]
        if not text.isdigit():
            continue
        if text in seen:
            continue
        seen.add(text)
        selected.append(text)
    return selected


def normalize_indexer_catalog(value: Any) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    catalog: List[Dict[str, str]] = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        indexer_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not indexer_id.isdigit() or not name or indexer_id in seen:
            continue
        seen.add(indexer_id)
        catalog.append({"id": indexer_id, "name": name})
    return catalog


def clean_indexer_name(raw_name: str, plugin_name: str, fallback: str = "") -> str:
    text = str(raw_name or "")
    cleaned = text.replace(f"{plugin_name}-", "", 1) if text else ""
    return cleaned or text or fallback


def catalog_from_indexers(
    indexers: Optional[List[Dict[str, Any]]],
    get_indexer_id: Callable[[Dict[str, Any]], str],
    plugin_name: str,
) -> List[Dict[str, str]]:
    catalog: List[Dict[str, str]] = []
    for indexer in indexers or []:
        indexer_id = get_indexer_id(indexer)
        if not indexer_id:
            continue
        name = clean_indexer_name(str(indexer.get("name") or ""), plugin_name, indexer_id)
        catalog.append({"id": indexer_id, "name": name})
    return catalog


def apply_indexer_selection(
    indexers: List[Dict[str, Any]],
    selected_ids: Optional[List[str]],
    get_indexer_id: Callable[[Dict[str, Any]], str],
) -> List[Dict[str, Any]]:
    """Empty selected_ids means bridge all eligible indexers."""
    if not indexers:
        return []
    selected = set(selected_ids or [])
    if not selected:
        return list(indexers)
    return [
        indexer
        for indexer in indexers
        if get_indexer_id(indexer) in selected
    ]


def indexer_select_items(catalog: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    return [
        {"title": f"{item['name']} (#{item['id']})", "value": item["id"]}
        for item in (catalog or [])
        if item.get("id") and item.get("name")
    ]
