"""test_image_fetcher_zip.py — image_fetcher.run_fetch zip 打包(followup #5)。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §9(范围外)
+ plan followup 5 收口:zip 打包上传/下载 — 把批量下载的图片打成单个 zip。

设计要点:
  - 新参数 ``zip_output: Optional[Path]`` — 设了就启用 zip 模式
  - zip 模式:下到临时目录,打 zip 成功 → 返 ``local_path = zip_output``,
    results 每个 entry 也带 zip 内相对路径(便于前端下载链接 / 复制)
  - 不动 mode/local/oss:zip 是「打包出口」,与 mode 正交
  - 临时目录 fetch_images 内部用 tempfile.mkdtemp,完成后清理
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from multimedia_parsing.image_fetcher import run_fetch as rf_mod
from multimedia_parsing.image_fetcher.downloader import DownloadedImage
from multimedia_parsing.image_fetcher.manifest import (
    ImageEntry,
    ImageFetchManifest,
)
from multimedia_parsing.image_fetcher.run_fetch import fetch_images, package_to_zip


# ---------------------------------------------------------------------------
# package_to_zip 单测
# ---------------------------------------------------------------------------


def test_package_to_zip_creates_zip_with_files(tmp_path: Path):
    """把 source_dir 下所有文件打进 zip。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "01_a.jpg").write_bytes(b"a-content")
    (src / "02_b.jpg").write_bytes(b"b-content")
    zip_path = tmp_path / "out.zip"

    package_to_zip(src, zip_path)

    assert zip_path.exists()
    assert zip_path.stat().st_size > 0
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
        assert names == ["01_a.jpg", "02_b.jpg"]
        assert zf.read("01_a.jpg") == b"a-content"
        assert zf.read("02_b.jpg") == b"b-content"


def test_package_to_zip_with_files_list(tmp_path: Path):
    """显式 files 列表模式:arcname = basename(全平铺,无视原目录结构)。"""
    f1 = tmp_path / "a.jpg"
    f2 = tmp_path / "sub" / "b.jpg"
    f2.parent.mkdir()
    f1.write_bytes(b"a-bytes")
    f2.write_bytes(b"b-bytes")
    zip_path = tmp_path / "out.zip"

    package_to_zip(zip_path=zip_path, files=[f1, f2])

    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
        assert names == ["a.jpg", "b.jpg"]  # 都在 zip 根目录,arcname = basename
        assert zf.read("a.jpg") == b"a-bytes"
        assert zf.read("b.jpg") == b"b-bytes"


def test_package_to_zip_files_list_skips_missing(tmp_path: Path):
    """files 列表里文件不存在 → 静默跳过(避免单条失败断整批)。"""
    f1 = tmp_path / "exists.jpg"
    f1.write_bytes(b"x")
    f_missing = tmp_path / "missing.jpg"  # 不创建
    zip_path = tmp_path / "out.zip"

    package_to_zip(zip_path=zip_path, files=[f1, f_missing])

    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["exists.jpg"]


def test_package_to_zip_empty_dir_creates_empty_zip(tmp_path: Path):
    """空目录也生成有效 zip(zip 头 + 0 entry)。"""
    src = tmp_path / "empty"
    src.mkdir()
    zip_path = tmp_path / "empty.zip"
    package_to_zip(src, zip_path)
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        # zip 内 0 文件,但 archive header 存在
        assert zf.namelist() == []


def test_package_to_zip_nested_dirs_preserves_structure(tmp_path: Path):
    """子目录结构保留(虽然本场景不常用,但 zip 行为正确性)。"""
    src = tmp_path / "nested"
    src.mkdir()
    (src / "sub").mkdir()
    (src / "sub" / "deep.jpg").write_bytes(b"deep")
    zip_path = tmp_path / "nested.zip"
    package_to_zip(src, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        # Windows 上可能用反斜杠,但 zipfile 走 /
        assert any(n.endswith("deep.jpg") for n in zf.namelist())


def test_package_to_zip_source_dir_not_exists_raises(tmp_path: Path):
    """source 不存在 → FileNotFoundError(避免静默生成空 zip 误导用户)。"""
    with pytest.raises(FileNotFoundError):
        package_to_zip(tmp_path / "does_not_exist", tmp_path / "out.zip")


# ---------------------------------------------------------------------------
# fetch_images zip_output 模式
# ---------------------------------------------------------------------------


def test_fetch_images_with_zip_output_packages_files(tmp_path: Path, monkeypatch):
    """zip_output 模式下,下载 → 打 zip → 清理临时目录,local_path = zip 路径。"""
    # Mock downloader:返 2 张成功,真建文件(让 package_to_zip 能找到)
    def fake_download(images, out_dir, **kwargs):
        results = []
        for i, img in enumerate(images):
            fp = out_dir / f"0{i+1}_{i+1}.jpg"
            fp.write_bytes(f"content-{i}".encode())  # 真建文件
            results.append(
                DownloadedImage(
                    file_path=fp,
                    image_url=img.url,
                    source_url=img.source_url,
                    index=i + 1,
                    error=None,
                )
            )
        return results
    monkeypatch.setattr(rf_mod, "downloader", MagicMock(download_images=fake_download))

    zip_path = tmp_path / "result.zip"
    manifest = ImageFetchManifest(
        batch_id="img-zip-001",
        mode="local",
        download_dir=str(tmp_path / "ignored"),  # zip 模式下被忽略
        images=[
            ImageEntry(url="https://cdn/a.jpg", source_url="https://example.com"),
            ImageEntry(url="https://cdn/b.jpg", source_url="https://example.com"),
        ],
    )
    results = fetch_images(manifest, zip_output=zip_path)

    assert all(r.is_success for r in results)
    # zip 存在
    assert zip_path.exists()
    # results 的 local_path 都指向 zip
    assert all(r.local_path == zip_path for r in results)
    # 验证 zip 内容
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
        assert "01_1.jpg" in names
        assert "02_2.jpg" in names


def test_fetch_images_with_zip_output_no_op_when_not_provided(
    tmp_path: Path, monkeypatch
):
    """未设 zip_output → 走原 local 模式(下载到 download_dir,不打包)。"""
    out_dir = tmp_path / "direct"

    def fake_download(images, out_dir, **kwargs):
        return [
            DownloadedImage(
                file_path=out_dir / f"0{i+1}_x.jpg",
                image_url=img.url,
                source_url=img.source_url,
                index=i + 1,
                error=None,
            )
            for i, img in enumerate(images)
        ]
    monkeypatch.setattr(rf_mod, "downloader", MagicMock(download_images=fake_download))

    manifest = ImageFetchManifest(
        batch_id="img-001",
        mode="local",
        download_dir=str(out_dir),
        images=[ImageEntry(url="https://cdn/x.jpg", source_url="https://example.com")],
    )
    results = fetch_images(manifest)
    # 不打 zip,local_path 指到具体文件
    assert results[0].local_path == out_dir / "01_x.jpg"
    assert not (out_dir.parent / "result.zip").exists()


def test_fetch_images_zip_output_partial_failure_still_packages_successful(
    tmp_path: Path, monkeypatch
):
    """zip 模式下单图失败:仍打 zip(只含成功的),失败的 result.error 标 failed。"""
    def fake_download(images, out_dir, **kwargs):
        # 第 1 张:成功,真建文件;第 2 张:失败
        fp1 = out_dir / "01_a.jpg"
        fp1.write_bytes(b"a")
        return [
            DownloadedImage(
                file_path=fp1,
                image_url=images[0].url,
                source_url=images[0].source_url,
                index=1,
                error=None,
            ),
            DownloadedImage(
                file_path=out_dir / "02_b.jpg",  # 不真建
                image_url=images[1].url,
                source_url=images[1].source_url,
                index=2,
                error="HTTP 404",
            ),
        ]
    monkeypatch.setattr(rf_mod, "downloader", MagicMock(download_images=fake_download))

    zip_path = tmp_path / "partial.zip"
    manifest = ImageFetchManifest(
        batch_id="img-partial",
        mode="local",
        download_dir=str(tmp_path / "tmp"),
        images=[
            ImageEntry(url="https://cdn/a.jpg", source_url="https://example.com"),
            ImageEntry(url="https://cdn/b.jpg", source_url="https://example.com"),
        ],
    )
    results = fetch_images(manifest, zip_output=zip_path)

    assert results[0].is_success
    assert results[1].error is not None
    # zip 仍生成(只含成功的 1 张)
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "01_a.jpg" in names
        assert "02_b.jpg" not in names  # 失败的没打进去


def test_fetch_images_zip_output_all_failed_no_zip(tmp_path: Path, monkeypatch):
    """zip 模式下全失败:不打 zip(zip 不存在),return 1 仍走 `all_success` 路径。"""
    def fake_download(images, out_dir, **kwargs):
        return [
            DownloadedImage(
                file_path=out_dir / "01_a.jpg",
                image_url=images[0].url,
                source_url=images[0].source_url,
                index=1,
                error="HTTP 500",
            ),
        ]
    monkeypatch.setattr(rf_mod, "downloader", MagicMock(download_images=fake_download))

    zip_path = tmp_path / "empty.zip"
    manifest = ImageFetchManifest(
        batch_id="img-allfail",
        mode="local",
        download_dir=str(tmp_path / "tmp"),
        images=[ImageEntry(url="https://cdn/a.jpg", source_url="https://example.com")],
    )
    results = fetch_images(manifest, zip_output=zip_path)
    assert results[0].error is not None
    # 全失败 → 不打 zip
    assert not zip_path.exists()

