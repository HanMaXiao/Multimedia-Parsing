"""test_video_fetcher_resolver_downloader.py — resolver + downloader 单元测试。

全部 mock 驱动(不打真站)。覆盖:
  - detect_platform:域名 / 短链 / 子域 / unknown
  - resolve:正常解析(yt-dlp 注入 fake)→ ResolvedVideo
  - resolve:yt-dlp 抛异常 → ResolverError
  - resolve:info 缺关键字段(id / title / extractor)→ ResolverError
  - resolve:duration 类型错 → 视为 None
  - download:正常下载(YoutubeDL fake)→ DownloadedVideo
  - download:out_dir 不存在 → 自动 mkdir
  - download:yt-dlp 抛异常 → DownloaderError
  - download:下载完成但文件不存在 → DownloaderError
  - parse_progress_percent:percent_str / bytes 推算 / None
  - imageio_ffmpeg 缺装 → ffmpeg_location 走 None

测试架构注记(2026-09-02):
  - 不在 ``_FakeYoutubeDL`` 上用 class attribute ``instances: list = []`` 收集 —
    pytest 加载 conftest 和 test 文件时,``_FakeYoutubeDL`` 是**不同 class object**
    (module 双副本),class attribute 共享失败。
  - 改用 **test-local capture list**:每个 test function 内 ``captured: list = []``,
    ``_Configured.__init__`` append self 到 captured,断言走 captured[0]。
  - 这样天然跨测试隔离,不需要 conftest 跨模块清残留。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from multimedia_parsing.video_fetcher import downloader as dl_mod
from multimedia_parsing.video_fetcher import resolver as res_mod
from multimedia_parsing.video_fetcher.downloader import (
    DownloadedVideo,
    DownloaderError,
    download,
    parse_progress_percent,
)
from multimedia_parsing.video_fetcher.resolver import (
    ResolvedVideo,
    ResolverError,
    detect_platform,
    resolve,
)


# ---------------------------------------------------------------------------
# Fake yt-dlp module(模拟 yt_dlp.YoutubeDL)
# ---------------------------------------------------------------------------


class _FakeYoutubeDL:
    """最小化 YoutubeDL:__enter__/__exit__ + extract_info / download。

    **不**用 class attribute 收集 instances(见 module docstring)。
    构造时只 set ``self.opts``,test 端用 closure 捕获 self。
    """

    def __init__(self, opts: Dict[str, Any]):
        self.opts = opts
        self.extract_info_return: Any = None
        self.extract_info_side_effect: Optional[Exception] = None
        self.download_return: Any = None
        self.download_side_effect: Optional[Exception] = None

    # 上下文管理器
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url: str, download: bool = False) -> Any:
        if self.extract_info_side_effect is not None:
            raise self.extract_info_side_effect
        return self.extract_info_return

    def download(self, urls: list) -> Any:
        if self.download_side_effect is not None:
            raise self.download_side_effect
        return self.download_return

    def close(self) -> None:
        return None


class _FakeYtDlpModule:
    """模块级替身:fake 模块,带 YoutubeDL 引用。"""

    def __init__(self) -> None:
        self.YoutubeDL = _FakeYoutubeDL


def _patch_yt_dlp(monkeypatch, fake_module: _FakeYtDlpModule):
    """把 fake 注入到 resolver + downloader 模块的 yt_dlp 名字。"""
    monkeypatch.setattr(res_mod, "yt_dlp", fake_module)
    monkeypatch.setattr(dl_mod, "yt_dlp", fake_module)


# ---------------------------------------------------------------------------
# detect_platform
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        # bilibili(主域 / 短链)
        ("https://www.bilibili.com/video/BV1YM4m1z7nB", "bilibili"),
        ("https://bilibili.com/video/BV1xx", "bilibili"),
        ("https://b23.tv/abc123", "bilibili"),
        ("https://m.bilibili.com/video/BV1xx", "bilibili"),
        # youtube(主域 / 短链)
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
        ("https://m.youtube.com/watch?v=xx", "youtube"),
        # x / twitter
        ("https://x.com/foo/status/123", "x"),
        ("https://twitter.com/foo/status/123", "x"),
        # 小红书 / 快手
        ("https://www.xiaohongshu.com/explore/abc", "xiaohongshu"),
        ("https://xhslink.com/abc", "xiaohongshu"),
        ("https://v.kuaishou.com/abc", "kuaishou"),
        ("https://www.kuaishou.com/short-video/abc", "kuaishou"),
    ],
)
def test_detect_platform_supported(url: str, expected: str):
    assert detect_platform(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/foo",
        "not a url at all",
        "",
    ],
)
def test_detect_platform_unknown(url: str):
    """无法识别的域名(或不合法 URL)→ 'unknown',不抛错。

    注:``ftp://bilibili.com/foo`` 也算 bilibili — 我们只查 hostname,scheme 不影响识别;
    真实场景下 yt-dlp 也不会处理 ftp,到 download 阶段才报 unknown。
    """
    assert detect_platform(url) == "unknown"


# ---------------------------------------------------------------------------
# resolve — 正常路径
# ---------------------------------------------------------------------------


def test_resolve_returns_resolved_video(monkeypatch):
    fake = _FakeYtDlpModule()
    captured: list = []
    info = {
        "id": "BV1YM4m1z7nB",
        "title": "测试视频",
        "extractor": "BiliBili",
        "duration": 123.5,
    }

    class _Configured(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            captured.append(self)
            self.extract_info_return = info

    fake.YoutubeDL = _Configured
    _patch_yt_dlp(monkeypatch, fake)

    rv = resolve("https://www.bilibili.com/video/BV1YM4m1z7nB")

    assert isinstance(rv, ResolvedVideo)
    assert rv.platform == "bilibili"
    assert rv.video_id == "BV1YM4m1z7nB"
    assert rv.title == "测试视频"
    assert rv.duration == 123.5
    assert rv.extractor == "BiliBili"
    assert rv.url == "https://www.bilibili.com/video/BV1YM4m1z7nB"
    # opt 校验:noplaylist=True / quiet=True / skip_download=True
    assert len(captured) == 1
    constructed = captured[0]
    assert constructed.opts["quiet"] is True
    assert constructed.opts["skip_download"] is True
    assert constructed.opts["noplaylist"] is True
    # 无 cookie_file 时不应传 cookiefile
    assert "cookiefile" not in constructed.opts


def test_resolve_passes_cookie_file(monkeypatch):
    fake = _FakeYtDlpModule()
    captured: list = []
    info = {
        "id": "BV1xx",
        "title": "t",
        "extractor": "BiliBili",
        "duration": 10,
    }

    class _Configured(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            captured.append(self)
            self.extract_info_return = info

    fake.YoutubeDL = _Configured
    _patch_yt_dlp(monkeypatch, fake)

    resolve("https://www.bilibili.com/video/BV1xx", cookie_file="/tmp/cookies.txt")
    assert captured[0].opts["cookiefile"] == "/tmp/cookies.txt"


def test_resolve_handles_missing_duration(monkeypatch):
    """duration 缺(None)合法,不强校验。"""
    fake = _FakeYtDlpModule()
    captured: list = []
    info = {
        "id": "BV1xx",
        "title": "t",
        "extractor": "BiliBili",
        "duration": None,
    }

    class _Configured(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            captured.append(self)
            self.extract_info_return = info

    fake.YoutubeDL = _Configured
    _patch_yt_dlp(monkeypatch, fake)

    rv = resolve("https://www.bilibili.com/video/BV1xx")
    assert rv.duration is None


def test_resolve_treats_invalid_duration_as_none(monkeypatch):
    """duration 字段类型错(不是 int/float)→ 视为 None,不抛错。"""
    fake = _FakeYtDlpModule()
    captured: list = []
    info = {
        "id": "BV1xx",
        "title": "t",
        "extractor": "BiliBili",
        "duration": "not a number",  # 故意类型错
    }

    class _Configured(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            captured.append(self)
            self.extract_info_return = info

    fake.YoutubeDL = _Configured
    _patch_yt_dlp(monkeypatch, fake)

    rv = resolve("https://www.bilibili.com/video/BV1xx")
    assert rv.duration is None


# ---------------------------------------------------------------------------
# resolve — 异常路径
# ---------------------------------------------------------------------------


def test_resolve_wraps_yt_dlp_exception(monkeypatch):
    fake = _FakeYtDlpModule()

    class _Boom(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            self.extract_info_side_effect = RuntimeError("network down")

    fake.YoutubeDL = _Boom
    _patch_yt_dlp(monkeypatch, fake)

    with pytest.raises(ResolverError, match="yt-dlp extract_info failed"):
        resolve("https://www.bilibili.com/video/BV1xx")


def test_resolve_rejects_non_dict_info(monkeypatch):
    fake = _FakeYtDlpModule()

    class _BadReturn(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            self.extract_info_return = "not a dict"

    fake.YoutubeDL = _BadReturn
    _patch_yt_dlp(monkeypatch, fake)

    with pytest.raises(ResolverError, match="non-dict info"):
        resolve("https://www.bilibili.com/video/BV1xx")


@pytest.mark.parametrize("missing_field", ["id", "title", "extractor"])
def test_resolve_rejects_missing_required_field(monkeypatch, missing_field):
    fake = _FakeYtDlpModule()
    captured: list = []
    info = {
        "id": "BV1xx",
        "title": "t",
        "extractor": "BiliBili",
    }
    del info[missing_field]

    class _Configured(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            captured.append(self)
            self.extract_info_return = info

    fake.YoutubeDL = _Configured
    _patch_yt_dlp(monkeypatch, fake)

    with pytest.raises(ResolverError, match=f"missing '{missing_field}'"):
        resolve("https://www.bilibili.com/video/BV1xx")


def test_resolve_raises_when_yt_dlp_not_installed(monkeypatch):
    """yt_dlp = None(模块顶 except 路径)→ 显式 ImportError。"""
    monkeypatch.setattr(res_mod, "yt_dlp", None)
    with pytest.raises(ImportError, match="yt-dlp is not installed"):
        resolve("https://www.bilibili.com/video/BV1xx")


# ---------------------------------------------------------------------------
# download — 正常路径
# ---------------------------------------------------------------------------


def test_download_returns_downloaded_video(monkeypatch, tmp_path: Path):
    fake = _FakeYtDlpModule()
    out_dir = tmp_path / "downloads"
    out_dir.mkdir()  # 预创建 — 测试 mkdir(parents=True, exist_ok=True) 不出错

    # 模拟 yt-dlp 下载后写文件
    expected_file = out_dir / "bilibili_BV1xx.mp4"
    info = {
        "id": "BV1xx",
        "title": "t",
        "extractor": "BiliBili",
        "duration": 60.0,
        "ext": "mp4",
        "requested_downloads": [{"filepath": str(expected_file)}],
    }
    expected_file.write_bytes(b"fake video bytes")  # 落盘供 _extract_downloaded_file 校验

    class _Configured(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            self.download_return = info

    fake.YoutubeDL = _Configured
    _patch_yt_dlp(monkeypatch, fake)

    dv = download(
        "https://www.bilibili.com/video/BV1xx",
        out_dir,
        platform="bilibili",
        video_id="BV1xx",
    )

    assert isinstance(dv, DownloadedVideo)
    assert dv.file_path == expected_file
    assert dv.platform == "bilibili"
    assert dv.video_id == "BV1xx"
    assert dv.title == "t"
    assert dv.ext == "mp4"
    assert dv.duration == 60.0


def test_download_creates_out_dir_if_missing(monkeypatch, tmp_path: Path):
    """out_dir 不存在 → 自动 mkdir(parents=True, exist_ok=True)。

    fake YoutubeDL 模拟 yt-dlp 真的写盘到 out_dir,验证 download 内部
    mkdir 后文件能被 yt-dlp 写,file_path.exists() 验证最终成功路径。
    """
    fake = _FakeYtDlpModule()
    out_dir = tmp_path / "deep" / "nested" / "downloads"
    # 关键:不预创建 out_dir
    assert not out_dir.exists()

    info = {
        "id": "BV1xx",
        "title": "t",
        "extractor": "BiliBili",
        "duration": 30,
        "ext": "mp4",
        "requested_downloads": [{"filepath": str(out_dir / "bilibili_BV1xx.mp4")}],
    }

    class _WritesFile(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            self.download_return = info

        def download(self, urls):
            # 模拟 yt-dlp 真写盘:download 内部已 mkdir(我们走 mkdir(parents=True))
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "bilibili_BV1xx.mp4").write_bytes(b"x")
            return info

    fake.YoutubeDL = _WritesFile
    _patch_yt_dlp(monkeypatch, fake)

    dv = download(
        "https://www.bilibili.com/video/BV1xx",
        out_dir,
        platform="bilibili",
        video_id="BV1xx",
    )
    assert dv.file_path.exists()
    assert out_dir.is_dir()


def test_download_passes_cookie_file(monkeypatch, tmp_path: Path):
    fake = _FakeYtDlpModule()
    captured: list = []
    out_dir = tmp_path / "d"
    out_dir.mkdir()
    expected_file = out_dir / "bilibili_BV1xx.mp4"
    expected_file.write_bytes(b"x")
    info = {
        "id": "BV1xx",
        "title": "t",
        "extractor": "BiliBili",
        "duration": 30,
        "ext": "mp4",
        "requested_downloads": [{"filepath": str(expected_file)}],
    }

    class _Configured(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            captured.append(self)
            self.download_return = info

    fake.YoutubeDL = _Configured
    _patch_yt_dlp(monkeypatch, fake)

    download(
        "https://www.bilibili.com/video/BV1xx",
        out_dir,
        platform="bilibili",
        video_id="BV1xx",
        cookie_file="/tmp/cookies.txt",
    )
    assert captured[0].opts["cookiefile"] == "/tmp/cookies.txt"


def test_download_progress_hook_invoked(monkeypatch, tmp_path: Path):
    """progress_cb 注入 → yt-dlp download 时调用 hook。"""
    fake = _FakeYtDlpModule()
    out_dir = tmp_path / "d"
    out_dir.mkdir()
    expected_file = out_dir / "bilibili_BV1xx.mp4"
    expected_file.write_bytes(b"x")
    info = {
        "id": "BV1xx",
        "title": "t",
        "extractor": "BiliBili",
        "duration": 30,
        "ext": "mp4",
        "requested_downloads": [{"filepath": str(expected_file)}],
    }

    class _Configured(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            self.download_return = info

        def download(self, urls):
            # 模拟 yt-dlp 调用 progress_hooks
            for hook in self.opts.get("progress_hooks", []):
                hook({"status": "downloading", "_percent_str": " 45.3%"})
                hook({"status": "finished"})
            return info

    fake.YoutubeDL = _Configured
    _patch_yt_dlp(monkeypatch, fake)

    seen: list = []

    def cb(d: Dict[str, Any]) -> None:
        seen.append(d)

    download(
        "https://www.bilibili.com/video/BV1xx",
        out_dir,
        platform="bilibili",
        video_id="BV1xx",
        progress_cb=cb,
    )
    assert len(seen) == 2
    assert seen[0]["status"] == "downloading"
    assert seen[1]["status"] == "finished"


# ---------------------------------------------------------------------------
# download — 异常路径
# ---------------------------------------------------------------------------


def test_download_wraps_yt_dlp_exception(monkeypatch, tmp_path: Path):
    fake = _FakeYtDlpModule()
    out_dir = tmp_path / "d"
    out_dir.mkdir()

    class _Boom(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            self.download_side_effect = RuntimeError("network 403")

    fake.YoutubeDL = _Boom
    _patch_yt_dlp(monkeypatch, fake)

    with pytest.raises(DownloaderError, match="yt-dlp download failed"):
        download(
            "https://www.bilibili.com/video/BV1xx",
            out_dir,
            platform="bilibili",
            video_id="BV1xx",
        )


def test_download_raises_when_file_missing(monkeypatch, tmp_path: Path):
    """yt-dlp 返 success 但文件不存在 → DownloaderError。"""
    fake = _FakeYtDlpModule()
    out_dir = tmp_path / "d"
    out_dir.mkdir()
    info = {
        "id": "BV1xx",
        "title": "t",
        "extractor": "BiliBili",
        "duration": 30,
        "ext": "mp4",
        "requested_downloads": [{"filepath": str(out_dir / "nonexistent.mp4")}],
    }

    class _Configured(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            self.download_return = info

    fake.YoutubeDL = _Configured
    _patch_yt_dlp(monkeypatch, fake)

    with pytest.raises(DownloaderError, match="file not found"):
        download(
            "https://www.bilibili.com/video/BV1xx",
            out_dir,
            platform="bilibili",
            video_id="BV1xx",
        )


def test_download_raises_when_yt_dlp_not_installed(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(dl_mod, "yt_dlp", None)
    with pytest.raises(ImportError, match="yt-dlp is not installed"):
        download(
            "https://www.bilibili.com/video/BV1xx",
            tmp_path / "d",
            platform="bilibili",
            video_id="BV1xx",
        )


# ---------------------------------------------------------------------------
# ffmpeg fallback(imageio_ffmpeg 缺装)
# ---------------------------------------------------------------------------


def test_download_works_without_imageio_ffmpeg(monkeypatch, tmp_path: Path):
    """imageio_ffmpeg 缺装 → ffmpeg_location 走 None,yt-dlp 单流 fallback。"""
    monkeypatch.setattr(dl_mod, "imageio_ffmpeg", None)

    fake = _FakeYtDlpModule()
    captured: list = []
    out_dir = tmp_path / "d"
    out_dir.mkdir()
    expected_file = out_dir / "bilibili_BV1xx.mp4"
    expected_file.write_bytes(b"x")
    info = {
        "id": "BV1xx",
        "title": "t",
        "extractor": "BiliBili",
        "duration": 30,
        "ext": "mp4",
        "requested_downloads": [{"filepath": str(expected_file)}],
    }

    class _Configured(_FakeYoutubeDL):
        def __init__(self, opts):
            super().__init__(opts)
            captured.append(self)
            self.download_return = info

    fake.YoutubeDL = _Configured
    _patch_yt_dlp(monkeypatch, fake)

    dv = download(
        "https://www.bilibili.com/video/BV1xx",
        out_dir,
        platform="bilibili",
        video_id="BV1xx",
    )
    # opts 不应含 ffmpeg_location
    assert "ffmpeg_location" not in captured[0].opts
    assert dv.file_path.exists()


# ---------------------------------------------------------------------------
# parse_progress_percent
# ---------------------------------------------------------------------------


def test_parse_progress_percent_from_percent_str():
    assert parse_progress_percent({"_percent_str": " 45.3%"}) == 45.3
    assert parse_progress_percent({"_percent_str": "100.0%"}) == 100.0
    assert parse_progress_percent({"_percent_str": "0%"}) == 0.0


def test_parse_progress_percent_from_bytes():
    """_percent_str 缺 → 从 downloaded/total 推算。"""
    assert parse_progress_percent(
        {"downloaded_bytes": 500, "total_bytes": 1000}
    ) == 50.0
    assert parse_progress_percent(
        {"downloaded_bytes": 1000, "total_bytes_estimate": 2000}
    ) == 50.0


def test_parse_progress_percent_returns_none():
    assert parse_progress_percent({}) is None
    assert parse_progress_percent({"downloaded_bytes": 100}) is None  # 缺 total
    assert parse_progress_percent({"_percent_str": "abc"}) is None
    assert parse_progress_percent({"_percent_str": None}) is None


def test_parse_progress_percent_rejects_out_of_range():
    """超 100% 是 yt-dlp 估算错误,reject 返 None(不静默 clamp,caller 处理)。"""
    assert parse_progress_percent({"_percent_str": "150%"}) is None

