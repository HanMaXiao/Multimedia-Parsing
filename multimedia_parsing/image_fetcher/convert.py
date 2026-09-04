"""image_fetcher.convert — 图片转码 / 压缩 / 缩放(plan followup #3)。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §9(范围外)
+ plan followup 3 收口:可选项 — 把下到的图转 webp(更小)/ 压缩 jpeg / 限制尺寸。

设计要点:
  - 纯 Pillow 包装,与 downloader 解耦(``downloader`` 在 download 完后可选调)
  - 3 种模式可独立或叠加:
      - ``target_format='webp'``  → 转码(后缀自动改)
      - ``quality=N`` (1-100)    → 压缩质量(对 jpeg / webp 生效)
      - ``max_size=(w, h)``       → 等比缩放(长边 ≤ 限定值)
  - 透明降级:
      - 缺 Pillow → ImportError(顶上 hint 安装命令)
      - 损坏输入 / 不支持解码 → ConvertError
  - 错误隔离(由 caller 决定语义):转换失败不自动删原图,
    downloader 把 ConvertError 入 result.error 不断批

被拒(2026-09-03 followup 3):
  - 服务端转码(七牛 / OSS 图片处理)— 多 1 跳,本期本机 Pillow 够用
  - HEIC / AVIF 编码 — 桌面端解码一般够,编码不常用
  - 多线程并行转码 — 简单串行,跟 spec 串行流水线一致
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

# Pillow 顶层 lazy import — 测试无 Pillow 时 import 失败,但显式 raise 提示安装。
try:
    from PIL import Image  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - publisher extra 必有
    Image = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class ConvertError(RuntimeError):
    """图片转换失败(解码 / 编码 / 写盘 / 格式不支持)。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


# Pillow 输出格式白名单(避免用户传 jpeg2000 等少见格式)
_VALID_TARGET_FORMATS: frozenset = frozenset(
    {"webp", "jpeg", "jpg", "png", "gif", "bmp", "tiff"}
)


@dataclass(frozen=True)
class ConvertOptions:
    """转换选项。任意字段 None = 不做该步骤。

    Attributes:
        target_format: 目标格式;'webp' / 'jpeg' / 'png' 等。None = 保持原格式
        quality: 压缩质量 1-100;仅对 jpeg / webp 生效。None = 保持默认
        max_size: ``(width, height)`` 等比缩放的长边上限。None = 不缩放
    """

    target_format: Optional[str] = None
    quality: Optional[int] = None
    max_size: Optional[Tuple[int, int]] = None

    def __post_init__(self) -> None:
        if self.target_format is not None:
            fmt = self.target_format.lower().lstrip(".")
            if fmt not in _VALID_TARGET_FORMATS:
                raise ValueError(
                    f"target_format must be one of {sorted(_VALID_TARGET_FORMATS)}, "
                    f"got {self.target_format!r}"
                )
        if self.quality is not None:
            if not isinstance(self.quality, int) or isinstance(self.quality, bool):
                raise ValueError(f"quality must be an int, got {type(self.quality).__name__}")
            if not 1 <= self.quality <= 100:
                raise ValueError(f"quality must be 1-100, got {self.quality}")
        if self.max_size is not None:
            if (
                not isinstance(self.max_size, tuple)
                or len(self.max_size) != 2
                or not all(isinstance(v, int) for v in self.max_size)
            ):
                raise ValueError(
                    f"max_size must be (width, height) tuple of ints, got {self.max_size!r}"
                )


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------


def _require_pillow() -> None:
    if Image is None:
        raise ImportError(
            "Pillow is not installed. Install with: pip install Pillow"
        )


def convert_image(
    src: Path,
    dst: Path,
    options: ConvertOptions,
) -> Path:
    """把 ``src`` 图像按 ``options`` 转换后写到 ``dst``。

    Parameters
    ----------
    src:
        输入图像文件路径(Pillow 能解的格式)
    dst:
        输出文件路径(后缀决定 Pillow 编码格式,与 ``options.target_format`` 配合)
    options:
        转换选项;全 None = 拷贝原图(dst 后缀 = src 后缀)

    Returns:
        ``dst``(绝对路径,方便 caller 链式用)

    Raises:
        ImportError: Pillow 未装
        ConvertError: 解码 / 编码 / 写盘 / 格式不支持
    """
    _require_pillow()
    src_p = Path(src)
    dst_p = Path(dst)
    if not src_p.exists():
        raise ConvertError(f"source not found: {src_p}")

    target_fmt = (options.target_format or _infer_format(src_p)).lower().lstrip(".")
    try:
        with Image.open(src_p) as img:  # type: ignore[union-attr]
            # 2) 缩放(等比,长边 ≤ max_size)
            if options.max_size is not None:
                img.thumbnail(options.max_size, Image.Resampling.LANCZOS)  # type: ignore[attr-defined]
            # 3) 编码参数
            save_kwargs: dict = {}
            if options.quality is not None and target_fmt in ("jpeg", "jpg", "webp"):
                save_kwargs["quality"] = options.quality
            if target_fmt == "webp":
                save_kwargs.setdefault("method", 6)  # balanced effort
            # 4) 写
            if img.mode not in ("RGB", "RGBA", "P", "L", "LA"):
                # 兜底:含 alpha 通道的转 jpeg 必先转 RGB
                if target_fmt in ("jpeg", "jpg"):
                    img = img.convert("RGB")
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            img.save(dst_p, format=target_fmt, **save_kwargs)
    except ConvertError:
        raise
    except Exception as e:
        raise ConvertError(
            f"convert failed for {src_p} -> {dst_p} (target={target_fmt}): {e}"
        ) from e
    return dst_p


def _infer_format(path: Path) -> str:
    """从文件后缀推断格式(无后缀默认 PNG)。"""
    ext = path.suffix.lower().lstrip(".")
    if ext in _VALID_TARGET_FORMATS:
        # 统一 jpeg 别名
        return "jpeg" if ext == "jpg" else ext
    return "png"


__all__ = [
    "ConvertError",
    "ConvertOptions",
    "convert_image",
]

