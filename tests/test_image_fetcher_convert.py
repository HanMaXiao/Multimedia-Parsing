"""test_image_fetcher_convert.py — 图片转码/压缩/webp(plan followup #3)。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §9(范围外)
+ plan followup 3 收口:可选项 — 把下到的图转 webp / 压缩 jpeg / 改 size。

设计要点:
  - 新模块 ``image_fetcher.convert`` — 纯 Pillow 包装,与 downloader 解耦
  - 支持 3 种模式:
      - ``target_format='webp'``  → 转码到 webp(更小)
      - ``quality=N`` (1-100)    → 压缩 jpeg / webp 质量
      - ``max_size=(w, h)``       → 等比缩放
  - downloader 集成:``download_one(image, out_dir, index, *, convert_options=...)``
    走转换路径后,file_path 指向 .webp
  - 透明降级:目标格式不支持(如 .svg → webp)→ raise ConvertError
  - 错误隔离:转换失败不阻断整批 — downloader 把 ConvertError 入 result.error

被拒(2026-09-03 followup 3):
  - 服务端转码(七牛 / OSS 图片处理)— 多 1 跳,本期本机 Pillow 够用
  - HEIC / AVIF 编码 — 桌面端解码一般够,编码不常用
  - 多线程并行转码 — 简单串行,跟 spec 串行流水线一致
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from multimedia_parsing.image_fetcher import convert as cvt
from multimedia_parsing.image_fetcher.convert import (
    ConvertError,
    ConvertOptions,
    convert_image,
)


# ---------------------------------------------------------------------------
# ConvertOptions
# ---------------------------------------------------------------------------


def test_convert_options_defaults():
    """默认无任何转换(原图原样)。"""
    o = ConvertOptions()
    assert o.target_format is None
    assert o.quality is None
    assert o.max_size is None


def test_convert_options_validation():
    """非法参数 → ValueError(防御式校验)。"""
    with pytest.raises(ValueError, match="target_format"):
        ConvertOptions(target_format="xyz")  # 不在白名单
    with pytest.raises(ValueError, match="quality"):
        ConvertOptions(quality=0)  # 必须 1-100
    with pytest.raises(ValueError, match="quality"):
        ConvertOptions(quality=101)
    with pytest.raises(ValueError, match="max_size"):
        ConvertOptions(max_size=(100,))  # 必须 2 元


# ---------------------------------------------------------------------------
# convert_image 单图
# ---------------------------------------------------------------------------


def _write_png(path: Path, color: str = "red", size=(100, 100)) -> Path:
    """造一个测试 PNG 图像。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    img.save(path, "PNG")
    return path


def test_convert_image_to_webp(tmp_path: Path):
    """PNG → webp 转码,文件存在 + 后缀正确 + 可读。"""
    src = _write_png(tmp_path / "src.png", color="blue", size=(200, 150))
    dst = tmp_path / "out.webp"
    options = ConvertOptions(target_format="webp", quality=85)

    out_path = convert_image(src, dst, options)

    assert out_path == dst
    assert dst.exists()
    assert dst.stat().st_size > 0
    # 可读 + 尺寸保留
    with Image.open(dst) as img:
        assert img.size == (200, 150)
        assert img.format == "WEBP"


def test_convert_image_with_quality_shrinks_file(tmp_path: Path):
    """低 quality 比高 quality 文件小(同 source / 同 format)。"""
    # 用 noise 图像(高频信息多,quality 差异明显)
    src = tmp_path / "src.png"
    img = Image.effect_noise((400, 300), 123)
    img.save(src, "PNG")

    high = tmp_path / "high.jpg"
    low = tmp_path / "low.jpg"
    convert_image(src, high, ConvertOptions(target_format="jpeg", quality=95))
    convert_image(src, low, ConvertOptions(target_format="jpeg", quality=20))

    assert high.stat().st_size > low.stat().st_size, (
        f"high quality ({high.stat().st_size}) should be larger than low ({low.stat().st_size})"
    )


def test_convert_image_max_size_scales_proportionally(tmp_path: Path):
    """max_size 等比缩放(200x100 → 100x50,不变形)。"""
    src = _write_png(tmp_path / "src.png", size=(200, 100))
    dst = tmp_path / "out.png"
    options = ConvertOptions(max_size=(100, 100))

    convert_image(src, dst, options)

    with Image.open(dst) as img:
        # 200x100 → 等比缩放到 100x50(因 width 限制)
        assert img.size == (100, 50)


def test_convert_image_no_options_just_copies(tmp_path: Path):
    """options 全 None(默认)→ 直接拷贝,不做任何处理(快速路径)。"""
    src = _write_png(tmp_path / "src.png")
    dst = tmp_path / "out.png"
    out_path = convert_image(src, dst, ConvertOptions())
    assert out_path == dst
    assert dst.exists()
    # 内容应一致
    with Image.open(dst) as img:
        assert img.size == (100, 100)


def test_convert_image_unsupported_format_raises(tmp_path: Path):
    """Pillow 无法解码损坏文件 → ConvertError。"""
    # 直接造一个损坏文件
    src = tmp_path / "fake.bmp"
    src.write_bytes(b"not a real bmp file")
    dst = tmp_path / "out.bmp"
    with pytest.raises(ConvertError, match="convert failed"):
        convert_image(src, dst, ConvertOptions())


def test_convert_image_preserves_aspect_ratio(tmp_path: Path):
    """max_size 不强制 1:1 — 等比缩放,只适配一边的限制。"""
    src = _write_png(tmp_path / "src.png", size=(800, 400))  # 2:1
    dst = tmp_path / "out.png"
    convert_image(src, dst, ConvertOptions(max_size=(400, 400)))
    with Image.open(dst) as img:
        assert img.size == (400, 200)  # 2:1 保持

