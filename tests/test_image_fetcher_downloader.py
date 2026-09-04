"""test_image_fetcher_downloader.py — image_fetcher/downloader.py 单元测试。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §4.1 + F2.2。

覆盖范围(全部 mock 驱动,不打真站):
  - 单图下载:成功 + content-type 校验 + Referer 头 + 文件名格式
  - 去重命名:同批次文件撞名 → 后缀 __2/__3
  - 目录创建:out_dir 不存在 → 自动 mkdir
  - 失败不中断:批量中一张失败 → 其它继续 + 失败项入 results
  - 取消:cancel_event set → 当前张收尾后停止
  - 进度回调:逐 chunk 触发 on_progress(done_bytes, total_bytes)
  - 边界:非 image/* content-type → DownloaderError;404 → DownloaderError
"""

from __future__ import annotations

import threading
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from multimedia_parsing.image_fetcher import downloader as dl_mod
from multimedia_parsing.image_fetcher.downloader import (
    DownloadedImage,
    DownloaderError,
    _filename_from_url,
    download_images,
    download_one,
)
from multimedia_parsing.image_fetcher.manifest import ImageEntry


# ---------------------------------------------------------------------------
# Fake requests.get — 模拟流式响应
# ---------------------------------------------------------------------------


class _FakeStreamResponse:
    """模拟 requests.get(stream=True) 返回的响应。

    行为:
      - status_code / headers["Content-Type"] 模拟服务端
      - iter_content(chunk_size) 按 block 产出 bytes
      - 支持 with 语句(stream=True 用法常见)
    """

    def __init__(
        self,
        content: bytes,
        *,
        content_type: str = "image/jpeg",
        status_code: int = 200,
    ):
        self._content = content
        self.status_code = status_code
        self.headers: Dict[str, str] = {"Content-Type": content_type}
        self._closed = False
        self._iter_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def close(self) -> None:
        self._closed = True

    def iter_content(self, chunk_size: int = 8192):
        self._iter_calls += 1
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _make_fake_get(responses: Dict[str, _FakeStreamResponse]):
    """构造 fake get 工厂:URL → response。"""

    def fake_get(url: str, **kwargs):
        if url not in responses:
            raise RuntimeError(f"unexpected URL in test: {url}")
        return responses[url]

    return fake_get


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _entry(url: str, source_url: str = "https://example.com/article/1") -> ImageEntry:
    return ImageEntry(url=url, source_url=source_url)


def _png_bytes() -> bytes:
    """用于"内容真存在"测试 — 任意 bytes,downloader 不校验 PNG magic。"""
    # 用 b"x" * 100 之类的固定字节也行,这里写 100 字节 "fake image data" 即可
    return b"\x89PNG_FAKE_DATA__" + b"x" * 80  # 100 bytes total


# ---------------------------------------------------------------------------
# _filename_from_url 单元测试
# ---------------------------------------------------------------------------


def test_filename_from_url_simple_path():
    assert _filename_from_url("https://example.com/path/photo.jpg") == "photo.jpg"


def test_filename_from_url_with_query():
    """query 参数在文件名里要剥(spec:用原文件名 + 序号)。"""
    out = _filename_from_url("https://example.com/path/photo.jpg?v=123")
    assert out == "photo.jpg"


def test_filename_from_url_no_extension():
    """URL 末段有名字但无扩展 → 返原名(扩展由 download_one 按 content-type 补)。"""
    out = _filename_from_url("https://example.com/get/12345")
    assert out == "12345"


def test_filename_from_url_root_path():
    """根路径 URL(``https://example.com/``) → 返 "image"。"""
    out = _filename_from_url("https://example.com/")
    assert out == "image"


# ---------------------------------------------------------------------------
# download_one 单图
# ---------------------------------------------------------------------------


def test_download_one_writes_file_with_indexed_name(tmp_path: Path, monkeypatch):
    """下载 1 张图 → 文件名 = {index:02d}_{原文件名},内容字节流写入文件。"""
    responses = {
        "https://cdn.example.com/photo.jpg": _FakeStreamResponse(
            _png_bytes(), content_type="image/png"
        ),
    }
    monkeypatch.setattr(dl_mod.requests, "get", _make_fake_get(responses))

    result = download_one(_entry("https://cdn.example.com/photo.jpg"), tmp_path, index=1)

    assert isinstance(result, DownloadedImage)
    assert result.file_path.exists()
    assert result.file_path.name == "01_photo.jpg"
    assert result.file_path.read_bytes() == _png_bytes()
    assert result.index == 1
    assert result.image_url == "https://cdn.example.com/photo.jpg"


def test_download_one_creates_directory(monkeypatch, tmp_path: Path):
    """out_dir 不存在 → 自动 mkdir(parents=True, exist_ok=True)。"""
    out_dir = tmp_path / "deep" / "nested" / "dir"
    responses = {
        "https://cdn.example.com/x.jpg": _FakeStreamResponse(
            _png_bytes(), content_type="image/jpeg"
        ),
    }
    monkeypatch.setattr(dl_mod.requests, "get", _make_fake_get(responses))

    result = download_one(_entry("https://cdn.example.com/x.jpg"), out_dir, index=1)

    assert out_dir.exists()
    assert result.file_path.parent == out_dir


def test_download_one_sets_referer_header(monkeypatch, tmp_path: Path):
    """Referer 头 = source_url(防盗链常见要求)。"""
    captured: Dict[str, Any] = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        return _FakeStreamResponse(_png_bytes(), content_type="image/jpeg")

    monkeypatch.setattr(dl_mod.requests, "get", fake_get)
    download_one(
        _entry(
            "https://cdn.example.com/x.jpg",
            source_url="https://example.com/article/42",
        ),
        tmp_path,
        index=1,
    )
    assert captured["headers"].get("Referer") == "https://example.com/article/42"


def test_download_one_rejects_non_image_content_type(monkeypatch, tmp_path: Path):
    """content-type 非 image/* → DownloaderError(防 HTML 错误页伪装成图)。"""
    responses = {
        "https://cdn.example.com/x.jpg": _FakeStreamResponse(
            b"<html>404 not found</html>", content_type="text/html"
        ),
    }
    monkeypatch.setattr(dl_mod.requests, "get", _make_fake_get(responses))

    with pytest.raises(DownloaderError, match="not an image content-type"):
        download_one(
            _entry("https://cdn.example.com/x.jpg"), tmp_path, index=1
        )


def test_download_one_raises_on_http_error(monkeypatch, tmp_path: Path):
    """HTTP 4xx/5xx → DownloaderError。"""
    responses = {
        "https://cdn.example.com/missing.jpg": _FakeStreamResponse(
            b"", content_type="image/jpeg", status_code=404
        ),
    }
    monkeypatch.setattr(dl_mod.requests, "get", _make_fake_get(responses))

    with pytest.raises(DownloaderError, match="404"):
        download_one(
            _entry("https://cdn.example.com/missing.jpg"), tmp_path, index=1
        )


def test_download_one_normalizes_protocol_relative_url(monkeypatch, tmp_path: Path):
    """gallery-dl 偶尔给出 ``//host/path`` 形式 URL(无 scheme),downloader 应自动补 https://。

    F2 验证(2026-09-03 xiaohongshu 实站):
      真实 URL:https://sns-webpic-qc.xhscdn.com/.../1.jpg → 下载成功
      噪点 URL://picasso-static.xiaohongshu.com/.../x.png → 旧版 requests.get 直接
        抛 InvalidSchema;新版在 downloader 入口补 https:// 后正常 GET。
    """
    captured: Dict[str, Any] = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        return _FakeStreamResponse(_png_bytes(), content_type="image/png")

    monkeypatch.setattr(dl_mod.requests, "get", fake_get)
    download_one(
        _entry("//picasso-static.xiaohongshu.com/fe-platform/x.png"),
        tmp_path,
        index=1,
    )
    # 真实请求 URL 必带 https:// 前缀
    assert captured["url"].startswith("https://"), (
        f"expected https:// prefix, got {captured['url']!r}"
    )
    assert "picasso-static.xiaohongshu.com" in captured["url"]


def test_download_one_uses_content_type_to_infer_extension(
    monkeypatch, tmp_path: Path
):
    """URL path 无扩展名时,按 content-type 补 .jpg / .png / .webp。"""
    responses = {
        "https://cdn.example.com/get/12345": _FakeStreamResponse(
            _png_bytes(), content_type="image/png"
        ),
    }
    monkeypatch.setattr(dl_mod.requests, "get", _make_fake_get(responses))

    result = download_one(_entry("https://cdn.example.com/get/12345"), tmp_path, index=1)
    assert result.file_path.suffix == ".png"
    assert result.file_path.name == "01_12345.png"


# ---------------------------------------------------------------------------
# download_images 批量
# ---------------------------------------------------------------------------


def test_download_images_batch_writes_all(tmp_path: Path, monkeypatch):
    """批量下载 3 张 → 3 个文件,序号 01/02/03。"""
    responses = {
        f"https://cdn.example.com/{i}.jpg": _FakeStreamResponse(
            _png_bytes(), content_type="image/jpeg"
        )
        for i in range(1, 4)
    }
    monkeypatch.setattr(dl_mod.requests, "get", _make_fake_get(responses))

    images = [
        _entry(f"https://cdn.example.com/{i}.jpg")
        for i in range(1, 4)
    ]
    results = download_images(images, tmp_path)

    assert len(results) == 3
    assert [r.index for r in results] == [1, 2, 3]
    assert [r.file_path.name for r in results] == [
        "01_1.jpg",
        "02_2.jpg",
        "03_3.jpg",
    ]
    # 全部成功
    assert all(r.file_path.exists() for r in results)


def test_download_images_continues_on_single_failure(tmp_path: Path, monkeypatch):
    """批量中第 2 张 404 → 其它成功,失败项以 status="failed" 形式返回。"""
    responses = {
        "https://cdn.example.com/1.jpg": _FakeStreamResponse(
            _png_bytes(), content_type="image/jpeg"
        ),
        "https://cdn.example.com/2.jpg": _FakeStreamResponse(
            b"", content_type="image/jpeg", status_code=404
        ),
        "https://cdn.example.com/3.jpg": _FakeStreamResponse(
            _png_bytes(), content_type="image/jpeg"
        ),
    }
    monkeypatch.setattr(dl_mod.requests, "get", _make_fake_get(responses))

    images = [
        _entry(f"https://cdn.example.com/{i}.jpg")
        for i in range(1, 4)
    ]
    results = download_images(images, tmp_path)

    # 3 项全返(失败的也带 result,error 字段)
    assert len(results) == 3
    success = [r for r in results if r.error is None]
    failed = [r for r in results if r.error is not None]
    assert len(success) == 2
    assert len(failed) == 1
    assert failed[0].index == 2
    assert "404" in (failed[0].error or "")


def test_download_images_dedup_filename(tmp_path: Path, monkeypatch):
    """同批次文件撞名 → 加 __2 / __3 后缀(序号本身保留)。"""
    # 2 张图 path 末段都是 "photo.jpg"——但 URL 不同
    responses = {
        "https://a.example.com/photo.jpg": _FakeStreamResponse(
            _png_bytes(), content_type="image/jpeg"
        ),
        "https://b.example.com/photo.jpg": _FakeStreamResponse(
            _png_bytes(), content_type="image/jpeg"
        ),
    }
    monkeypatch.setattr(dl_mod.requests, "get", _make_fake_get(responses))

    images = [
        _entry("https://a.example.com/photo.jpg"),
        _entry("https://b.example.com/photo.jpg"),
    ]
    results = download_images(images, tmp_path)

    names = [r.file_path.name for r in results]
    assert names == ["01_photo.jpg", "02_photo__2.jpg"]
    # 两个文件都落盘,内容不同 source
    assert all(r.file_path.exists() for r in results)


def test_download_images_respects_cancel_event(tmp_path: Path, monkeypatch):
    """cancel_event set 后,当前张收尾 → 后续张不入 results。"""
    cancel = threading.Event()
    cancel.set()  # 启动前就 set,所有 URL 都跳过

    images = [
        _entry("https://cdn.example.com/1.jpg"),
        _entry("https://cdn.example.com/2.jpg"),
    ]
    results = download_images(images, tmp_path, cancel_event=cancel)

    assert results == []


def test_download_images_emits_progress_callback(tmp_path: Path, monkeypatch):
    """逐 chunk 触发 on_progress(done_bytes, total_bytes) 回调。"""
    responses = {
        "https://cdn.example.com/1.jpg": _FakeStreamResponse(
            b"a" * 100, content_type="image/jpeg"
        ),
    }
    monkeypatch.setattr(dl_mod.requests, "get", _make_fake_get(responses))

    captured: List[Dict[str, int]] = []

    def on_progress(done: int, total: int) -> None:
        captured.append({"done": done, "total": total})

    images = [_entry("https://cdn.example.com/1.jpg")]
    download_images(images, tmp_path, on_progress=on_progress)

    # 至少 1 次 progress(收尾必发 done==total)
    assert len(captured) >= 1
    assert captured[-1] == {"done": 100, "total": 100}

