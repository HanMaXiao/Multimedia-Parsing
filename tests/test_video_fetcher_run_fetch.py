"""test_video_fetcher_run_fetch.py — run_fetch 编排器单元测试。

覆盖范围(spec §4.1 / §5 / §8):
  - 单 URL 全程:logging_in → uploading(progress) → processing → success
  - progress hook 触发 + progress 字段透传
  - 单 URL resolve 失败:failed with "resolve: ..." error 前缀
  - 单 URL download 失败:logging_in → uploading → failed("download:" 前缀)
  - 单 URL upload 失败:logging_in → uploading → processing → failed("upload:" 前缀)
  - 整批多 URL:URL1 失败不中断,URL2 仍跑
  - 整批取消:cancel_event set → 所有未启动 cell → cancelled
  - 单 URL 取消:跑到一半 cancel → 该 cell cancelled
  - event_cb=None 静默
  - article 标识 = ``{platform}_{video_id}``(spec §5)
  - source_url 字段传递(spec §5 加性字段)
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from multimedia_parsing.video_fetcher import run_fetch as rf
from multimedia_parsing.video_fetcher.downloader import DownloadedVideo, DownloaderError
from multimedia_parsing.video_fetcher.manifest import (
    ManifestValidationError,
    OssConfig,
    VideoFetchManifest,
)
from multimedia_parsing.oss import OssResult, OssUploadError  # Plan 2026-09-03 F0.5: 移至 publisher.oss
from multimedia_parsing.video_fetcher.resolver import ResolvedVideo, ResolverError
from multimedia_parsing.video_fetcher.run_fetch import CellResult, run_fetch


# ---------------------------------------------------------------------------
# Fake 依赖
# ---------------------------------------------------------------------------


class _FakeResolved(ResolvedVideo):
    """绕过 ResolvedVideo frozen 限制直接构造(测试用)。"""

    def __init__(self, url: str, platform: str, video_id: str) -> None:
        # 用 object.__setattr__ 绕过 frozen
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "video_id", video_id)
        object.__setattr__(self, "title", f"title-{video_id}")
        object.__setattr__(self, "duration", 60.0)
        object.__setattr__(self, "extractor", platform)


class _FakeDownloaded(DownloadedVideo):
    def __init__(self, file_path: Path, platform: str, video_id: str, ext: str = "mp4") -> None:
        object.__setattr__(self, "file_path", file_path)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "video_id", video_id)
        object.__setattr__(self, "title", f"title-{video_id}")
        object.__setattr__(self, "ext", ext)
        object.__setattr__(self, "duration", 60.0)


def _oss_config() -> OssConfig:
    return OssConfig(
        provider="aliyun_oss",
        endpoint="oss-cn-hangzhou.aliyuncs.com",
        region="cn-hangzhou",
        bucket="b",
        access_key_id="AK",
        secret_access_key="SK",
    )


def _manifest(urls: List[str], **overrides) -> VideoFetchManifest:
    base = dict(
        batch_id="batch_test",
        urls=urls,
        oss=_oss_config(),
        download_dir=Path("/tmp/runs/batch_test/downloads"),
    )
    base.update(overrides)
    return VideoFetchManifest(**base)


# ---------------------------------------------------------------------------
# 事件 capture helper
# ---------------------------------------------------------------------------


def _capture_cb_factory() -> tuple[list, callable]:
    """返 (captured, cb) — cb 把 (article, platform, status, **kwargs) 拍平成 dict 入 list。"""
    captured: list = []

    def cb(article: str, platform: str, status: str, **kwargs: Any) -> None:
        captured.append(
            {
                "article": article,
                "platform": platform,
                "status": status,
                **kwargs,
            }
        )

    return captured, cb


# ---------------------------------------------------------------------------
# 单 URL 全程(成功)
# ---------------------------------------------------------------------------


def test_run_fetch_happy_path_emits_expected_sequence(monkeypatch, tmp_path: Path):
    """单 URL 全程:logging_in → uploading(progress) → processing → success。"""
    manifest = _manifest(
        ["https://www.bilibili.com/video/BV1xx"], download_dir=tmp_path / "d"
    )
    captured, cb = _capture_cb_factory()

    # fake resolve
    def fake_resolve(url: str, *, cookie_file: Optional[str] = None) -> ResolvedVideo:
        return _FakeResolved(url, platform="bilibili", video_id="BV1xx")

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)

    # fake download
    def fake_download(
        url: str, out_dir: Path, *, platform: str, video_id: str,
        cookie_file: Optional[str] = None,
        progress_cb: Optional[Any] = None,
    ) -> DownloadedVideo:
        # 模拟 yt-dlp 调用 progress hook 一次
        if progress_cb is not None:
            progress_cb({"status": "downloading", "_percent_str": " 50.0%"})
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    monkeypatch.setattr(rf, "download", fake_download)

    # fake uploader
    class _FakeUploader:
        def __init__(self, config: OssConfig) -> None:
            self.config = config

        def upload(
            self, local_path: Path, *, platform: str, video_id: str, ext: str
        ) -> OssResult:
            return OssResult(
                key=f"videos/2026-09-02/{platform}_{video_id}.{ext}",
                url=f"https://b.oss-cn-hangzhou.aliyuncs.com/{platform}_{video_id}.{ext}",
                bucket="b",
                size_bytes=1,
            )

    monkeypatch.setattr(rf, "OssUploader", _FakeUploader)

    cells = run_fetch(manifest, event_cb=cb)

    assert len(cells) == 1
    cell = cells[0]
    assert cell.status == "success"
    assert cell.article == "bilibili_BV1xx"
    assert cell.platform == "bilibili"
    assert cell.url is not None
    assert cell.source_url == "https://www.bilibili.com/video/BV1xx"
    assert cell.error is None

    # 事件序列断言:logging_in → uploading(+ progress 50) → processing → success
    statuses = [e["status"] for e in captured]
    assert statuses == ["logging_in", "uploading", "uploading", "processing", "success"]

    # progress 字段在 uploading 阶段透传
    uploading_with_progress = [
        e for e in captured if e["status"] == "uploading" and e.get("progress") is not None
    ]
    assert len(uploading_with_progress) == 1
    assert uploading_with_progress[0]["progress"] == 50.0

    # 所有事件带 source_url(spec §5 加性字段)
    for e in captured:
        if e["status"] in ("uploading", "processing", "success"):
            assert e["source_url"] == "https://www.bilibili.com/video/BV1xx"


# ---------------------------------------------------------------------------
# 单 URL resolve 失败
# ---------------------------------------------------------------------------


def test_run_fetch_resolver_error_emits_failed(monkeypatch, tmp_path: Path):
    """resolve 失败 → logging_in → failed('resolve:' 前缀)。"""
    manifest = _manifest(["https://example.com/bad"], download_dir=tmp_path / "d")
    captured, cb = _capture_cb_factory()

    def fake_resolve(url: str, *, cookie_file: Optional[str] = None) -> ResolvedVideo:
        raise ResolverError("unsupported site")

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)

    cells = run_fetch(manifest, event_cb=cb)
    assert len(cells) == 1
    assert cells[0].status == "failed"
    assert "resolve:" in (cells[0].error or "")
    assert "unsupported site" in (cells[0].error or "")

    statuses = [e["status"] for e in captured]
    assert statuses == ["logging_in", "failed"]


# ---------------------------------------------------------------------------
# 单 URL download 失败
# ---------------------------------------------------------------------------


def test_run_fetch_download_error_emits_failed(monkeypatch, tmp_path: Path):
    """download 失败 → logging_in → uploading → failed('download:' 前缀)。"""
    manifest = _manifest(["https://www.bilibili.com/video/BV1xx"], download_dir=tmp_path / "d")
    captured, cb = _capture_cb_factory()

    def fake_resolve(url, *, cookie_file=None):
        return _FakeResolved(url, platform="bilibili", video_id="BV1xx")

    def fake_download(
        url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None
    ):
        raise DownloaderError("yt-dlp download failed: network 403")

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)

    cells = run_fetch(manifest, event_cb=cb)
    assert len(cells) == 1
    assert cells[0].status == "failed"
    assert "download:" in (cells[0].error or "")
    assert "network 403" in (cells[0].error or "")

    statuses = [e["status"] for e in captured]
    assert statuses == ["logging_in", "uploading", "failed"]


# ---------------------------------------------------------------------------
# 单 URL upload 失败
# ---------------------------------------------------------------------------


def test_run_fetch_upload_error_emits_failed(monkeypatch, tmp_path: Path):
    """upload 失败 → logging_in → uploading → processing → failed('upload:' 前缀)。"""
    manifest = _manifest(["https://www.bilibili.com/video/BV1xx"], download_dir=tmp_path / "d")
    captured, cb = _capture_cb_factory()

    def fake_resolve(url, *, cookie_file=None):
        return _FakeResolved(url, platform="bilibili", video_id="BV1xx")

    def fake_download(
        url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None
    ):
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    class _FakeUploader:
        def __init__(self, config):
            pass

        def upload(self, local_path, *, platform, video_id, ext):
            raise OssUploadError("S3 upload failed: AccessDenied")

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)
    monkeypatch.setattr(rf, "OssUploader", _FakeUploader)

    cells = run_fetch(manifest, event_cb=cb)
    assert cells[0].status == "failed"
    assert "upload:" in (cells[0].error or "")
    assert "AccessDenied" in (cells[0].error or "")

    statuses = [e["status"] for e in captured]
    assert statuses == ["logging_in", "uploading", "processing", "failed"]


# ---------------------------------------------------------------------------
# 整批:URL1 失败不中断,URL2 仍跑
# ---------------------------------------------------------------------------


def test_run_fetch_one_url_failure_does_not_interrupt_batch(monkeypatch, tmp_path: Path):
    """spec §8:单 URL 失败不中断整批。"""
    manifest = _manifest(
        [
            "https://www.bilibili.com/video/BV_BAD",  # 会失败
            "https://www.bilibili.com/video/BV_OK",   # 会成功
        ],
        download_dir=tmp_path / "d",
    )
    captured, cb = _capture_cb_factory()

    def fake_resolve(url, *, cookie_file=None):
        if "BV_BAD" in url:
            raise ResolverError("bad url")
        return _FakeResolved(url, platform="bilibili", video_id="BV_OK")

    def fake_download(
        url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None
    ):
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    class _FakeUploader:
        def __init__(self, config):
            pass

        def upload(self, local_path, *, platform, video_id, ext):
            return OssResult(
                key="k", url=f"https://b/{video_id}.{ext}",
                bucket="b", size_bytes=1,
            )

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)
    monkeypatch.setattr(rf, "OssUploader", _FakeUploader)

    cells = run_fetch(manifest, event_cb=cb)
    assert len(cells) == 2
    assert cells[0].status == "failed"
    assert "resolve:" in (cells[0].error or "")
    assert cells[1].status == "success"
    assert cells[1].article == "bilibili_BV_OK"


# ---------------------------------------------------------------------------
# 整批取消:所有未启动 cell → cancelled
# ---------------------------------------------------------------------------


def test_run_fetch_batch_cancellation(monkeypatch, tmp_path: Path):
    """cancel_event set → 当前 cell 收尾 cancelled,未启动的 cell 直接 cancelled。"""
    cancel_event = threading.Event()
    cancel_event.set()  # 启动前就取消

    manifest = _manifest(
        ["https://www.bilibili.com/video/BV1", "https://www.bilibili.com/video/BV2"],
        download_dir=tmp_path / "d",
    )
    captured, cb = _capture_cb_factory()

    # 没注册 fake_resolve / fake_download — 如果真调了会抛
    def fail_if_called(*args, **kwargs):
        raise AssertionError("resolve should not be called after cancel")

    monkeypatch.setattr(rf, "resolve_video", fail_if_called)

    cells = run_fetch(manifest, event_cb=cb, cancel_event=cancel_event)
    assert len(cells) == 2
    for c in cells:
        assert c.status == "cancelled"
        assert c.source_url in (
            "https://www.bilibili.com/video/BV1",
            "https://www.bilibili.com/video/BV2",
        )
    # 没有触发任何中间事件(直接 cancelled)
    assert captured == []


# ---------------------------------------------------------------------------
# 单 URL 取消:跑到一半 cancel
# ---------------------------------------------------------------------------


def test_run_fetch_mid_run_cancellation(monkeypatch, tmp_path: Path):
    """download 阶段 cancel → 该 cell cancelled,后续 cell 不启动。"""
    cancel_event = threading.Event()
    # resolve 时未取消;download 时取消
    cancel_during_download = {"value": False}

    def fake_resolve(url, *, cookie_file=None):
        return _FakeResolved(url, platform="bilibili", video_id="BV1")

    def fake_download(
        url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None
    ):
        cancel_event.set()  # download 入口立刻 cancel
        # cancel_event set 后,run_fetch 内层在 download 之后会检查 cancel_event
        # 我们返 fake_downloaded,run_fetch 走到 upload 前会检查 cancel_event → cancelled
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    class _FakeUploader:
        def __init__(self, config):
            pass

        def upload(self, *args, **kwargs):
            raise AssertionError("upload should not be called after cancel")

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)
    monkeypatch.setattr(rf, "OssUploader", _FakeUploader)

    manifest = _manifest(
        ["https://www.bilibili.com/video/BV1", "https://www.bilibili.com/video/BV2"],
        download_dir=tmp_path / "d",
    )
    captured, cb = _capture_cb_factory()

    cells = run_fetch(manifest, event_cb=cb, cancel_event=cancel_event)
    # 第 1 个 cell cancelled(中途 cancel),第 2 个没启动也 cancelled
    assert cells[0].status == "cancelled"
    assert cells[1].status == "cancelled"


# ---------------------------------------------------------------------------
# event_cb=None 静默
# ---------------------------------------------------------------------------


def test_run_fetch_with_no_event_cb_succeeds(monkeypatch, tmp_path: Path):
    """event_cb=None 时不抛错,events 不发,cell 仍返。"""
    manifest = _manifest(["https://www.bilibili.com/video/BV1"], download_dir=tmp_path / "d")

    def fake_resolve(url, *, cookie_file=None):
        return _FakeResolved(url, platform="bilibili", video_id="BV1")

    def fake_download(
        url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None
    ):
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    class _FakeUploader:
        def __init__(self, config):
            pass

        def upload(self, local_path, *, platform, video_id, ext):
            return OssResult(
                key="k", url="https://b/v.mp4", bucket="b", size_bytes=1
            )

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)
    monkeypatch.setattr(rf, "OssUploader", _FakeUploader)

    cells = run_fetch(manifest)  # event_cb 默认 None
    assert cells[0].status == "success"


# ---------------------------------------------------------------------------
# uploader 注入:测试可直接传 fake,跳过 OssUploader(config) 构造
# ---------------------------------------------------------------------------


def test_run_fetch_accepts_injected_uploader(monkeypatch, tmp_path: Path):
    """run_fetch(uploader=...) 注入,跳过默认 OssUploader(config)。"""
    manifest = _manifest(["https://www.bilibili.com/video/BV1"], download_dir=tmp_path / "d")
    captured, cb = _capture_cb_factory()

    def fake_resolve(url, *, cookie_file=None):
        return _FakeResolved(url, platform="bilibili", video_id="BV1")

    def fake_download(
        url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None
    ):
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    upload_calls: List[Dict[str, Any]] = []

    class _FakeUploader:
        def __init__(self, config):
            self.config = config

        def upload(self, local_path, *, platform, video_id, ext):
            upload_calls.append(
                {
                    "local_path": local_path,
                    "platform": platform,
                    "video_id": video_id,
                    "ext": ext,
                }
            )
            return OssResult(
                key="custom-key", url="https://custom.example.com/k.mp4",
                bucket="custom-bucket", size_bytes=42,
            )

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)
    monkeypatch.setattr(rf, "OssUploader", _FakeUploader)

    injected = _FakeUploader(_oss_config())
    cells = run_fetch(manifest, event_cb=cb, uploader=injected)
    assert cells[0].status == "success"
    assert cells[0].url == "https://custom.example.com/k.mp4"
    assert len(upload_calls) == 1
    assert upload_calls[0]["platform"] == "bilibili"
    assert upload_calls[0]["video_id"] == "BV1"


# ---------------------------------------------------------------------------
# article 标识
# ---------------------------------------------------------------------------


def test_run_fetch_article_id_format(monkeypatch, tmp_path: Path):
    """article = ``{platform}_{video_id}``(spec §5)。"""
    manifest = _manifest(["https://youtu.be/abc"], download_dir=tmp_path / "d")
    captured, cb = _capture_cb_factory()

    def fake_resolve(url, *, cookie_file=None):
        return _FakeResolved(url, platform="youtube", video_id="abc")

    def fake_download(
        url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None
    ):
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    class _FakeUploader:
        def __init__(self, config):
            pass

        def upload(self, local_path, *, platform, video_id, ext):
            return OssResult(
                key="k", url="https://b/k", bucket="b", size_bytes=1
            )

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)
    monkeypatch.setattr(rf, "OssUploader", _FakeUploader)

    cells = run_fetch(manifest, event_cb=cb)
    assert cells[0].article == "youtube_abc"


# ---------------------------------------------------------------------------
# cookie_file 透传
# ---------------------------------------------------------------------------


def test_run_fetch_passes_cookie_file_to_resolve_and_download(monkeypatch, tmp_path: Path):
    """manifest.cookie_file 透传到 resolve + download。"""
    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    manifest = _manifest(
        ["https://www.bilibili.com/video/BV1xx"],
        download_dir=tmp_path / "d",
        cookie_file=cookie_path,
    )
    captured, cb = _capture_cb_factory()

    resolve_cookie: List[Optional[str]] = []
    download_cookie: List[Optional[str]] = []

    def fake_resolve(url, *, cookie_file=None):
        resolve_cookie.append(cookie_file)
        return _FakeResolved(url, platform="bilibili", video_id="BV1xx")

    def fake_download(
        url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None
    ):
        download_cookie.append(cookie_file)
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    class _FakeUploader:
        def __init__(self, config):
            pass

        def upload(self, local_path, *, platform, video_id, ext):
            return OssResult(
                key="k", url="https://b/k", bucket="b", size_bytes=1
            )

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)
    monkeypatch.setattr(rf, "OssUploader", _FakeUploader)

    run_fetch(manifest, event_cb=cb)
    assert resolve_cookie == [str(cookie_path)]
    assert download_cookie == [str(cookie_path)]


def test_run_fetch_no_cookie_file_passes_none(monkeypatch, tmp_path: Path):
    """manifest.cookie_file=None → resolve / download 收 None。"""
    manifest = _manifest(
        ["https://www.bilibili.com/video/BV1xx"],
        download_dir=tmp_path / "d",
        cookie_file=None,
    )
    captured, cb = _capture_cb_factory()

    seen: List[Optional[str]] = []

    def fake_resolve(url, *, cookie_file=None):
        seen.append(cookie_file)
        return _FakeResolved(url, platform="bilibili", video_id="BV1xx")

    def fake_download(
        url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None
    ):
        seen.append(cookie_file)
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    class _FakeUploader:
        def __init__(self, config):
            pass

        def upload(self, local_path, *, platform, video_id, ext):
            return OssResult(
                key="k", url="https://b/k", bucket="b", size_bytes=1
            )

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)
    monkeypatch.setattr(rf, "OssUploader", _FakeUploader)

    run_fetch(manifest, event_cb=cb)
    assert seen == [None, None]


# ---------------------------------------------------------------------------
# 进度事件:finished 状态也发 uploading + progress=100
# ---------------------------------------------------------------------------


def test_run_fetch_emits_finished_with_progress_100(monkeypatch, tmp_path: Path):
    """yt-dlp hook ``status='finished'`` → uploading + progress=100。"""
    manifest = _manifest(["https://www.bilibili.com/video/BV1"], download_dir=tmp_path / "d")
    captured, cb = _capture_cb_factory()

    def fake_resolve(url, *, cookie_file=None):
        return _FakeResolved(url, platform="bilibili", video_id="BV1")

    def fake_download(
        url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None
    ):
        if progress_cb is not None:
            progress_cb({"status": "downloading", "_percent_str": " 25.0%"})
            progress_cb({"status": "finished"})
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    class _FakeUploader:
        def __init__(self, config):
            pass

        def upload(self, local_path, *, platform, video_id, ext):
            return OssResult(
                key="k", url="https://b/k", bucket="b", size_bytes=1
            )

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)
    monkeypatch.setattr(rf, "OssUploader", _FakeUploader)

    run_fetch(manifest, event_cb=cb)

    progress_events = [e for e in captured if e.get("progress") is not None]
    assert len(progress_events) == 2
    assert progress_events[0]["progress"] == 25.0
    assert progress_events[1]["progress"] == 100.0


# ============================================================================
# F5:本地下载模式(Plan follow-up 5/6)
# ============================================================================


def test_run_fetch_local_mode_skips_upload(tmp_path: Path, monkeypatch):
    """F5:manifest.oss = None → run_fetch 跳过 upload,success url = ile:// 本地路径。"""
    from multimedia_parsing.video_fetcher import run_fetch as rf
    from multimedia_parsing.video_fetcher.manifest import VideoFetchManifest

    # 关键:本地模式 manifest 不带 oss
    manifest = VideoFetchManifest(
        batch_id="b-local",
        urls=["https://www.bilibili.com/video/BV1local"],
        oss=None,
        download_dir=tmp_path / "d",
    )

    captured = []
    def cb(article, platform, status, **kwargs):
        captured.append({"article": article, "platform": platform, "status": status, **kwargs})

    def fake_resolve(url, *, cookie_file=None):
        return _FakeResolved(url, platform="bilibili", video_id="BV1local")

    def fake_download(url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None):
        if progress_cb is not None:
            progress_cb({"status": "finished"})
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    upload_called = {"flag": False}
    def must_not_be_called_uploader(*args, **kwargs):
        upload_called["flag"] = True
        raise AssertionError("本地模式绝不应触发 upload / OssUploader")

    # 注入 — 即便 OssUploader 构造也直接 raise
    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)
    monkeypatch.setattr(rf, "OssUploader", must_not_be_called_uploader)

    results = run_fetch(manifest, event_cb=cb)

    assert len(results) == 1
    cell = results[0]
    assert cell.status == "success"
    assert cell.url.startswith("file://"), f"本地模式 url 必为 file://,实际 {cell.url!r}"
    assert "BV1local" in cell.url
    assert not upload_called["flag"], "本地模式不应触发 OssUploader"

    # 事件序列:logging_in → uploading(进度)→ success(无 processing/upload)
    statuses = [e["status"] for e in captured]
    assert "processing" not in statuses, f"本地模式不应 emit processing,实际 {statuses}"
    assert statuses[-1] == "success"


def test_run_fetch_explicit_uploader_none_overrides_manifest_oss(tmp_path: Path, monkeypatch):
    """F5:显式传 uploader=None 覆盖 manifest.oss(测试 / 离线预览场景)。"""
    from multimedia_parsing.video_fetcher import run_fetch as rf
    from multimedia_parsing.video_fetcher.manifest import VideoFetchManifest, OssConfig

    # manifest 即使有 oss,显式 uploader=None 仍走本地模式
    manifest = VideoFetchManifest(
        batch_id="b-explicit",
        urls=["https://youtu.be/abc"],
        oss=OssConfig(
            provider="aliyun_oss", endpoint="oss-cn-hangzhou.aliyuncs.com",
            region="cn-hangzhou", bucket="b", access_key_id="AK", secret_access_key="SK",
        ),
        download_dir=tmp_path / "d",
    )

    def fake_resolve(url, *, cookie_file=None):
        return _FakeResolved(url, platform="youtube", video_id="abc")

    def fake_download(url, out_dir, *, platform, video_id, cookie_file=None, progress_cb=None):
        if progress_cb is not None:
            progress_cb({"status": "finished"})
        fake_file = out_dir / f"{platform}_{video_id}.mp4"
        fake_file.parent.mkdir(parents=True, exist_ok=True)
        fake_file.write_bytes(b"x")
        return _FakeDownloaded(fake_file, platform, video_id, ext="mp4")

    monkeypatch.setattr(rf, "resolve_video", fake_resolve)
    monkeypatch.setattr(rf, "download", fake_download)
    # 不 patch OssUploader,因为显式 uploader=None 不应构造它

    results = run_fetch(manifest, uploader=None, event_cb=lambda *a, **k: None)
    assert results[0].status == "success"
    assert results[0].url.startswith("file://")
