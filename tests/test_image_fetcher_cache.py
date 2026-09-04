"""test_image_fetcher_cache.py — 解析结果磁盘缓存(plan followup #4)。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §9(范围外)
+ plan followup 4 收口:同 URL 二次解析直接走缓存,避免重复 HTTP + gallery-dl 调用。

设计要点:
  - 简单磁盘缓存:``{cache_dir}/{sha256(url)[:16]}.json`` 存 ``{parsed_at, images}``
  - TTL 可配置(默认 24h)— 过期返回 None,让调用方走正常解析
  - URL hash 包含完整 URL(协议+域+path+query)— 不同 query 不同缓存
  - 并发安全:同 URL 双调用 → 第二个读到 stale 不算 bug
  - 测试驱动:
    - miss → store → hit → 同一 cached 内容(without re-fetching)
    - TTL 过期 → miss
    - 不同 URL 隔离(独立 key)
    - 缓存目录不存在自动创建
    - 损坏 JSON 当 miss 处理(不抛)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from multimedia_parsing.image_fetcher.cache import (
    ImageParseCache,
    _url_hash,
)


# ---------------------------------------------------------------------------
# _url_hash 单元测试
# ---------------------------------------------------------------------------


def test_url_hash_same_url_same_hash():
    """同 URL → 同一 hash(稳定映射)。"""
    h1 = _url_hash("https://example.com/article/1")
    h2 = _url_hash("https://example.com/article/1")
    assert h1 == h2
    # 16 字符 sha256 截断
    assert len(h1) == 16


def test_url_hash_different_url_different_hash():
    h1 = _url_hash("https://example.com/article/1")
    h2 = _url_hash("https://example.com/article/2")
    assert h1 != h2


def test_url_hash_query_matters():
    """不同 query 参数(即使 path 相同)→ 不同 hash(避免缓存错乱)。"""
    h1 = _url_hash("https://example.com/img.jpg?v=1")
    h2 = _url_hash("https://example.com/img.jpg?v=2")
    assert h1 != h2


# ---------------------------------------------------------------------------
# ImageParseCache 基本操作
# ---------------------------------------------------------------------------


def test_cache_get_returns_none_on_miss(tmp_path: Path):
    """cache 目录空 / 没该 URL → 返 None(走正常解析)。"""
    cache = ImageParseCache(tmp_path / "cache", ttl_seconds=3600)
    assert cache.get("https://example.com/article/1") is None


def test_cache_set_then_get_returns_stored_data(tmp_path: Path):
    """set 后 get 拿回原数据 + 缓存文件存在。"""
    cache = ImageParseCache(tmp_path / "cache", ttl_seconds=3600)
    url = "https://example.com/article/1"
    images = [
        {"url": "https://cdn/a.jpg", "width": 100, "height": 100},
        {"url": "https://cdn/b.jpg", "width": None, "height": None},
    ]
    cache.set(url, images)
    got = cache.get(url)
    assert got is not None
    assert got == images


def test_cache_ttl_expiry_returns_none(tmp_path: Path):
    """TTL 过期 → get 返 None(走正常解析)。"""
    cache = ImageParseCache(tmp_path / "cache", ttl_seconds=1)
    url = "https://example.com/article/1"
    cache.set(url, [{"url": "x.jpg", "width": None, "height": None}])
    assert cache.get(url) is not None
    # 等 TTL 过期
    time.sleep(1.2)
    assert cache.get(url) is None


def test_cache_different_urls_isolated(tmp_path: Path):
    """不同 URL 互不污染 — A URL 的缓存不影响 B URL。"""
    cache = ImageParseCache(tmp_path / "cache", ttl_seconds=3600)
    cache.set("https://a.com/1", [{"url": "a.jpg", "width": None, "height": None}])
    cache.set("https://b.com/2", [{"url": "b.jpg", "width": None, "height": None}])
    # 各取各的
    assert cache.get("https://a.com/1")[0]["url"] == "a.jpg"
    assert cache.get("https://b.com/2")[0]["url"] == "b.jpg"


def test_cache_corrupted_json_returns_none(tmp_path: Path):
    """缓存文件损坏 → get 返 None,不抛(spec 容错:重新解析填正确缓存)。"""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    url = "https://example.com/article/1"
    cache_file = cache_dir / f"{_url_hash(url)}.json"
    cache_file.write_text("{ this is not valid json", encoding="utf-8")

    cache = ImageParseCache(cache_dir, ttl_seconds=3600)
    assert cache.get(url) is None  # 不抛


def test_cache_dir_auto_created(tmp_path: Path):
    """cache_dir 不存在 → set 时自动创建。"""
    cache = ImageParseCache(tmp_path / "nested" / "cache", ttl_seconds=3600)
    cache.set("https://example.com/1", [{"url": "x.jpg", "width": None, "height": None}])
    assert (tmp_path / "nested" / "cache").exists()


# ---------------------------------------------------------------------------
# resolver.parse_url 集成
# ---------------------------------------------------------------------------


def test_parse_url_uses_cache_on_second_call(tmp_path: Path, monkeypatch):
    """同 URL 第二次 parse_url → 走缓存,不调 gallery-dl / requests。"""
    from multimedia_parsing.image_fetcher import resolver as res_mod
    from multimedia_parsing.image_fetcher.resolver import ResolvedImage, parse_url, set_parse_cache

    gallery_dl_calls: list = []

    class _FakeExtractor:
        def __iter__(self):
            yield ("https://cdn/a.jpg", {})

    def fake_find(url):
        gallery_dl_calls.append(url)
        return _FakeExtractor()

    fake_gd = MagicMock()
    fake_gd.extractor.find = fake_find
    monkeypatch.setattr(res_mod, "gallery_dl", fake_gd)

    def fake_requests_get(url, **kwargs):
        return MagicMock(status_code=200, text="<html></html>")

    monkeypatch.setattr(res_mod.requests, "get", fake_requests_get)

    # 通过 set_parse_cache 注入(模块级 helper)
    cache = ImageParseCache(tmp_path / "cache", ttl_seconds=3600)
    set_parse_cache(cache)
    try:
        url = "https://example.com/article/1"
        # 第一次 — 应调 gallery-dl
        r1 = parse_url(url)
        assert len(gallery_dl_calls) == 1
        # 第二次同 URL — 期望走缓存,gallery_dl_calls 计数仍 1
        r2 = parse_url(url)
        assert len(gallery_dl_calls) == 1, "expected second call to use cache"
        # 两次结果一致
        assert len(r1) == len(r2) == 1
        assert r1[0].url == r2[0].url == "https://cdn/a.jpg"
    finally:
        set_parse_cache(None)

