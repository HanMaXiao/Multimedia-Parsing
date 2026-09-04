"""image_fetcher.downloader — requests 流式图片下载 + 防覆盖命名 + content-type 校验。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §4.1 + F2.2。

设计要点:
  - **流式下载**:``requests.get(stream=True)`` + ``iter_content(chunk_size=8KB)``,
    大图不全载入内存(spec F2.2:图片是简单 HTTP GET,直接 requests 即可,无需 yt-dlp)。
  - **防盗链**:`Referer` 头 = ``source_url``(该图所在源页 URL),多数图片 CDN 鉴权要求。
  - **content-type 校验**(F2.2):首 chunk 读出 ``Content-Type`` 响应头,必须以
    ``image/`` 开头;否则 ``DownloaderError``(防 HTML 错误页 / 登录页伪装成图)。
  - **文件名**:``{index:02d}_{原文件名}``(spec §4.1 + 验收 §3)— 原文件名从 URL path
    末段取,无扩展时按 content-type 推 ``.jpg`` / ``.png`` / ``.webp`` / ``.gif`` /
    ``.avif``;序号是图片在 manifest.images 中的 1-based 索引。
  - **去重命名**:同批次内文件名撞了 → 加 ``__2`` / ``__3`` 后缀(序号本身保留,确保
    每个文件首位数字 = manifest 位置;后缀只解决撞名)。
  - **失败不中断整批**(spec §8 验收 5):``download_images`` 单张失败时记 error 字段
    入 result,继续后续;调用方 ``run_fetch`` 据此决定事件 status。
  - **取消**:``cancel_event`` set 后,当前张收尾,后续张不入 results(spec §8 验收 8)。
  - **进度回调**:``on_progress(done_bytes, total_bytes)`` — 总字节数用
    ``Content-Length``(无则用已收字节作为 done,total=None 让上层降级处理)。

被拒(2026-09-02 F2.2 决策):
  - gallery-dl 自带 downloader — 与 extractor 耦合,通用解析分支(BS4)用不了。
  - yt-dlp 的 downloader — 大材小用,图片就是简单 HTTP GET。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urlparse

import requests  # 已在项目根 requirements

from .manifest import ImageEntry


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class DownloaderError(RuntimeError):
    """图片下载失败(网络 / content-type 错 / 写盘失败 / 取消)。"""


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DownloadedImage:
    """单张图下载结果视图(批量的最小单元)。

    字段:
      - ``file_path``:本地落盘绝对路径。
      - ``image_url``:原图片 URL(回传用,event payload 必带)。
      - ``source_url``:该图来源页 URL(回传用,event payload 必带)。
      - ``index``:1-based 序号(对应 manifest.images 位置)。
      - ``error``:失败时的人类可读错误描述;成功为 None。
    """

    file_path: Path
    image_url: str
    source_url: str
    index: int
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.error is None and self.file_path.exists()


# 进度回调签名:on_progress(done_bytes: int, total_bytes: Optional[int])
ProgressCallback = Callable[[int, Optional[int]], None]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


# chunk 大小 — 8KB 平衡内存与吞吐量(requests 文档推荐值)
_CHUNK_SIZE = 8 * 1024

# content-type → 扩展名 映射(URL path 无扩展时兜底)
_CONTENT_TYPE_TO_EXT: dict = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",  # 一般会被 _filter_and_dedupe 过滤,此处只是兜底
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _filename_from_url(url: str) -> str:
    """从 URL 拿文件名 — path 末段;无扩展或为空 → "image"。"""
    try:
        p = urlparse(url)
    except (ValueError, TypeError):
        return "image"
    path = p.path or ""
    name = path.rsplit("/", 1)[-1] if path else ""
    name = name.strip()
    if not name:
        return "image"
    return name


def _infer_extension(content_type: str) -> str:
    """content-type → 扩展名(URL path 无扩展时用)。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    return _CONTENT_TYPE_TO_EXT.get(ct, ".bin")


def _indexed_filename(index: int, base_name: str) -> str:
    """``{index:02d}_{base_name}`` 命名,index 从 1 起。"""
    return f"{index:02d}_{base_name}"


def _dedup_filename(used_names: dict, base_name: str) -> int:
    """同批次内同 base_name 出现次数(1-indexed)。首次返 1。

    ``used_names`` 在 ``download_images`` 内维护,key = base_name,value = 出现次数。
    """
    n = used_names.get(base_name, 0) + 1
    used_names[base_name] = n
    return n


# ---------------------------------------------------------------------------
# 单图下载
# ---------------------------------------------------------------------------


def download_one(
    image: ImageEntry,
    out_dir: Path,
    index: int,
    *,
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> DownloadedImage:
    """下载单张图片到 out_dir,文件名 = ``{index:02d}_{原文件名}``。

    Parameters
    ----------
    image:
        待下载的 ImageEntry(url + source_url + 可选 width/height)。
    out_dir:
        本地落盘目录(不存在则自动 mkdir)。
    index:
        1-based 序号(用于文件名前缀,供 ``download_images`` 批处理用)。
    cancel_event:
        ``threading.Event``;set 后本次调用立即返回(由 ``download_images`` 收尾)。
    on_progress:
        进度回调,签名 ``(done_bytes, total_bytes)``。可选,不发进度事件可省。

    Returns
    -------
    DownloadedImage:
        成功时 file_path 存在;失败时 file_path 可能不存在,error 字段填错。

    Raises
    ------
    DownloaderError:
        致命错误(网络 / content-type / 写盘)— 失败仍以返回值形式由 ``download_images``
        接管,这里 raise 是为了细粒度测试与一致性。
    """
    if cancel_event is not None and cancel_event.is_set():
        # 取消不视为错误,直接返(由 caller 决定是否算 cancelled)
        return DownloadedImage(
            file_path=Path(""),
            image_url=image.url,
            source_url=image.source_url,
            index=index,
            error=None,  # cancelled 走单独路径,见 download_images
        )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base_name = _filename_from_url(image.url)
    indexed_name = _indexed_filename(index, base_name)
    file_path = out_dir / indexed_name

    # 补全 protocol-relative URL(gallery-dl 偶尔给 ``//host/path`` 没 scheme)
    request_url = image.url
    if request_url.startswith("//"):
        request_url = "https:" + request_url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Referer": image.source_url,  # 防盗链:源页 URL
    }

    try:
        resp = requests.get(request_url, headers=headers, stream=True, timeout=30)
    except Exception as e:
        raise DownloaderError(f"requests.get failed for {image.url!r}: {e}") from e

    try:
        if resp.status_code >= 400:
            raise DownloaderError(
                f"HTTP {resp.status_code} for {image.url!r}"
            )
        # content-type 校验(只能读 1 次 stream,先 peek headers)
        content_type = resp.headers.get("Content-Type", "").lower()
        if not content_type.startswith("image/"):
            raise DownloaderError(
                f"not an image content-type for {image.url!r}: {content_type!r}"
            )
        # URL path 无扩展 → 按 content-type 补扩展名
        if not Path(base_name).suffix:
            file_path = file_path.with_suffix(_infer_extension(content_type))

        # 写盘
        total = None
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit():
            total = int(cl)
        done = 0
        with file_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=_CHUNK_SIZE):
                if cancel_event is not None and cancel_event.is_set():
                    # 取消中,关 stream + 删半成品
                    resp.close()
                    try:
                        file_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return DownloadedImage(
                        file_path=file_path,
                        image_url=image.url,
                        source_url=image.source_url,
                        index=index,
                        error=None,  # cancelled 走单独路径
                    )
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if on_progress is not None:
                    on_progress(done, total)
        # 收尾必触发一次 100% 进度(若 total 缺失,total=done)
        if on_progress is not None:
            on_progress(done, total if total is not None else done)
    finally:
        resp.close()

    return DownloadedImage(
        file_path=file_path,
        image_url=image.url,
        source_url=image.source_url,
        index=index,
        error=None,
    )


# ---------------------------------------------------------------------------
# 批量下载
# ---------------------------------------------------------------------------


def download_images(
    images: List[ImageEntry],
    out_dir: Path,
    *,
    cancel_event: Optional[threading.Event] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> List[DownloadedImage]:
    """逐张下载图片到 out_dir,失败不中断整批(单张错入 result.error)。

    Parameters
    ----------
    images:
        待下载图片列表(顺序决定文件名前缀 index 1..N)。
    out_dir:
        本地落盘目录(不存在自动 mkdir,父目录也会建)。
    cancel_event:
        ``threading.Event``;set 后当前张收尾 → 后续张不入 results。
    on_progress:
        进度回调(``on_progress(done, total)``),逐 chunk 触发;批量模式下 done/total
        表达"单张内"的进度,跨张独立计数。

    Returns
    -------
    list[DownloadedImage]:
        与 ``images`` 等长,每条带 success / error 终态。失败的 file_path 可能不存在
        (cleanup 取决于具体错误;非 cancelled 错误保留半成品便于排查)。

    去重策略:
      - 同 base_name(URL path 末段)多次出现 → 第 1 张 = 原始 indexed 名;第 2/3/... 张
        = 在 stem 后追加 ``__N``(保留扩展名,spec §8 验收 3 "无覆盖")。
      - 序号(indexed 前缀)始终是 1..N,不参与去重 — 保证文件名首位数字 = manifest
        位置,用户回查方便。
    """
    out_dir = Path(out_dir)
    used_names: dict = {}  # base_name → 出现次数
    results: List[DownloadedImage] = []
    for i, image in enumerate(images, start=1):
        if cancel_event is not None and cancel_event.is_set():
            # 后续整批跳过(spec §8 验收 8)
            break
        base_name = _filename_from_url(image.url)
        occurrence = _dedup_filename(used_names, base_name)  # 1, 2, 3, ...
        try:
            dl = download_one(
                image,
                out_dir,
                index=i,
                cancel_event=cancel_event,
                on_progress=on_progress,
            )
        except DownloaderError as e:
            results.append(
                DownloadedImage(
                    file_path=out_dir
                    / _indexed_filename(i, base_name),  # 可能没真存在
                    image_url=image.url,
                    source_url=image.source_url,
                    index=i,
                    error=str(e),
                )
            )
            continue
        except Exception as e:  # 防御兜底(写盘 / 权限等)
            results.append(
                DownloadedImage(
                    file_path=out_dir / _indexed_filename(i, base_name),
                    image_url=image.url,
                    source_url=image.source_url,
                    index=i,
                    error=f"unexpected: {e}",
                )
            )
            continue
        if dl.error is not None:
            # cancelled(在 download_one 内已 unlink 半成品)
            results.append(dl)
            continue
        if not dl.is_success:
            # download_one 报了 error 但又没 raise(当前实现没这条路径,防御)
            results.append(dl)
            continue
        # occurrence > 1 → 加 __N 后缀(保留 download_one 已推断的扩展名)
        if occurrence > 1:
            p = Path(dl.file_path.name)
            new_name = f"{p.stem}__{occurrence}{p.suffix}"
            new_path = dl.file_path.parent / new_name
            dl.file_path.rename(new_path)
            results.append(
                DownloadedImage(
                    file_path=new_path,
                    image_url=image.url,
                    source_url=image.source_url,
                    index=i,
                    error=None,
                )
            )
        else:
            results.append(dl)
    return results


__all__ = [
    "DownloadedImage",
    "DownloaderError",
    "ProgressCallback",
    "download_images",
    "download_one",
]

