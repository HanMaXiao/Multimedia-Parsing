"""image_fetcher.run_fetch — 解析 + 下载/上传 编排器。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §4.1 + §4.3 + F2.1。

设计要点:
  - **两阶段入口**(spec §4.1 + F3.2):
      - ``parse_urls(manifest)`` 解析阶段,逐 URL 调 ``resolver.parse_url``,
        回传 ``ImageParseResult`` list + 发 ``image_fetch_status phase=parse`` 事件。
      - ``fetch_images(manifest)`` 下载/上传阶段,逐张走 download → (optional upload),
        回传 ``ImageFetchResult`` list + 发 ``image_fetch_status phase=download/upload`` 事件。
  - **事件协议**(spec §4.3 + F3.1):走 ``event_emitter.emit_image_fetch_status``,
    通过 ``event_cb`` 注入(测试 capture,生产 stdout 双流)。
    progress shape:``{"done": N, "total": M}``(spec §4.3,区别于 video 模块的 float 0-100)。
  - **单失败不中断整批**(spec §8 验收 5):download / upload 单图失败 → 记 error 入
    result,继续后续。
  - **取消**:`cancel_event = threading.Event` set 后当前图收尾 → 后续图不入 results。
  - **OSS 复用 video_fetcher**(spec F2.1):``OssUploader.upload_with_key(local_path, key)``
    上传,key 在 image_fetcher 侧按 spec §3 D3 拼好(``{prefix}/{batch_id}/{序号}_{文件名}``)。

被拒:
  - 异步 asyncio.gather 并发 — spec §4.4 Rust 端串行 spawn,Python 端镜像串行更稳,
    失败语义清晰(每图独立 try/except)。
  - 复用 platform_status + **extra 塞 images — spec F3.1 拒绝新增 type 的前提
    (字段结构够用)此处不成立,故另立 image_fetch_status type。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from typing import TYPE_CHECKING

from . import downloader, resolver
from .downloader import DownloadedImage, _filename_from_url as _url_filename
from .manifest import ImageEntry, ImageFetchManifest, ImageParseManifest
from .resolver import ResolvedImage

if TYPE_CHECKING:
    # 仅供类型检查时用,运行时 import 走模块末尾 helper 避免循环依赖
    from multimedia_parsing.oss import OssUploader


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageParseResult:
    """单 URL 解析结果(对应 ``parse_urls`` 1 个入参 URL 的回传)。

    字段:
      - ``source_url``:原始 URL(spec §5 加性字段,事件 payload 必带)。
      - ``images``:解析出的图片列表(可能为空,部分页面无图)。
      - ``error``:失败原因,成功为 None。
    """

    source_url: str
    images: List[ResolvedImage]
    error: Optional[str] = None


@dataclass(frozen=True)
class ImageFetchResult:
    """单张图下载/上传结果(对应 ``fetch_images`` 1 个入参 image 的回传)。

    字段:
      - ``index``:1-based 序号(对应 manifest.images 位置)。
      - ``image_url``:原图片 URL(回传用)。
      - ``source_url``:该图所在源页 URL(回传用)。
      - ``local_path``:本地落盘绝对路径(mode=local / oss 都会有,因为 oss 模式先下到临时区)。
      - ``oss_url``:上传成功后的 OSS 直链(mode=oss)或 None(mode=local)。
      - ``error``:失败原因,成功为 None。
    """

    index: int
    image_url: str
    source_url: str
    local_path: Path
    oss_url: Optional[str] = None
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.error is None


# event_cb 签名:接受 dict(``image_fetch_status`` 完整 payload)。
EventCallback = Callable[[Dict[str, Any]], None]


# ---------------------------------------------------------------------------
# OSS key 构造(spec §3 D3 + F2.1)
# ---------------------------------------------------------------------------


def _build_image_oss_key(
    *,
    prefix: str,
    batch_id: str,
    index: int,
    filename: str,
) -> str:
    """拼图片 OSS object key:``{prefix}/{batch_id}/{index:02d}_{filename}``。

    文件名中含 ``/`` 时替换为 ``_`` 避免误判目录层级(spec §6 + F2.1)。
    """
    safe_filename = filename.replace("/", "_")
    parts: List[str] = []
    prefix = (prefix or "").strip("/")
    if prefix:
        parts.append(prefix)
    if batch_id:
        parts.append(batch_id)
    parts.append(f"{index:02d}_{safe_filename}")
    return "/".join(parts)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _to_event_dict(payload: Dict[str, Any]) -> Dict[str, Any]:
    """event_cb 直接接受 dict;本地复用时去掉 None 字段(与 emit 行为对齐)。"""
    return {k: v for k, v in payload.items() if v is not None}


def _emit(
    event_cb: Optional[EventCallback],
    *,
    batch_id: str,
    phase: str,
    status: str,
    source_url: Optional[str] = None,
    image_url: Optional[str] = None,
    images: Optional[List[Dict[str, Any]]] = None,
    progress: Optional[Dict[str, int]] = None,
    url: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """本地 emit 包装(不走 stdout,直接构造 payload 调 event_cb)。"""
    if event_cb is None:
        return
    payload: Dict[str, Any] = {
        "type": "image_fetch_status",
        "batch_id": batch_id,
        "phase": phase,
        "status": status,
    }
    if source_url is not None:
        payload["source_url"] = source_url
    if image_url is not None:
        payload["image_url"] = image_url
    if images is not None:
        payload["images"] = images
    if progress is not None:
        payload["progress"] = progress
    if url is not None:
        payload["url"] = url
    if error is not None:
        payload["error"] = error
    event_cb(payload)


# ---------------------------------------------------------------------------
# 阶段 1:parse_urls
# ---------------------------------------------------------------------------


def parse_urls(
    manifest: ImageParseManifest,
    *,
    event_cb: Optional[EventCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> List[ImageParseResult]:
    """逐 URL 调 ``resolver.parse_url``,emit parse 事件,回传 ImageParseResult list。

    单 URL 失败不中断整批(spec §8 验收 5) — 失败时 emit ``status=failed`` + error,
    继续下一个 URL。事件 payload 形状见 spec §4.3 / emit_image_fetch_status docstring。
    """
    results: List[ImageParseResult] = []
    total = len(manifest.urls)
    for idx, url in enumerate(manifest.urls):
        if cancel_event is not None and cancel_event.is_set():
            break
        _emit(
            event_cb,
            batch_id=manifest.batch_id,
            phase="parse",
            status="processing",
            source_url=url,
        )
        try:
            images = resolver.parse_url(url)
        except Exception as e:
            err = f"parse: {e}"
            _emit(
                event_cb,
                batch_id=manifest.batch_id,
                phase="parse",
                status="failed",
                source_url=url,
                error=err,
            )
            results.append(
                ImageParseResult(source_url=url, images=[], error=err)
            )
            continue
        # 成功 — images 字段序列化为 [{url, width, height}, ...]
        images_payload = [
            {"url": img.url, "width": img.width, "height": img.height}
            for img in images
        ]
        _emit(
            event_cb,
            batch_id=manifest.batch_id,
            phase="parse",
            status="success",
            source_url=url,
            images=images_payload,
        )
        results.append(ImageParseResult(source_url=url, images=images))
    return results


# ---------------------------------------------------------------------------
# 阶段 2:fetch_images
# ---------------------------------------------------------------------------


_UNSET = object()  # 哨兵:区分"未传"和"显式 None"(F2.1 镜像 video_fetcher F5 模式)


def _get_local_path_str(dl: DownloadedImage) -> str:
    """从 DownloadedImage 拿 file:// URL(本地模式用)。"""
    p = Path(dl.file_path)
    if not p.is_absolute():
        p = p.resolve()
    return f"file://{p.as_posix()}"


def fetch_images(
    manifest: ImageFetchManifest,
    *,
    event_cb: Optional[EventCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    uploader: Any = _UNSET,
    zip_output: Optional[Path] = None,
) -> List[ImageFetchResult]:
    """逐张走 download → (optional upload OSS),emit download/upload 事件。

    Parameters
    ----------
    manifest:
        已校验的 ImageFetchManifest(mode=local/oss + images + 配置)。
    event_cb:
        接受 dict 的回调,None 时静默(测试 / 离线预览)。Phase 3 publisher CLI
        入口把 event_cb 接到 ``make_event_cb``(stdout 双流通道)。
    cancel_event:
        ``threading.Event``;set 后当前张收尾 → 后续张不入 results。
    uploader:
        注入 OssUploader 实例(测试用 fake;默认行为按 manifest.mode 决定):
          - 未传 + ``manifest.mode == 'local'`` → 不上传,跳过 upload 步骤
          - 未传 + ``manifest.mode == 'oss'`` → 构造默认 OssUploader(走 OSS 模式)
          - 显式传 OssUploader 实例 → 用它(测试 fake)
        显式传 None + manifest.mode == 'oss' → 错(应由 manifest 校验层拦)。

    Returns
    -------
    list[ImageFetchResult]:
        长度 = len(manifest.images);顺序与 images 一致。每条带 success / error 终态。
    """
    if uploader is _UNSET:
        if manifest.mode == "oss":
            uploader = OssUploader(manifest.oss) if manifest.oss is not None else None
        else:
            uploader = None

    if manifest.mode == "oss" and uploader is None:
        # 防御兜底(manifest 校验层已拒,这里再挡一道)
        raise ValueError(
            "fetch_images: mode='oss' requires an OssUploader instance "
            "(manifest.oss missing or uploader=None)"
        )

    images = manifest.images
    total = len(images)
    results: List[ImageFetchResult] = []
    done_count = 0  # 跨 phase 共享的 done 计数

    # zip 模式:用临时目录收下载,事后 package_to_zip → 清理。
    # 与 mode 正交 — local/oss 都可加 zip。
    zip_temp_dir: Optional[Path] = None
    actual_download_dir: Path
    if zip_output is not None:
        zip_temp_dir = Path(tempfile.mkdtemp(prefix="image_fetch_zip_"))
        actual_download_dir = zip_temp_dir
    else:
        # manifest.download_dir 可能是 str(ImageFetchManifest 接受)→ 统一转 Path
        actual_download_dir = (
            Path(manifest.download_dir) if manifest.download_dir else Path("./.image_fetch_tmp")
        )

    # 1) 下载阶段(批量)— 失败 / 取消由 download_images 内部消化
    if cancel_event is not None and cancel_event.is_set():
        _maybe_cleanup_zip_temp(zip_temp_dir)
        return []
    download_results: List[DownloadedImage] = downloader.download_images(
        images,
        actual_download_dir,
        cancel_event=cancel_event,
    )

    # 2) 逐张 emit 事件 + (OSS 模式)上传
    for dl in download_results:
        if cancel_event is not None and cancel_event.is_set():
            break
        # download 阶段:processing 已经在 download_images 内部陆续发? 不 — 当前实现不发
        # 这里发统一的 download processing + success/failed
        _emit(
            event_cb,
            batch_id=manifest.batch_id,
            phase="download",
            status="processing",
            source_url=dl.source_url,
            image_url=dl.image_url,
            progress={"done": done_count, "total": total},
        )
        if dl.error is not None:
            _emit(
                event_cb,
                batch_id=manifest.batch_id,
                phase="download",
                status="failed",
                source_url=dl.source_url,
                image_url=dl.image_url,
                progress={"done": done_count, "total": total},
                error=f"download: {dl.error}",
            )
            results.append(
                ImageFetchResult(
                    index=dl.index,
                    image_url=dl.image_url,
                    source_url=dl.source_url,
                    local_path=dl.file_path,
                    oss_url=None,
                    error=f"download: {dl.error}",
                )
            )
            done_count += 1
            continue
        # download success
        local_url = _get_local_path_str(dl)
        _emit(
            event_cb,
            batch_id=manifest.batch_id,
            phase="download",
            status="success",
            source_url=dl.source_url,
            image_url=dl.image_url,
            progress={"done": done_count + 1, "total": total},
            url=local_url,
        )
        # local 模式:download success 即终态
        if manifest.mode == "local":
            results.append(
                ImageFetchResult(
                    index=dl.index,
                    image_url=dl.image_url,
                    source_url=dl.source_url,
                    local_path=dl.file_path,
                    oss_url=None,
                    error=None,
                )
            )
            done_count += 1
            continue
        # OSS 模式:upload 阶段
        _emit(
            event_cb,
            batch_id=manifest.batch_id,
            phase="upload",
            status="processing",
            source_url=dl.source_url,
            image_url=dl.image_url,
            progress={"done": done_count, "total": total},
        )
        try:
            key = _build_image_oss_key(
                prefix=manifest.oss.path_prefix if manifest.oss else "",
                batch_id=manifest.batch_id,
                index=dl.index,
                # 用原 URL 末段,不是已加序号的 file_path.name(避免 key 双序号)
                filename=_url_filename(dl.image_url),
            )
            oss = uploader.upload_with_key(dl.file_path, key)
        except Exception as e:
            err = f"upload: {e}"
            _emit(
                event_cb,
                batch_id=manifest.batch_id,
                phase="upload",
                status="failed",
                source_url=dl.source_url,
                image_url=dl.image_url,
                progress={"done": done_count, "total": total},
                error=err,
            )
            results.append(
                ImageFetchResult(
                    index=dl.index,
                    image_url=dl.image_url,
                    source_url=dl.source_url,
                    local_path=dl.file_path,
                    oss_url=None,
                    error=err,
                )
            )
            done_count += 1
            continue
        _emit(
            event_cb,
            batch_id=manifest.batch_id,
            phase="upload",
            status="success",
            source_url=dl.source_url,
            image_url=dl.image_url,
            progress={"done": done_count + 1, "total": total},
            url=oss.url,
        )
        results.append(
            ImageFetchResult(
                index=dl.index,
                image_url=dl.image_url,
                source_url=dl.source_url,
                local_path=dl.file_path,
                oss_url=oss.url,
                error=None,
            )
        )
        done_count += 1

    # zip 模式:成功后打包,失败的全失败才不打 zip(部分失败仍打 — 含成功的)
    if zip_output is not None:
        successful_results = [r for r in results if r.is_success]
        if successful_results:
            # 精确文件列表:从 download_results 拿真实成功项的文件路径,
            # 避免目录扫描误打(临时目录里可能含 .part 半成品)
            files_to_zip = [r.local_path for r in successful_results]
            package_to_zip(zip_path=Path(zip_output), files=files_to_zip)
            # 所有 successful 的 local_path 统一指向 zip(frozen dataclass,rebuild)
            zip_path = Path(zip_output)
            results = [
                r if not r.is_success
                else ImageFetchResult(
                    index=r.index,
                    image_url=r.image_url,
                    source_url=r.source_url,
                    local_path=zip_path,
                    oss_url=r.oss_url,
                    error=r.error,
                )
                for r in results
            ]
        # 清理临时目录(无论全成功/部分成功/全失败)
        _maybe_cleanup_zip_temp(zip_temp_dir)
    return results


# ---------------------------------------------------------------------------
# zip 打包 helper(plan followup 5)
# ---------------------------------------------------------------------------


def package_to_zip(
    source_dir: Path = None,
    zip_path: Path = None,
    *,
    files: Optional[List[Path]] = None,
) -> Path:
    """把 ``source_dir`` 下所有文件,或显式 ``files`` 列表,打成 zip。

    两种用法:
      - ``package_to_zip(source_dir, zip_path)``:目录 → 递归 zip(保留子目录结构)
      - ``package_to_zip(zip_path=zip_path, files=[p1, p2, ...])``:精确文件列表 → zip,
        arcname = basename(便于 fetch_images 收集 successful 结果打 zip)

    Returns:
        ``zip_path``(绝对路径,方便 caller 链式用)。

    Raises:
        FileNotFoundError: source_dir 不存在(避免静默生成误导性空 zip)
        ValueError: source_dir 和 files 都没传。
    """
    target = Path(zip_path) if zip_path is not None else None
    if target is None:
        raise ValueError("zip_path is required")
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if files is not None:
            # 精确文件列表模式:arcname = basename(全平铺到 zip 根)
            for fp in files:
                fp = Path(fp)
                if fp.exists() and fp.is_file():
                    zf.write(fp, arcname=fp.name)
        elif source_dir is not None:
            # 目录模式:递归保留子目录结构
            source = Path(source_dir)
            if not source.exists():
                raise FileNotFoundError(f"source_dir does not exist: {source}")
            for root in source.rglob("*"):
                if root.is_file():
                    zf.write(root, arcname=str(root.relative_to(source)))
        else:
            raise ValueError("either source_dir or files must be provided")
    return target


def _maybe_cleanup_zip_temp(temp_dir: Optional[Path]) -> None:
    """zip 模式结束(成功 / 失败 / 取消)后清理临时目录,best-effort 不抛。"""
    if temp_dir is None:
        return
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass  # 清理失败不影响主流程


__all__ = [
    "EventCallback",
    "ImageFetchResult",
    "ImageParseResult",
    "fetch_images",
    "package_to_zip",
    "parse_urls",
]


# ---------------------------------------------------------------------------
# 跨包 import helper — 避免循环 import,延迟到 module body 末尾
# ---------------------------------------------------------------------------
# OssUploader 从 video_fetcher 导入;image_fetcher 依赖 video_fetcher,反之不行。
# 这里走"模块末尾 helper"模式,所有需要 OssUploader 的代码先于本行执行没问题
# (Python 解释器在首次访问时才解析此行);Phase 3 也要用 OssUploader,统一在此。
from multimedia_parsing.oss import OssUploader  # F0.5 2026-09-03 上提; 旧 video_fetcher.oss_uploader 留 deprecation shim

