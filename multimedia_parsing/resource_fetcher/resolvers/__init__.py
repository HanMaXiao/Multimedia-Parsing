"""resource_fetcher.resolvers — 各资源类型的 ResourceResolver 实现.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §4.

本期(2026-09-03):
  - video.py: VideoResolver — Commit 2 包装 video_fetcher (yt-dlp 链路).
  - image.py: ImageResolver — Commit 2 包装 image_fetcher (gallery-dl + BS4 链路).

Phase 7+ (2026-09-04) — 平台特定 / 资源类型扩展样板 (同事扩展新平台用):
  - xiaohongshu.py: XiaohongshuResolver — F0.1 智能 dispatch (跨 video/image 终判).
  - bilibili.py: BilibiliResolver — 继承 VideoResolver, 加 B 站特定元数据.
  - youtube.py: YouTubeResolver — 继承 VideoResolver, 加 YouTube 特定元数据.
  - audio.py: AudioResolver — 真实现, 走 yt-dlp audio-only mode (soundcloud / podcast 等).
  - model.py: ModelResolver — D2 决策 stub, 同事未来扩展 huggingface / civitai 等.

设计层级:
  - **基类** ResourceResolver (base.py Protocol): parse(url) + fetch(item, dest).
  - **通用 resolver**: VideoResolver / ImageResolver / AudioResolver — 走通用工具链.
  - **平台特定扩展** (继承通用): BilibiliResolver / YouTubeResolver — 加平台特定 metadata.
  - **混合 dispatch** (跨类型终判): XiaohongshuResolver — F0.1 拿 note_info 判 video/image.
  - **占位 stub**: ModelResolver — 接口就位, 实现等同事扩展.

扩展新平台工作流 (详见 resolver-extension skill, C:/Users/xiaoge/.minimax/skills/):
  1. 评估: 走现有通用 resolver 够吗? 够 → 改 router.py 加一行注册即可.
  2. 不够 → 选模板 (通用增强 / 平台 dispatch / 音频 / 模型), 复制对应文件.
  3. 改 ``_platform_domains`` + 重写 parse/fetch.
  4. router.py 加 ``ResolverRule`` 注册.
  5. 加 tests/test_{platform}_resolver.py (4 类测试: parse / fetch / fetch_wrong_type / router).
  6. 跑 pytest + 真 e2e 验证.
"""
from __future__ import annotations

from .audio import AudioResolver
from .bilibili import BilibiliResolver
from .image import ImageResolver
from .model import ModelResolver
from .video import VideoResolver
from .xiaohongshu import XiaohongshuResolver
from .youtube import YouTubeResolver

__all__ = [
    "AudioResolver",
    "BilibiliResolver",
    "ImageResolver",
    "ModelResolver",
    "VideoResolver",
    "XiaohongshuResolver",
    "YouTubeResolver",
]
