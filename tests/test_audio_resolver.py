"""test_audio_resolver — 音频资源解析器测试.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.
phase: 2026-09-04 Phase 7+ audio 真实现.

设计要点:
  - AudioResolver.parse() 复用 video_fetcher.resolver.resolve (yt-dlp 自动选 extractor).
  - AudioResolver.fetch() 自己调 yt-dlp audio-only mode (不走 video_fetcher.downloader,
    因为那个强制 video format).
  - audio_format 默认 mp3 (走 FFmpegExtractAudio 转码).
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from multimedia_parsing.resource_fetcher.resolvers.audio import AudioResolver
from multimedia_parsing.resource_fetcher.base import (
    FetchDestination,
    FetchResult,
    ResourceItem,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResolvedAudio:
    def __init__(
        self,
        platform: str = "soundcloud",
        video_id: str = "track-123",
        title: str = "测试音频",
        duration: float = 180.0,
        extractor: str = "soundcloud",
    ):
        self.platform = platform
        self.video_id = video_id
        self.title = title
        self.duration = duration
        self.extractor = extractor


def _install_fake_resolve(monkeypatch, raises: Optional[Exception] = None,
                          resolved: List[_FakeResolvedAudio] | None = None):
    """monkeypatch _resolve_audio_meta (= video_fetcher.resolver.resolve)."""
    if raises is not None:
        def fake(url, cookie_file=None):
            raise raises
    elif resolved is None:
        resolved = [_FakeResolvedAudio()]

        def fake(url, cookie_file=None):
            if not resolved:
                raise RuntimeError("no items")
            return resolved[0]
    else:

        def fake(url, cookie_file=None):
            return resolved[0]

    import multimedia_parsing.resource_fetcher.resolvers.audio as amod
    monkeypatch.setattr(amod, "_resolve_audio_meta", fake)


# ---------------------------------------------------------------------------
# parse() tests
# ---------------------------------------------------------------------------


def test_audio_parse_success_returns_audio_item(monkeypatch):
    """parse() 成功 → 1 条 ResourceItem, resource_type='audio'."""
    _install_fake_resolve(monkeypatch, resolved=[_FakeResolvedAudio()])

    items = AudioResolver().parse("https://soundcloud.com/artist/track-123")
    assert len(items) == 1
    assert items[0].resource_type == "audio"
    assert items[0].platform == "soundcloud"
    assert items[0].item_id == "soundcloud_track-123"
    assert items[0].meta.get("audio_id") == "track-123"
    assert items[0].meta.get("audio_format") == "mp3"  # 默认


def test_audio_parse_resolver_error_returns_empty(monkeypatch):
    """parse() 失败 (ResolverError / 其他) → 返空 list, run_parse 层发 failed 事件."""
    _install_fake_resolve(monkeypatch, raises=RuntimeError("network error"))

    items = AudioResolver().parse("https://soundcloud.com/artist/track-123")
    assert items == []


# ---------------------------------------------------------------------------
# fetch() tests
# ---------------------------------------------------------------------------


def test_audio_fetch_wrong_resource_type_returns_error():
    """fetch() 收到非 audio item → 返 error, 不调 yt-dlp."""
    bad_item = ResourceItem(
        item_id="x_1",
        resource_type="video",  # 不是 audio
        platform="soundcloud",
        source_url="https://soundcloud.com/artist/track",
    )
    dest = FetchDestination(mode="local", download_dir=Path("/tmp"))
    result = AudioResolver().fetch(bad_item, dest)

    assert result.is_success is False
    assert "AudioResolver cannot fetch" in result.error


def test_audio_fetch_missing_audio_id_returns_error(tmp_path: Path):
    """fetch() item.meta 无 audio_id → 返 error."""
    bad_item = ResourceItem(
        item_id="x_1",
        resource_type="audio",
        platform="soundcloud",
        source_url="https://soundcloud.com/artist/track",
        meta={},  # 缺 audio_id
    )
    dest = FetchDestination(mode="local", download_dir=tmp_path)
    result = AudioResolver().fetch(bad_item, dest)

    assert result.is_success is False
    assert "missing audio_id" in result.error
