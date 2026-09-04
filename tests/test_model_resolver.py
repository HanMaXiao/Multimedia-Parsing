"""test_model_resolver — 模型资源解析器 stub 测试 (D2 决策未实现).

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.
phase: 2026-09-04 Phase 7+ model stub (同事未来扩展 huggingface / civitai).

设计要点:
  - ModelResolver.parse() 返空 list (未实现, 不抛错).
  - ModelResolver.fetch() 返 error (not_implemented) — 提示同事用 resolver-extension skill.
  - fetch_wrong_type 返 error.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from multimedia_parsing.resource_fetcher.resolvers.model import ModelResolver
from multimedia_parsing.resource_fetcher.base import (
    FetchDestination,
    FetchResult,
    ResourceItem,
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_model_parse_returns_empty_list():
    """parse() stub → 返空 list (D2 决策, 同事实现 huggingface/civitai 后这里会有 item)."""
    items = ModelResolver().parse("https://huggingface.co/bert-base-uncased")
    assert items == []


def test_model_fetch_returns_not_implemented_error(tmp_path: Path):
    """fetch() stub → 返 not_implemented error, 不抛异常."""
    item = ResourceItem(
        item_id="huggingface_bert-base-uncased",
        resource_type="model",
        platform="huggingface",
        source_url="https://huggingface.co/bert-base-uncased",
        meta={"model_id": "bert-base-uncased"},
    )
    dest = FetchDestination(mode="local", download_dir=tmp_path)
    result = ModelResolver().fetch(item, dest)

    assert result.is_success is False
    assert "stub" in result.error.lower() or "not_implemented" in result.error.lower()
    # 必须提示同事用 skill 扩展
    assert "resolver-extension" in result.error or "skill" in result.error.lower()


def test_model_fetch_wrong_resource_type_returns_error(tmp_path: Path):
    """fetch() 收到非 model item → 返 error."""
    bad_item = ResourceItem(
        item_id="x_1",
        resource_type="video",  # 不是 model
        platform="huggingface",
        source_url="https://huggingface.co/foo",
    )
    dest = FetchDestination(mode="local", download_dir=tmp_path)
    result = ModelResolver().fetch(bad_item, dest)

    assert result.is_success is False
    assert "ModelResolver cannot fetch" in result.error
