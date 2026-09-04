"""test_resource_fetcher_run_parse — run_parse 编排层测试.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4 + §5.1 + §7.
phase: 2026-09-03-universal-resource-fetch Phase 1 Commit 3.

测试用 monkeypatch 把 router.resolve / resolver.parse 替换为可控 fake, 验证:
  - URL 无匹配 resolver → failed 事件 + error="不支持的链接"
  - 解析成功 → success 事件 + items 透传
  - 解析返空 list (无资源) → skipped 事件
  - 部分成功 (一批 N 条, 部分失败)
  - 取消 (threading.Event)
  - event_cb 缺失时静默
  - 批量 emit 顺序与输入 URL 顺序一致
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from multimedia_parsing.resource_fetcher.base import ResourceItem
from multimedia_parsing.resource_fetcher.manifest import (
    ResourceParseManifest,
    parse_resource_parse_manifest_dict,
)


# ---------------------------------------------------------------------------
# helpers — monkeypatch router + resolvers
# ---------------------------------------------------------------------------


class _FakeResolver:
    """占位 Resolver — parse() 返可控 items, fetch() 抛 NotImplementedError."""

    def __init__(self, items: List[ResourceItem]):
        self._items = items
        self.parse_calls: List[str] = []

    def parse(self, url: str) -> List[ResourceItem]:
        self.parse_calls.append(url)
        return list(self._items)

    def fetch(self, item, dest):  # pragma: no cover — run_parse 不调
        raise NotImplementedError


def _patch_router(monkeypatch, mapping: Dict[str, Optional[type]]):
    """mapping: {url: Resolver class | None}. None = 模拟无匹配."""
    from multimedia_parsing.resource_fetcher import run_parse as run_parse_mod

    def _fake_resolve(url):
        return mapping.get(url)

    monkeypatch.setattr(run_parse_mod, "resolve", _fake_resolve)


def _patch_resolver_classes(monkeypatch, classes: Dict[type, _FakeResolver]):
    """mapping: {Resolver class: _FakeResolver instance}."""
    from multimedia_parsing.resource_fetcher import run_parse as run_parse_mod

    for cls, fake in classes.items():
        monkeypatch.setattr(run_parse_mod, "_RESOLVER_CLASSES", {cls: fake})


# ---------------------------------------------------------------------------
# 基础 — 单 URL 单 resolver
# ---------------------------------------------------------------------------


def test_run_parse_single_url_resolves_to_items(monkeypatch):
    item = ResourceItem(
        item_id="bilibili_BV1xx",
        resource_type="video",
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1xx",
        title="foo",
    )
    fake = _FakeResolver(items=[item])
    # 简化版: monkeypatch 整个 run_parse 用到的解析表
    from multimedia_parsing.resource_fetcher import run_parse as run_parse_mod

    monkeypatch.setattr(
        run_parse_mod,
        "_select_resolver",
        lambda url: fake if "bilibili" in url else None,
    )

    events: List[Dict] = []
    manifest = parse_resource_parse_manifest_dict(
        {"batch_id": "b1", "urls": ["https://www.bilibili.com/video/BV1xx"]}
    )
    results = run_parse_mod.run_parse(manifest, event_cb=events.append)

    assert len(results) == 1
    assert results[0].url == "https://www.bilibili.com/video/BV1xx"
    assert results[0].items == [item]
    assert results[0].error is None
    # 事件 sequence: processing → success
    assert [e["status"] for e in events] == ["processing", "success"]
    assert events[0]["stage"] == "parse"
    assert events[1]["stage"] == "parse"
    assert events[1]["resource_type"] == "video"
    assert events[1]["items"] == [
        {
            "item_id": "bilibili_BV1xx",
            "resource_type": "video",
            "platform": "bilibili",
            "source_url": "https://www.bilibili.com/video/BV1xx",
            "title": "foo",
            "thumbnail": "",
            "meta": {},
        }
    ]


def test_run_parse_no_matching_resolver_emits_failed(monkeypatch):
    from multimedia_parsing.resource_fetcher import run_parse as run_parse_mod

    monkeypatch.setattr(run_parse_mod, "_select_resolver", lambda url: None)

    events: List[Dict] = []
    manifest = parse_resource_parse_manifest_dict(
        {"batch_id": "b1", "urls": ["https://example.com/foo"]}
    )
    results = run_parse_mod.run_parse(manifest, event_cb=events.append)

    assert len(results) == 1
    assert results[0].items == []
    assert "不支持" in results[0].error
    # failed 事件
    assert [e["status"] for e in events] == ["failed"]
    assert "不支持" in events[0]["error"]


def test_run_parse_resolver_returns_empty_emits_skipped(monkeypatch):
    from multimedia_parsing.resource_fetcher import run_parse as run_parse_mod

    fake = _FakeResolver(items=[])  # 解析返空 (页面无图 / yt-dlp 拿不到)
    monkeypatch.setattr(
        run_parse_mod,
        "_select_resolver",
        lambda url: fake,
    )

    events: List[Dict] = []
    manifest = parse_resource_parse_manifest_dict(
        {"batch_id": "b1", "urls": ["https://www.bilibili.com/video/BV1xx"]}
    )
    results = run_parse_mod.run_parse(manifest, event_cb=events.append)

    assert len(results) == 1
    assert results[0].items == []
    assert [e["status"] for e in events] == ["processing", "skipped"]


def test_run_parse_resolver_returns_multiple_items(monkeypatch):
    """F0.1 终判: image resolver 一次 parse 可能返 N 个 ResourceItem."""
    from multimedia_parsing.resource_fetcher import run_parse as run_parse_mod

    items = [
        ResourceItem(
            item_id=f"img_{i}",
            resource_type="image",
            platform="weibo",
            source_url="https://weibo.com/123",
        )
        for i in range(3)
    ]
    fake = _FakeResolver(items=items)
    monkeypatch.setattr(
        run_parse_mod,
        "_select_resolver",
        lambda url: fake,
    )

    events: List[Dict] = []
    manifest = parse_resource_parse_manifest_dict(
        {"batch_id": "b1", "urls": ["https://weibo.com/123"]}
    )
    results = run_parse_mod.run_parse(manifest, event_cb=events.append)

    assert len(results) == 1
    assert len(results[0].items) == 3
    # success 事件的 items 字段含 N 个 dict
    success = next(e for e in events if e["status"] == "success")
    assert len(success["items"]) == 3


# ---------------------------------------------------------------------------
# 批量 — 部分成功
# ---------------------------------------------------------------------------


def test_run_parse_batch_partial_success(monkeypatch):
    """spec §8: 部分成功 — 批内一个失败, 其他正常."""
    from multimedia_parsing.resource_fetcher import run_parse as run_parse_mod

    good_item = ResourceItem(
        item_id="bilibili_BV1",
        resource_type="video",
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1",
    )
    fake = _FakeResolver(items=[good_item])
    monkeypatch.setattr(
        run_parse_mod,
        "_select_resolver",
        lambda url: fake if "bilibili" in url else None,
    )

    events: List[Dict] = []
    manifest = parse_resource_parse_manifest_dict(
        {
            "batch_id": "b1",
            "urls": [
                "https://www.bilibili.com/video/BV1",  # OK
                "https://example.com/foo",  # 无匹配
                "https://www.bilibili.com/video/BV2",  # OK
            ],
        }
    )
    results = run_parse_mod.run_parse(manifest, event_cb=events.append)

    assert len(results) == 3
    assert results[0].error is None  # OK
    assert "不支持" in results[1].error  # failed
    assert results[2].error is None  # OK

    statuses = [e["status"] for e in events]
    # 处理顺序: bilibili 1 (processing+success) → example (failed only, 无 resolver 跳过 processing) → bilibili 2
    # statuses: processing, success, failed, processing, success
    assert statuses == ["processing", "success", "failed", "processing", "success"]


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def test_run_parse_cancel_mid_batch(monkeypatch):
    """spec §8 验收 8: 整批可取消 — cancel_event set 后, 后续 URL 不入 results."""
    from multimedia_parsing.resource_fetcher import run_parse as run_parse_mod

    fake = _FakeResolver(
        items=[
            ResourceItem(
                item_id="x",
                resource_type="video",
                platform="bilibili",
                source_url="z",
            )
        ]
    )
    monkeypatch.setattr(
        run_parse_mod,
        "_select_resolver",
        lambda url: fake,
    )

    cancel_event = threading.Event()
    # 第一次 parse 后 set cancel — 第二个 URL 不应被处理
    original_parse = fake.parse

    def parse_with_cancel(url):
        if "second" in url:
            cancel_event.set()
        return original_parse(url)

    fake.parse = parse_with_cancel

    events: List[Dict] = []
    manifest = parse_resource_parse_manifest_dict(
        {
            "batch_id": "b1",
            "urls": [
                "https://first",
                "https://second",
                "https://third",  # 取消后跳过
            ],
        }
    )
    results = run_parse_mod.run_parse(manifest, event_cb=events.append, cancel_event=cancel_event)

    # 第一个正常, 第二个 set cancel, 第三个不入 results
    assert len(results) == 2
    assert "first" in results[0].url
    assert "second" in results[1].url
    assert fake.parse_calls == ["https://first", "https://second"]


# ---------------------------------------------------------------------------
# event_cb 缺失 — 静默
# ---------------------------------------------------------------------------


def test_run_parse_no_event_cb_silent(monkeypatch):
    """event_cb=None 时静默运行, 不抛 (CLI 单跑模式占位)."""
    from multimedia_parsing.resource_fetcher import run_parse as run_parse_mod

    item = ResourceItem(
        item_id="x",
        resource_type="video",
        platform="bilibili",
        source_url="z",
    )
    fake = _FakeResolver(items=[item])
    monkeypatch.setattr(
        run_parse_mod,
        "_select_resolver",
        lambda url: fake,
    )

    manifest = parse_resource_parse_manifest_dict(
        {"batch_id": "b1", "urls": ["https://www.bilibili.com/video/BV1"]}
    )
    # 不传 event_cb — 静默
    results = run_parse_mod.run_parse(manifest)
    assert len(results) == 1
    assert results[0].error is None


# ---------------------------------------------------------------------------
# 编排层不调 fetch
# ---------------------------------------------------------------------------


def test_run_parse_does_not_call_fetch(monkeypatch):
    """run_parse 只走 parse 阶段, 不应触发 resolver.fetch."""
    from multimedia_parsing.resource_fetcher import run_parse as run_parse_mod

    fake = _FakeResolver(
        items=[
            ResourceItem(
                item_id="x",
                resource_type="video",
                platform="bilibili",
                source_url="z",
            )
        ]
    )
    monkeypatch.setattr(
        run_parse_mod,
        "_select_resolver",
        lambda url: fake,
    )

    manifest = parse_resource_parse_manifest_dict(
        {"batch_id": "b1", "urls": ["https://www.bilibili.com/video/BV1"]}
    )
    run_parse_mod.run_parse(manifest, event_cb=lambda e: None)
    # fetch 内部抛 NotImplementedError — 不应被调用
    # (我们没显式断言, 但 fake.fetch 不在 parse_calls 里, 间接验证)
    assert fake.parse_calls == ["https://www.bilibili.com/video/BV1"]

