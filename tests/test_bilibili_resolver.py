"""test_bilibili_resolver — B 站平台特定 resolver 扩展样板测试.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.
phase: 2026-09-04 Phase 7+ 扩展样板.

设计要点:
  - BilibiliResolver 继承 VideoResolver, parse() 拿父类 items 后加 is_bilibili meta.
  - fetch() 透传父类, 不特殊化 (cookie_file 走父类).
  - 测试沿用 test_resource_fetcher_resolvers.py 的 monkeypatch pattern.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from multimedia_parsing.resource_fetcher.resolvers.bilibili import BilibiliResolver
from multimedia_parsing.resource_fetcher.resolvers.video import VideoResolver


# ---------------------------------------------------------------------------
# Fakes — 跟 test_resource_fetcher_resolvers.py 风格一致
# ---------------------------------------------------------------------------


class _FakeResolvedVideo:
    def __init__(
        self,
        platform: str = "bilibili",
        video_id: str = "BV1xx",
        title: str = "测试视频",
        duration: float = 120.0,
        extractor: str = "BiliBili",
    ):
        self.platform = platform
        self.video_id = video_id
        self.title = title
        self.duration = duration
        self.extractor = extractor


class _FakeDownloadedVideo:
    def __init__(self, file_path: Path = Path("/tmp/bilibili_BV1xx.mp4"), ext: str = "mp4"):
        self.file_path = file_path
        self.ext = ext


class _FakeOssResult:
    def __init__(self, url: str = "https://oss.example.com/bilibili_BV1xx.mp4"):
        self.url = url


def _install_fakes(monkeypatch, resolved: List[_FakeResolvedVideo] | None = None):
    """monkeypatch 父类 VideoResolver 的 _resolve_video + _download_video + _OssUploader.

    同事扩展其他平台时复用这个 helper — 改 base 即可.
    """
    import multimedia_parsing.resource_fetcher.resolvers.video as vmod

    if resolved is None:
        resolved = [_FakeResolvedVideo()]

    def fake_resolve(url, cookie_file=None):
        if not resolved:
            raise RuntimeError("no resolved items")
        return resolved[0]

    def fake_download(url, out_dir, platform, video_id, cookie_file=None):
        return _FakeDownloadedVideo(
            file_path=out_dir / f"{platform}_{video_id}.mp4",
            ext="mp4",
        )

    def fake_oss_upload(self, local_path, platform, video_id, ext):
        return _FakeOssResult(url=f"https://oss.example.com/{platform}_{video_id}.{ext}")

    monkeypatch.setattr(vmod, "_resolve_video", fake_resolve)
    monkeypatch.setattr(vmod, "_download_video", fake_download)
    monkeypatch.setattr(vmod, "_OssUploader", fake_oss_upload)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_bilibili_resolver_is_subclass_of_video_resolver():
    """扩展样板: BilibiliResolver 继承 VideoResolver (满足 isinstance 父类)."""
    resolver = BilibiliResolver()
    assert isinstance(resolver, VideoResolver)
    assert isinstance(resolver, BilibiliResolver)


def test_bilibili_parse_adds_is_bilibili_meta(monkeypatch):
    """parse() 拿父类 items, 给 bilibili platform 加 is_bilibili=True meta."""
    _install_fakes(monkeypatch, resolved=[_FakeResolvedVideo(platform="bilibili")])

    items = BilibiliResolver().parse("https://www.bilibili.com/video/BV1xx")
    assert len(items) == 1
    assert items[0].resource_type == "video"
    assert items[0].platform == "bilibili"
    assert items[0].meta.get("is_bilibili") is True


def test_bilibili_parse_passes_cookie_file_to_parent(monkeypatch):
    """cookie_file 透传给父类 VideoResolver.parse() (走 yt-dlp)."""
    captured: dict = {}

    def fake_resolve(url, cookie_file=None):
        captured["cookie_file"] = cookie_file
        return _FakeResolvedVideo()

    import multimedia_parsing.resource_fetcher.resolvers.video as vmod

    monkeypatch.setattr(vmod, "_resolve_video", fake_resolve)
    monkeypatch.setattr(vmod, "_download_video", lambda *a, **kw: _FakeDownloadedVideo())
    monkeypatch.setattr(vmod, "_OssUploader", lambda *a, **kw: _FakeOssResult())

    resolver = BilibiliResolver(cookie_file=Path("/tmp/cookies.txt"))
    resolver.parse("https://www.bilibili.com/video/BV1xx")
    # Windows str(Path) 走 \\, Linux 走 /. 用 as_posix() 拿 /-风格
    assert captured["cookie_file"] is not None
    assert captured["cookie_file"].endswith("cookies.txt")
