"""resource_fetcher.base — 通用资源 dataclass + resolver 协议.

权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md
  - §4  Resolver 协议
  - §5.2 mode 互斥校验
  - §9  错误处理 + both 模式 OSS 失败降级

设计要点:
  - ResourceType 用 str + Enum: 4 个值 (video / image / audio / model), 本期只实现 video / image,
    audio / model 是 D2 决策预留, 给前端分类 tab 灰显 + 路由枚举用.
  - ResourceItem frozen dataclass + default_factory=dict: 避免可变默认参数陷阱, 每个实例独立 meta.
  - FetchDestination 校验 mode 互斥 (local / oss / both 三选一, D3 决策).
  - FetchResult.is_success: 本地或 OSS 任一有产物即为 success, spec §9 both 模式 OSS 失败
    但本地已成功 → success + oss_error 字段记录, 消息明确.
  - ResourceResolver Protocol + @runtime_checkable: 让 isinstance 检查在运行时工作 (TypeScript
    duck-typing 思路), 方便路由层和前端 type guard 对齐.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

# 复用 video_fetcher 的 OssConfig (F0.5: Phase 1 Commit 3 会上提到 publisher/oss/, 此处先引用
# 现有路径, Commit 3 改 import 即可, dataclass 字段不变)
from multimedia_parsing.video_fetcher.manifest import OssConfig  # noqa: F401  (re-export)


# ---------------------------------------------------------------------------
# 枚举 + 白名单
# ---------------------------------------------------------------------------


class ResourceType(str, Enum):
    """spec §4 资源类型 — 4 个, 本期只实现 video / image.

    继承 str 让 enum 成员直接是 str 实例, 可序列化进 JSON / event payload,
    也可直接跟 spec §5.1 字符串字面量比较.
    """

    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"  # D2 预留, 路由枚举有, resolver 暂无
    MODEL = "model"  # D2 预留, 路由枚举有, resolver 暂无


VALID_MODES: frozenset[str] = frozenset({"local", "oss", "both"})


# ---------------------------------------------------------------------------
# 资源条目 (解析阶段产出)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceItem:
    """统一资源条目 — 解析阶段产出, 前端 / 下载阶段共用.

    字段:
      - item_id: 平台内资源标识. video = "{platform}_{video_id}" (沿用 video_fetcher 约定),
                 image = uuid4 短串 (image URL 无稳定平台 id).
      - resource_type: "video" / "image" / "audio" / "model" (spec §4 终判字段).
      - platform: 平台标识 (bilibili / xiaohongshu / weibo / ...).
      - source_url: 用户原始粘贴 URL (前端 source_url 字段用).
      - title: 资源标题 (前端卡片展示).
      - thumbnail: 缩略图 URL (可选; image 类解析可拿 og:image).
      - meta: resolver-specific 扩展字段 (video: duration / ext / extractor;
              image: width / height). 前端可选消费.

    F0.1 终判: 解析器产出的 resource_type 是权威, 覆盖 router 初判的猜测 — 前端卡片
    类型徽标以此字段为准.
    """

    item_id: str
    resource_type: str
    platform: str
    source_url: str
    title: str = ""
    thumbnail: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 下载目标
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchDestination:
    """下载目标 — mode 互斥 (D3 决策).

    mode 互斥规则:
      - mode='local':  download_dir 必填, oss 必 None
      - mode='oss':    oss 必填, download_dir 必 None
      - mode='both':   download_dir + oss 都必填 (D3 决策, 新增合法 mode)
    """

    mode: str
    download_dir: Optional[Path] = None
    oss: Optional[OssConfig] = None

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(
                f"mode must be one of {sorted(VALID_MODES)}, got {self.mode!r}"
            )
        if self.mode == "local":
            if self.download_dir is None:
                raise ValueError("download_dir is required when mode='local'")
            if self.oss is not None:
                raise ValueError("oss must be null when mode='local'")
        elif self.mode == "oss":
            if self.oss is None:
                raise ValueError("oss is required when mode='oss'")
            if self.download_dir is not None:
                raise ValueError("download_dir must be null when mode='oss'")
        else:  # mode == "both"
            if self.download_dir is None:
                raise ValueError("download_dir is required when mode='both'")
            if self.oss is None:
                raise ValueError("oss is required when mode='both'")


# ---------------------------------------------------------------------------
# 下载结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchResult:
    """单资源下载结果.

    成功条件 (spec §9):
      - mode='local' → local_path 存在
      - mode='oss'   → oss_url 非空
      - mode='both'  → local_path 存在 (oss 失败降级 — oss_error 字段记录)
      - 其他情况     → is_success = False

    字段:
      - item_id / resource_type / platform: 透传自 ResourceItem.
      - local_path: 本地落盘绝对路径 (mode=local/both 有).
      - oss_url: OSS 直链 (mode=oss/both 成功上传后有).
      - oss_error: OSS 上传失败描述 (mode=both 时本地已成功 + OSS 失败的场景).
      - error: 整体失败描述 (解析 / 下载 / 取消 等错误).
    """

    item_id: str
    resource_type: str
    platform: str
    local_path: Optional[Path] = None
    oss_url: Optional[str] = None
    oss_error: Optional[str] = None
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        if self.error is not None:
            return False
        return self.local_path is not None or self.oss_url is not None


# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class ResourceFetchError(RuntimeError):
    """资源 fetch 失败 (resolve / download / upload 任一阶段).

    继承 RuntimeError 让现有 except RuntimeError 也能 catch.
    """


# ---------------------------------------------------------------------------
# Resolver 协议
# ---------------------------------------------------------------------------


@runtime_checkable
class ResourceResolver(Protocol):
    """spec §4 Resolver 协议 — 新资源类型只需实现这两个方法 + router 加一行注册.

    协议方法:
      - parse(url) -> List[ResourceItem]:
          单 URL 解析, 产出 0..N 条 ResourceItem. resource_type 终判字段由 resolver 决定
          (例: yt-dlp 解析 xhs 链接可能产出 video 类型, 也可能产出 image 图集).
      - fetch(item, dest) -> FetchResult:
          单条目下载. dest.mode 决定 local / oss / both. 失败不抛, 返 FetchResult(error=...).

    实现者: resolvers/video.py (VideoResolver), resolvers/image.py (ImageResolver).
    """

    def parse(self, url: str) -> List[ResourceItem]:
        ...

    def fetch(self, item: ResourceItem, dest: FetchDestination) -> FetchResult:
        ...


__all__ = [
    "OssConfig",  # re-export
    "VALID_MODES",
    "ResourceType",
    "ResourceItem",
    "FetchDestination",
    "FetchResult",
    "ResourceFetchError",
    "ResourceResolver",
]

