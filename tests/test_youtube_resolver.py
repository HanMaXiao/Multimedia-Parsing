"""test_youtube_resolver — YouTube 平台特定 resolver 扩展样板测试.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.
phase: 2026-09-04 Phase 7+ 扩展样板.

设计要点:
  - YouTubeResolver 继承 VideoResolver, parse() 拿父类 items 后加 is_youtube meta.
  - 跟 BilibiliResolver 同结构, 但识别 platform in ('youtube', 'yt') (yt-dlp 两种命名).
  - cookie_file 强烈推荐 (YouTube 强反爬).
"""
from __future__ import annotations

import pytest

from multimedia_parsing.resource_fetcher.resolvers.video import VideoResolver
from multimedia_parsing.resource_fetcher.resolvers.youtube import YouTubeResolver


class _FakeResolvedVideo:
    def __init__(
        self,
        platform: str = "youtube",
        video_id: str = "dQw4w9WgXcQ",
        title: str = "测试视频",
        duration: float = 213.0,
        extractor: str = "youtube",
    ):
        self.platform = platform
        self.video_id = video_id
        self.title = title
        self.duration = duration
        self.extractor = extractor


class _FakeDownloadedVideo:
    def __init__(self):
        from pathlib import Path
        self.file_path = Path("/tmp/youtube_dQw4w9WgXcQ.mp4")
        self.ext = "mp4"


class _FakeOssResult:
    def __init__(self):
        self.url = "https://oss.example.com/youtube_dQw4w9WgXcQ.mp4"


def _install_fakes(monkeypatch, resolved: list | None = None):
    import multimedia_parsing.resource_fetcher.resolvers.video as vmod

    if resolved is None:
        resolved = [_FakeResolvedVideo()]

    def fake_resolve(url, cookie_file=None):
        if not resolved:
            raise RuntimeError("no resolved items")
        return resolved[0]

    def fake_download(*args, **kwargs):
        return _FakeDownloadedVideo()

    def fake_oss_upload(self, local_path, platform, video_id, ext):
        return _FakeOssResult()

    monkeypatch.setattr(vmod, "_resolve_video", fake_resolve)
    monkeypatch.setattr(vmod, "_download_video", fake_download)
    monkeypatch.setattr(vmod, "_OssUploader", fake_oss_upload)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_youtube_resolver_is_subclass_of_video_resolver():
    """扩展样板: YouTubeResolver 继承 VideoResolver."""
    resolver = YouTubeResolver()
    assert isinstance(resolver, VideoResolver)
    assert isinstance(resolver, YouTubeResolver)


def test_youtube_parse_adds_is_youtube_meta(monkeypatch):
    """parse() 拿父类 items, 给 youtube platform 加 is_youtube=True meta."""
    _install_fakes(monkeypatch, resolved=[_FakeResolvedVideo(platform="youtube")])

    items = YouTubeResolver().parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert len(items) == 1
    assert items[0].resource_type == "video"
    assert items[0].platform == "youtube"
    assert items[0].meta.get("is_youtube") is True


def test_youtube_parse_recognizes_short_link_platform_yt(monkeypatch):
    """yt-dlp 对 youtu.be 短链可能返回 platform='yt' (历史 extractor 命名)."""
    _install_fakes(monkeypatch, resolved=[_FakeResolvedVideo(platform="yt")])

    items = YouTubeResolver().parse("https://youtu.be/dQw4w9WgXcQ")
    assert len(items) == 1
    assert items[0].meta.get("is_youtube") is True
