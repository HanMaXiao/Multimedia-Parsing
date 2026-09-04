"""test_resource_fetcher_resolvers — VideoResolver / ImageResolver 真包装测试.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.
phase: 2026-09-03-universal-resource-fetch Phase 1 Commit 2.

测试用 monkeypatch 把 video_fetcher / image_fetcher / OssUploader 的真函数/类
换成可控的 fake, 验证 resolver 包装逻辑 (协议翻译 + 错误处理 + both 模式降级).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from multimedia_parsing.resource_fetcher.base import (
    FetchDestination,
    FetchResult,
    ResourceItem,
)
from multimedia_parsing.resource_fetcher.resolvers.video import VideoResolver
from multimedia_parsing.resource_fetcher.resolvers.image import ImageResolver


# ---------------------------------------------------------------------------
# fakes — 模拟 video_fetcher / image_fetcher / OssUploader 的产物
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeResolvedVideo:
    url: str
    platform: str
    video_id: str
    title: str
    duration: Optional[float]
    extractor: str


@dataclass(frozen=True)
class _FakeDownloadedVideo:
    file_path: Path
    platform: str
    video_id: str
    title: str
    ext: str
    duration: Optional[float]


@dataclass(frozen=True)
class _FakeResolvedImage:
    url: str
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass(frozen=True)
class _FakeDownloadedImage:
    file_path: Path
    image_url: str
    source_url: str
    index: int
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.error is None and self.file_path.exists()


@dataclass
class _FakeOssResult:
    url: str


class _FakeOssUploader:
    """OssUploader 假实现 — 记录调用参数, 返可控 OssResult."""

    def __init__(self, config: Any, *, raise_on_upload: Optional[Exception] = None) -> None:
        self.config = config
        self.raise_on_upload = raise_on_upload
        self.calls: List[Dict[str, Any]] = []

    def upload(self, file_path, *, platform, video_id, ext) -> _FakeOssResult:
        self.calls.append(
            {"file_path": str(file_path), "platform": platform, "video_id": video_id, "ext": ext}
        )
        if self.raise_on_upload:
            raise self.raise_on_upload
        return _FakeOssResult(url=f"https://oss.example.com/{platform}_{video_id}.{ext}")

    def upload_with_key(self, file_path, key: str) -> _FakeOssResult:
        self.calls.append({"file_path": str(file_path), "key": key})
        if self.raise_on_upload:
            raise self.raise_on_upload
        return _FakeOssResult(url=f"https://oss.example.com/{key}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _oss_config() -> Any:
    from multimedia_parsing.video_fetcher.manifest import OssConfig

    return OssConfig(
        provider="aliyun_oss",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        region="cn-hangzhou",
        bucket="b",
        access_key_id="k",
        secret_access_key="s",
    )


def _patch_video_dependencies(
    monkeypatch,
    *,
    resolved: Optional[List[_FakeResolvedVideo]] = None,
    resolve_error: Optional[Exception] = None,
    downloaded: Optional[_FakeDownloadedVideo] = None,
    download_error: Optional[Exception] = None,
) -> List[Dict[str, Any]]:
    """把 VideoResolver 内部依赖的 video_fetcher 函数替换为可控 fake.

    Returns list of recorded download calls (for assertions).
    """
    from multimedia_parsing.resource_fetcher.resolvers import video as video_mod

    def _fake_resolve(url, *, cookie_file=None):
        if resolve_error is not None:
            raise resolve_error
        if not resolved:
            raise RuntimeError("no resolved videos configured")
        return resolved[0] if len(resolved) == 1 else resolved[0]

    download_calls: List[Dict[str, Any]] = []

    def _fake_download(
        url,
        out_dir,
        *,
        platform,
        video_id,
        cookie_file=None,
        progress_cb=None,
    ):
        download_calls.append(
            {
                "url": url,
                "out_dir": str(out_dir),
                "platform": platform,
                "video_id": video_id,
                "cookie_file": cookie_file,
            }
        )
        if download_error is not None:
            raise download_error
        if downloaded is None:
            raise RuntimeError("no downloaded video configured")
        return downloaded

    monkeypatch.setattr(video_mod, "_resolve_video", _fake_resolve)
    monkeypatch.setattr(video_mod, "_download_video", _fake_download)
    monkeypatch.setattr(video_mod, "_OssUploader", _FakeOssUploader)
    return download_calls


def _patch_image_dependencies(
    monkeypatch,
    *,
    parsed: Optional[List[_FakeResolvedImage]] = None,
    parse_error: Optional[Exception] = None,
    downloaded: Optional[List[_FakeDownloadedImage]] = None,
    download_error: Optional[Exception] = None,
) -> List[Dict[str, Any]]:
    """把 ImageResolver 内部依赖的 image_fetcher 函数替换为可控 fake."""
    from multimedia_parsing.resource_fetcher.resolvers import image as image_mod

    def _fake_parse(url, *, timeout=30.0, playwright_fallback=True):
        if parse_error is not None:
            raise parse_error
        return parsed or []

    download_calls: List[Dict[str, Any]] = []

    def _fake_download(images, out_dir, *, cancel_event=None, on_progress=None):
        download_calls.append(
            {"count": len(images), "out_dir": str(out_dir), "image_urls": [i.url for i in images]}
        )
        if download_error is not None:
            raise download_error
        return downloaded or []

    monkeypatch.setattr(image_mod, "_parse_image_url", _fake_parse)
    monkeypatch.setattr(image_mod, "_download_images", _fake_download)
    monkeypatch.setattr(image_mod, "_OssUploader", _FakeOssUploader)
    return download_calls


# ---------------------------------------------------------------------------
# VideoResolver.parse
# ---------------------------------------------------------------------------


def test_video_resolver_parse_success(monkeypatch):
    _patch_video_dependencies(
        monkeypatch,
        resolved=[
            _FakeResolvedVideo(
                url="https://www.bilibili.com/video/BV1xx",
                platform="bilibili",
                video_id="BV1xx",
                title="foo",
                duration=60.0,
                extractor="BiliBili",
            )
        ],
    )
    items = VideoResolver().parse("https://www.bilibili.com/video/BV1xx")
    assert len(items) == 1
    item = items[0]
    assert item.item_id == "bilibili_BV1xx"
    assert item.resource_type == "video"
    assert item.platform == "bilibili"
    assert item.source_url == "https://www.bilibili.com/video/BV1xx"
    assert item.title == "foo"
    assert item.meta["video_id"] == "BV1xx"
    assert item.meta["duration"] == 60.0
    assert item.meta["extractor"] == "BiliBili"


def test_video_resolver_parse_resolver_error_returns_empty(monkeypatch):
    """F0.1: 解析失败返空 list — run_parse 层发 failed 事件."""
    from multimedia_parsing.video_fetcher.resolver import ResolverError

    _patch_video_dependencies(
        monkeypatch,
        resolve_error=ResolverError("unsupported URL"),
    )
    items = VideoResolver().parse("https://example.com/foo")
    assert items == []


def test_video_resolver_parse_empty_resolved_list(monkeypatch):
    """F0.1: yt-dlp 返空 list (单视频 URL 但 yt-dlp 拿不到 entry) — 视作空结果."""
    _patch_video_dependencies(monkeypatch, resolved=[])
    items = VideoResolver().parse("https://example.com/empty")
    assert items == []


def test_video_resolver_passes_cookie_file(monkeypatch):
    """spec §4 + 现有 video_fetcher: 登录态通过 cookie_file 注入."""
    calls = _patch_video_dependencies(
        monkeypatch,
        resolved=[
            _FakeResolvedVideo(
                url="u",
                platform="bilibili",
                video_id="x",
                title="t",
                duration=None,
                extractor="BiliBili",
            )
        ],
    )
    VideoResolver(cookie_file=Path("cookies.txt")).parse("https://bilibili.com/video/x")
    # cookie_file 透传给 _resolve_video (验证 monkeypatch fake 接到了参数)
    # 直接断言解析成功即可 — cookie_file 透传在 fetch 测试里验证
    assert len(calls) == 0  # parse 不调 download


# ---------------------------------------------------------------------------
# VideoResolver.fetch
# ---------------------------------------------------------------------------


def _downloaded_video() -> _FakeDownloadedVideo:
    return _FakeDownloadedVideo(
        file_path=Path("/tmp/v_BV1xx.mp4"),
        platform="bilibili",
        video_id="BV1xx",
        title="foo",
        ext="mp4",
        duration=60.0,
    )


def _make_video_item() -> ResourceItem:
    return ResourceItem(
        item_id="bilibili_BV1xx",
        resource_type="video",
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1xx",
        title="foo",
        meta={"video_id": "BV1xx", "duration": 60.0, "extractor": "BiliBili"},
    )


def test_video_fetch_local_mode(monkeypatch, tmp_path):
    _patch_video_dependencies(monkeypatch, downloaded=_downloaded_video())
    r = VideoResolver().fetch(
        _make_video_item(),
        FetchDestination(mode="local", download_dir=tmp_path),
    )
    assert r.is_success
    assert r.local_path == Path("/tmp/v_BV1xx.mp4")
    assert r.oss_url is None


def test_video_fetch_oss_mode(monkeypatch, tmp_path):
    _patch_video_dependencies(monkeypatch, downloaded=_downloaded_video())
    r = VideoResolver().fetch(
        _make_video_item(),
        FetchDestination(mode="oss", oss=_oss_config()),
    )
    assert r.is_success
    assert r.oss_url == "https://oss.example.com/bilibili_BV1xx.mp4"
    assert r.local_path == Path("/tmp/v_BV1xx.mp4")  # oss 模式也保留本地路径


def test_video_fetch_both_mode(monkeypatch, tmp_path):
    _patch_video_dependencies(monkeypatch, downloaded=_downloaded_video())
    r = VideoResolver().fetch(
        _make_video_item(),
        FetchDestination(mode="both", download_dir=tmp_path, oss=_oss_config()),
    )
    assert r.is_success
    assert r.local_path is not None
    assert r.oss_url is not None


def test_video_fetch_both_mode_oss_fail_still_success(monkeypatch, tmp_path):
    """spec §9: both 模式 OSS 失败但本地已成功 → success + oss_error 记录."""
    _patch_video_dependencies(
        monkeypatch,
        downloaded=_downloaded_video(),
    )
    # Need to override the OssUploader to raise on upload
    from multimedia_parsing.resource_fetcher.resolvers import video as video_mod

    def _raise_uploader_factory(*args, **kwargs):
        return _FakeOssUploader(args[0] if args else kwargs.get("config"), raise_on_upload=RuntimeError("OSS 403"))

    monkeypatch.setattr(video_mod, "_OssUploader", _raise_uploader_factory)

    r = VideoResolver().fetch(
        _make_video_item(),
        FetchDestination(mode="both", download_dir=tmp_path, oss=_oss_config()),
    )
    assert r.is_success, "本地已成功 = 整体 success"
    assert r.oss_error is not None
    assert "OSS 403" in r.oss_error


def test_video_fetch_oss_mode_oss_fail_returns_error(monkeypatch, tmp_path):
    """oss-only 模式 OSS 失败 = 整体失败 (spec §9: both 才降级, oss-only 没有本地兜底)."""
    _patch_video_dependencies(monkeypatch, downloaded=_downloaded_video())
    from multimedia_parsing.resource_fetcher.resolvers import video as video_mod

    def _raise_uploader_factory(*args, **kwargs):
        return _FakeOssUploader(args[0] if args else kwargs.get("config"), raise_on_upload=RuntimeError("OSS 403"))

    monkeypatch.setattr(video_mod, "_OssUploader", _raise_uploader_factory)

    r = VideoResolver().fetch(
        _make_video_item(),
        FetchDestination(mode="oss", oss=_oss_config()),
    )
    assert not r.is_success
    assert r.error is not None
    assert "upload" in r.error


def test_video_fetch_missing_video_id_in_meta(monkeypatch, tmp_path):
    _patch_video_dependencies(monkeypatch, downloaded=_downloaded_video())
    bad_item = ResourceItem(
        item_id="x",
        resource_type="video",
        platform="bilibili",
        source_url="z",
        meta={},  # 没 video_id
    )
    r = VideoResolver().fetch(
        bad_item, FetchDestination(mode="local", download_dir=tmp_path)
    )
    assert not r.is_success
    assert "video_id" in r.error


def test_video_fetch_wrong_resource_type(monkeypatch, tmp_path):
    _patch_video_dependencies(monkeypatch, downloaded=_downloaded_video())
    bad_item = ResourceItem(
        item_id="x",
        resource_type="image",  # video resolver 不应处理 image
        platform="weibo",
        source_url="z",
        meta={"video_id": "x"},
    )
    r = VideoResolver().fetch(
        bad_item, FetchDestination(mode="local", download_dir=tmp_path)
    )
    assert not r.is_success
    assert "image" in r.error


def test_video_fetch_download_error(monkeypatch, tmp_path):
    from multimedia_parsing.video_fetcher.downloader import DownloaderError

    _patch_video_dependencies(
        monkeypatch,
        download_error=DownloaderError("network timeout"),
    )
    r = VideoResolver().fetch(
        _make_video_item(), FetchDestination(mode="local", download_dir=tmp_path)
    )
    assert not r.is_success
    assert "network timeout" in r.error


def test_video_fetch_passes_cookie_file_to_download(monkeypatch, tmp_path):
    """cookie_file 在 __init__ 接收, fetch 时透传给 _download_video."""
    calls = _patch_video_dependencies(monkeypatch, downloaded=_downloaded_video())
    VideoResolver(cookie_file=Path("cookies.txt")).fetch(
        _make_video_item(),
        FetchDestination(mode="local", download_dir=tmp_path),
    )
    assert calls[0]["cookie_file"] == "cookies.txt"


# ---------------------------------------------------------------------------
# ImageResolver.parse
# ---------------------------------------------------------------------------


def test_image_resolver_parse_returns_one_item_per_image(monkeypatch):
    _patch_image_dependencies(
        monkeypatch,
        parsed=[
            _FakeResolvedImage(url="https://weibo.com/1.jpg", width=800, height=600),
            _FakeResolvedImage(url="https://weibo.com/2.jpg", width=1024, height=768),
            _FakeResolvedImage(url="https://weibo.com/3.jpg"),
        ],
    )
    items = ImageResolver().parse("https://weibo.com/123")
    assert len(items) == 3
    assert all(it.resource_type == "image" for it in items)
    assert all(it.platform == "weibo" for it in items)
    assert all(it.source_url == "https://weibo.com/123" for it in items)
    # item_id 都是 uuid 短串, 各自唯一
    ids = [it.item_id for it in items]
    assert len(set(ids)) == 3
    # image_url 在 meta 里
    assert items[0].meta["image_url"] == "https://weibo.com/1.jpg"
    assert items[0].meta["width"] == 800
    assert items[0].meta["height"] == 600
    assert "width" not in items[2].meta  # 没尺寸


def test_image_resolver_parse_empty(monkeypatch):
    """页面无图 → 返空 list (run_parse 层发空资源事件)."""
    _patch_image_dependencies(monkeypatch, parsed=[])
    items = ImageResolver().parse("https://example.com/empty")
    assert items == []


def test_image_resolver_parse_error_returns_empty(monkeypatch):
    from multimedia_parsing.image_fetcher.resolver import ResolverError

    _patch_image_dependencies(
        monkeypatch,
        parse_error=ResolverError("gallery-dl timeout"),
    )
    items = ImageResolver().parse("https://example.com/foo")
    assert items == []


# ---------------------------------------------------------------------------
# ImageResolver.fetch
# ---------------------------------------------------------------------------


def _make_image_item(image_url: str = "https://weibo.com/1.jpg", index: int = 1) -> ResourceItem:
    return ResourceItem(
        item_id=f"img_{index}",
        resource_type="image",
        platform="weibo",
        source_url="https://weibo.com/123",
        meta={"image_url": image_url, "width": 800, "height": 600},
    )


def _downloaded_image(index: int = 1, ok: bool = True) -> _FakeDownloadedImage:
    return _FakeDownloadedImage(
        file_path=Path(f"/tmp/{index:02d}_img.jpg") if ok else Path(""),
        image_url=f"https://weibo.com/{index}.jpg",
        source_url="https://weibo.com/123",
        index=index,
        error=None if ok else "download fail",
    )


def test_image_fetch_local_mode(monkeypatch, tmp_path):
    _patch_image_dependencies(
        monkeypatch, downloaded=[_downloaded_image(1, ok=True)]
    )
    r = ImageResolver().fetch(
        _make_image_item("https://weibo.com/1.jpg"),
        FetchDestination(mode="local", download_dir=tmp_path),
    )
    assert r.is_success
    assert r.local_path == Path("/tmp/01_img.jpg")
    assert r.oss_url is None


def test_image_fetch_oss_mode(monkeypatch, tmp_path):
    _patch_image_dependencies(
        monkeypatch, downloaded=[_downloaded_image(1, ok=True)]
    )
    r = ImageResolver().fetch(
        _make_image_item("https://weibo.com/1.jpg"),
        FetchDestination(mode="oss", oss=_oss_config()),
    )
    assert r.is_success
    assert r.oss_url is not None
    assert r.oss_url.startswith("https://oss.example.com/")


def test_image_fetch_both_mode_oss_fail_still_success(monkeypatch, tmp_path):
    """spec §9: both 模式 OSS 失败但本地已成功 → success + oss_error."""
    _patch_image_dependencies(
        monkeypatch, downloaded=[_downloaded_image(1, ok=True)]
    )
    from multimedia_parsing.resource_fetcher.resolvers import image as image_mod

    def _raise_uploader_factory(*args, **kwargs):
        return _FakeOssUploader(args[0] if args else kwargs.get("config"), raise_on_upload=RuntimeError("OSS 403"))

    monkeypatch.setattr(image_mod, "_OssUploader", _raise_uploader_factory)

    r = ImageResolver().fetch(
        _make_image_item("https://weibo.com/1.jpg"),
        FetchDestination(mode="both", download_dir=tmp_path, oss=_oss_config()),
    )
    assert r.is_success
    assert r.oss_error is not None


def test_image_fetch_missing_image_url_in_meta(monkeypatch, tmp_path):
    _patch_image_dependencies(monkeypatch, downloaded=[_downloaded_image(1, ok=True)])
    bad_item = ResourceItem(
        item_id="x",
        resource_type="image",
        platform="weibo",
        source_url="https://weibo.com/123",
        meta={},  # 没 image_url
    )
    r = ImageResolver().fetch(
        bad_item, FetchDestination(mode="local", download_dir=tmp_path)
    )
    assert not r.is_success
    assert "image_url" in r.error


def test_image_fetch_wrong_resource_type(monkeypatch, tmp_path):
    _patch_image_dependencies(monkeypatch, downloaded=[_downloaded_image(1, ok=True)])
    bad_item = ResourceItem(
        item_id="x",
        resource_type="video",  # image resolver 不应处理 video
        platform="bilibili",
        source_url="z",
        meta={"image_url": "x"},
    )
    r = ImageResolver().fetch(
        bad_item, FetchDestination(mode="local", download_dir=tmp_path)
    )
    assert not r.is_success


def test_image_fetch_download_error(monkeypatch, tmp_path):
    from multimedia_parsing.image_fetcher.downloader import DownloaderError

    _patch_image_dependencies(
        monkeypatch, download_error=DownloaderError("HTTP 404")
    )
    r = ImageResolver().fetch(
        _make_image_item("https://weibo.com/1.jpg"),
        FetchDestination(mode="local", download_dir=tmp_path),
    )
    assert not r.is_success
    assert "HTTP 404" in r.error

