#!/usr/bin/env python3
"""event_emitter.py — Phase 3 Python 端 ``__EVENT__:`` 行封装工具。

权威定义:
  - docs/SPEC.md §3.5(IPC 协议:stdout 双流通道)
  - .harness/docs/event-protocol.md §2(__EVENT__: JSON schema)
  - .harness/docs/event-protocol.md §7(Python 端 emit 工具)

设计要点:
  - 入口立即 ``sys.stdout.reconfigure(encoding='utf-8')``,Windows 避免 GBK 乱码
    (SPEC §3.5 关键约束)
  - ``ts`` 字段自动填 ISO 8601 UTC,调用方不传(避免时钟漂移)
  - ``status`` 走 9 状态白名单防御校验,错别字 / 拼写错误立即抛 ValueError
    (Python 端"不会出现"是常态,但作为防御边界,emit 入口一次性兜底)
  - ``ensure_ascii=False`` 保留中文(article 名 / error 文案经常含中文)
  - 所有 ``__EVENT__:`` 行必须走 ``emit_platform_status``,**不要**散落 print

Phase 8 起,现有 standalone_markdown_folder_publisher.py 在 5 个位置(SPEC §3.6)
``from event_emitter import emit_platform_status`` 然后调它。
Phase 56(2026-06-29):standalone_markdown_folder_publisher 已下沉到
``publisher.dispatch.run_dispatch``,cancel_event 改用 asyncio.Event 直接透传,
不再走 cancellation.BatchCancellationToken;module-level entry_id 仍被
``publisher.dispatch.run_dispatch`` 用于 emit 注入。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# 入口副作用
# ---------------------------------------------------------------------------

# SPEC §3.5 关键约束:Windows 默认 cp936 / GBK 会把中文 article 名 / error 文案
# 乱码成 '??' 或 UnicodeDecodeError,直接 reconfigure 成 utf-8 一行搞定。
# Python 3.7+ 才支持 stdout.reconfigure,3.6 及以下需要退到 sys.stdout =
# io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')。Playwright 1.40+ 要求
# Python 3.8+,所以 reconfigure 在我们 support matrix 里一直可用。
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    # AttributeError: Python < 3.7
    # ValueError: reconfigure 在 non-TextIOWrapper 上调用时(几乎不会发生)
    pass

# ---------------------------------------------------------------------------
# Phase 30a Spec 5:module-level entry_id 状态
# ---------------------------------------------------------------------------
# 多 Entry 并行场景下,每个 Python 子进程对应 1 个 entry,所有 emit 出来的 event
# 都要带 entry_id 字段(前端 applyEvent 按 entryId 路由到 cellsByEntry[id])。
# Rust 端在 cmd.env("AUTO_PUBLISH_ENTRY_ID", entry_id) 注入;Python main() 读
# 后调 set_current_entry_id 一次。None = spec 3 路径(emit 时 entry_id=null)。
#
# module-level 而非 emit_* 形参:event_emitter 是 1 文件 5 emit 函数,50KB+
# publisher 脚本里 emit 调用点很多(SPEC §3.6 "5 处注入" 之外的几十处 cell 事件);
# 形参传染要改全 50KB,module-level 一次设置全场生效。
_current_entry_id: Optional[str] = os.environ.get("AUTO_PUBLISH_ENTRY_ID")


def set_current_entry_id(entry_id: Optional[str]) -> None:
    """覆盖 module-level entry_id 状态。

    默认从 env var 读(进程级一次性初始化)。main() 解析 --entry-id CLI 后调,
    保证 CLI 覆盖 env(让手动 `--entry-id=foo` 优先级最高)。
    """
    global _current_entry_id
    _current_entry_id = entry_id


def get_current_entry_id() -> Optional[str]:
    """读 module-level entry_id(retry_engine 共用,Phase 56 起不再被 cancellation 引用)。

    唯一 source of truth 在 event_emitter 模块级变量。Phase 30a Spec 5 多 entry 并行时,
    make_retry_event 构造的 payload 也带 entry_id 字段,前端 applyEvent 按 evt.entryId
    路由到 cellsByEntry[id]。Phase 56 起 cancellation.py 已删除,此函数仅剩 retry_engine
    一处 caller。
    """
    return _current_entry_id


# ---------------------------------------------------------------------------
# 9 状态白名单(SPEC §3.3 状态机)
# ---------------------------------------------------------------------------

# 4 非终态 + 5 终态 = 9 个。Spec 1 文章类不发送 processing(保留给 Spec 4 视频类),
# 但白名单必须包含,因为处理函数不区分文章 / 视频,调用方决定要不要发 processing。
VALID_STATUSES: frozenset[str] = frozenset(
    {
        # 非终态
        "pending",
        "logging_in",
        "uploading",
        "processing",
        # 终态
        "success",
        "draft",
        "failed",
        "skipped",
        "cancelled",
    }
)

# Plan 2026-09-02-image-fetch-oss (2026-09-02):图片模块专用 phase 白名单
# 区别于 9 状态机:phase 是"哪一阶段"(parse / download / upload),与 status 配合用。
# 解析阶段回传"图片列表"导致必须新加事件 type 而非复用 platform_status(spec §4.3 F3.1)。
VALID_IMAGE_PHASES: frozenset[str] = frozenset({"parse", "download", "upload"})

# Plan 2026-09-03-universal-resource-fetch (2026-09-03):统一 resource_fetch_status 事件
# 阶段白名单 (跟 image_fetch 阶段名一致 — 都是 parse / download / upload).
# 旧 image_fetch_status 仍走 VALID_IMAGE_PHASES 字段, 不动, 避免破坏既有测试.
VALID_RESOURCE_STAGES: frozenset[str] = frozenset({"parse", "download", "upload"})

# 资源类型白名单 (D2 决策预留 audio / model).
VALID_RESOURCE_TYPES: frozenset[str] = frozenset({"video", "image", "audio", "model"})

VALID_PLATFORMS: frozenset[str] = frozenset(
    {
        # Spec 1 已实现
        "toutiao",
        "csdn",
        "zhihu",
        "juejin",
        # hello_sidecar.py Phase 2 demo 用过 "hello" 这个伪平台,允许
        "hello",
        # Plan 2026-09-02-video-fetch-oss (D4 决策):视频解析下载首期支持 bilibili,
        # 后续扩展 YouTube/X/小红书/快手时按需加。emit 走 platform_status + 9 状态机,
        # platform 字段 = 站点 id(spec §5)。
        "bilibili",
    }
)


# F3.3 (2026-09-02) 加性字段白名单 — emit_platform_status 接受 **extra,只把白名单
# 字段合入 payload(其他 key 静默忽略,避免 payload 漂移 + 调试信号)。
#
# 加新字段:
#   1. 在这里加字段名
#   2. spec §5 表格加一行(权威规范)
#   3. event-protocol.md §2 + §6 同步(权威文档)
#   4. event-emit TS 类型镜像加 optional 字段(前端 introspection)
_EXTRA_FIELDS: frozenset[str] = frozenset(
    {
        # video_fetcher (Plan 2026-09-02) 引入,spec §5 表格"加性扩展":
        "source_url",  # str|None:视频原 URL
        "progress",  # float|None:0-100 进度(下载/上传)
    }
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """ISO 8601 UTC with millisecond precision, e.g. ``2026-06-22T17:00:00.000Z``.

    用 ``datetime.now(timezone.utc).isoformat()`` 然后手动把 ``+00:00`` 换成
    ``Z`` —— Python 3.11 之前 ``isoformat(timespec='milliseconds')`` 才支持毫秒,
    用 ``strftime`` + slice 兼容 3.8 / 3.9 / 3.10。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# 主入口:emit_platform_status
# ---------------------------------------------------------------------------


def emit_platform_status(
    article: str,
    platform: str,
    status: str,
    *,
    url: Optional[str] = None,
    error: Optional[str] = None,
    screenshot: Optional[str] = None,
    **extra: Any,
) -> None:
    """往 stdout 打一行 ``__EVENT__:<JSON>``,Tauri 解析后 emit ``publish-event``。

    Parameters
    ----------
    article:
        内容标识(相对输入文件夹,basename + 扩展名),例 ``article_001.md``。
    platform:
        平台标识。Spec 1 阶段 ``toutiao`` / ``csdn`` / ``zhihu`` / ``juejin``;
        demo 允许 ``hello``;Plan 2026-09-02 video_fetch 加 ``bilibili``(后续扩
        YouTube/X/小红书/快手)。完整列表见 ``VALID_PLATFORMS``。
    status:
        状态机 9 个状态之一(见 ``VALID_STATUSES``)。错别字会抛 ``ValueError``。
    url:
        ``status in {"success", "draft"}`` 时建议带,否则 ``None``。
        SPEC §2 必带是 Tauri 端的"防御式降级"约束,Python 端不强制。
    error:
        ``status == "failed"`` 时建议带人类可读原因,否则 ``None``。
    screenshot:
        ``status == "failed"`` 时建议带截图绝对路径,否则 ``None``。
    **extra:
        加性字段转发(spec §5:F3.3 决策)。当前白名单:

        - ``source_url`` (str|None):视频/文章原 URL,前端可选消费,做"原链接"展示。
        - ``progress`` (float|None):0-100 浮点进度。下载/上传阶段用,前端渲染进度条。

        其他 key 会被静默忽略(防御边界,未来加新字段不用改签名)。只把
        ``is not None`` 的字段合入 payload,避免 ``progress: null`` 之类无意义字段。

    Raises
    ------
    ValueError:
        ``status`` 不在 9 状态白名单里 / ``article`` 空串 / ``platform`` 空串。

    Notes
    -----
    ``ts`` 字段自动填当前 UTC 时间,调用方**不要**传。如果未来需要回溯式事件
    (例:落盘时重放历史),再加个 ``ts: Optional[str] = None`` 参数,内部 ``if not
    ts: ts = now_iso()``。
    """
    # ---- 防御校验(SPEC §5 "Python 端不会出现"作为常态,但 emit 入口兜底) ----
    if not article:
        raise ValueError("emit_platform_status: article must be non-empty")
    if not platform:
        raise ValueError("emit_platform_status: platform must be non-empty")
    if status not in VALID_STATUSES:
        raise ValueError(
            f"emit_platform_status: unknown status {status!r}; "
            f"expected one of {sorted(VALID_STATUSES)}"
        )

    payload: Dict[str, Any] = {
        "type": "platform_status",  # Spec 1 阶段只有这一种 type
        "entry_id": _current_entry_id,  # Phase 30a Spec 5:多 Entry 并行路由 key(spec 3 路径 null)
        "article": article,
        "platform": platform,
        "status": status,
        "url": url,
        "error": error,
        "screenshot": screenshot,
        "ts": now_iso(),
    }

    # F3.3 (2026-09-02):加性字段白名单转发。新字段加 _EXTRA_FIELDS 即可,不改签名。
    for k in _EXTRA_FIELDS:
        if k in extra and extra[k] is not None:
            payload[k] = extra[k]

    # flush=True 是关键 —— Tauri 端 BufReader::lines() 看不到 buffer 里没 flush 的数据
    # (Python 默认 stdout 行缓冲但 pipe 模式下经常 block buffered)
    print("__EVENT__:" + json.dumps(payload, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def emit_image_fetch_status(
    batch_id: str,
    phase: str,
    status: str,
    *,
    source_url: Optional[str] = None,
    image_url: Optional[str] = None,
    images: Optional[List[Dict[str, Any]]] = None,
    progress: Optional[Dict[str, int]] = None,
    url: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """往 stdout 打一行 ``__EVENT__:<JSON>``,Tauri 解析后 emit ``image-fetch-event``。

    Plan 2026-09-02-image-fetch-oss (2026-09-02) 新加的事件 type(spec §4.3):
    解析阶段回传"图片列表"(N 条 URL + 可选尺寸)装不下 platform_status 单条 payload,
    因此新增 type + 配套 emit 函数。Rust 透传不解析(照抄 video_fetch 通道)。

    Parameters
    ----------
    batch_id:
        批次 id(Rust 侧生成,事件原样带回)。
    phase:
        ``"parse"`` / ``"download"`` / ``"upload"`` 三选一。
    status:
        9 状态白名单之一(``success`` / ``failed`` / ``cancelled`` / ``processing`` / ...)。
    source_url:
        当前条目对应的源 URL(解析阶段 = 用户粘贴的页面 URL;下载/上传 = 该图所在源页 URL)。
    image_url:
        下载/上传阶段:当前这张图的 URL(逐张进度事件用)。
    images:
        解析阶段:解析出的图片列表(``[{"url": "...", "width": ..., "height": ...}]``)。
        None 时不发此字段。其它阶段忽略。
    progress:
        下载/上传阶段:``{"done": N, "total": M}``(spec §4.3 规定形状,区别于 video 的
        float 0-100)。None 时不发此字段。
    url:
        上传成功时的 OSS 直链(或本地模式下的 ``file://`` 路径)。
    error:
        失败时的人类可读错误描述。

    Raises
    ------
    ValueError:
        batch_id / phase / status 字段缺失或不在白名单。
    """
    if not batch_id:
        raise ValueError("emit_image_fetch_status: batch_id must be non-empty")
    if phase not in VALID_IMAGE_PHASES:
        raise ValueError(
            f"emit_image_fetch_status: phase must be one of "
            f"{sorted(VALID_IMAGE_PHASES)}, got {phase!r}"
        )
    if status not in VALID_STATUSES:
        raise ValueError(
            f"emit_image_fetch_status: unknown status {status!r}; "
            f"expected one of {sorted(VALID_STATUSES)}"
        )

    payload: Dict[str, Any] = {
        "type": "image_fetch_status",
        "entry_id": _current_entry_id,  # 与 platform_status 一致(多 entry 路由)
        "batch_id": batch_id,
        "phase": phase,
        "status": status,
        "ts": now_iso(),
    }
    # 加性字段:None 不发(payload 漂移防御)
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

    print("__EVENT__:" + json.dumps(payload, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# Plan 2026-09-03-universal-resource-fetch: 统一 resource_fetch_status 事件
# ---------------------------------------------------------------------------


def emit_resource_fetch_status(
    batch_id: str,
    stage: str,
    status: str,
    *,
    resource_type: Optional[str] = None,
    item_id: Optional[str] = None,
    platform: Optional[str] = None,
    source_url: Optional[str] = None,
    image_url: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    progress: Optional[Any] = None,  # float (0-100) 或 {done, total} dict
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    message: Optional[str] = None,
) -> None:
    """往 stdout 打一行 ``__EVENT__:<JSON>``,Tauri 解析后 emit ``resource-fetch-event``.

    权威定义: docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md §5.1.

    Plan 2026-09-03-universal-resource-fetch 统一事件 — 替代 video_fetch_status +
    image_fetch_status 两套 type. payload 字段见 spec §5.1 表格, 加性字段 None 时不发
    (避免 payload 漂移 + 调试信号清晰).

    Parameters
    ----------
    batch_id:
        批次 id (Rust 侧生成).
    stage:
        ``"parse"`` / ``"download"`` / ``"upload"`` 三选一.
    status:
        9 状态白名单之一 (running / success / failed / skipped / cancelled / ...).
    resource_type:
        ``"video"`` / ``"image"`` / ``"audio"`` / ``"model"`` (D2 预留 audio / model).
    item_id:
        平台内资源标识 (video = "{platform}_{video_id}", image = uuid).
    platform:
        平台标识 (bilibili / xiaohongshu / weibo / ...).
    source_url:
        用户原始粘贴 URL (解析阶段回传用).
    image_url:
        下载/上传阶段: 当前这张图的 URL (逐张进度事件用).
    items:
        解析阶段: 解析出的 ResourceItem 列表 (序列化为 [dict, ...]). None 时不发.
    progress:
        下载/上传阶段: 进度. 两种形状 (spec §5.1):
          - float 0-100 (video 模块兼容 — yt-dlp progress hook 是 0-100)
          - {done: N, total: M} dict (image 模块兼容 — gallery-dl 逐张计数)
    result:
        终态产物: ``{"local_path": ..., "oss_url": ..., "oss_error": ...,
                     "thumbnail": ..., "title": ...}``.
    error:
        失败时人类可读错误描述.
    message:
        提示消息 (spec §9: both 模式 OSS 失败降级时显示 "本地已保存, OSS 上传失败").

    Raises
    ------
    ValueError:
        必填字段缺失或不在白名单 (batch_id / stage / status / resource_type).
    """
    if not batch_id:
        raise ValueError("emit_resource_fetch_status: batch_id must be non-empty")
    if stage not in VALID_RESOURCE_STAGES:
        raise ValueError(
            f"emit_resource_fetch_status: stage must be one of "
            f"{sorted(VALID_RESOURCE_STAGES)}, got {stage!r}"
        )
    if status not in VALID_STATUSES:
        raise ValueError(
            f"emit_resource_fetch_status: status must be one of "
            f"{sorted(VALID_STATUSES)}, got {status!r}"
        )
    if resource_type is not None and resource_type not in VALID_RESOURCE_TYPES:
        raise ValueError(
            f"emit_resource_fetch_status: resource_type must be one of "
            f"{sorted(VALID_RESOURCE_TYPES)}, got {resource_type!r}"
        )

    payload: Dict[str, Any] = {
        "type": "resource_fetch_status",  # 统一事件 type — Rust 透传不解析
        "entry_id": _current_entry_id,  # 跟其它事件一致 (多 entry 路由)
        "batch_id": batch_id,
        "stage": stage,
        "status": status,
        "ts": now_iso(),
    }
    # 加性字段: None 不发 (payload 漂移防御)
    if resource_type is not None:
        payload["resource_type"] = resource_type
    if item_id is not None:
        payload["item_id"] = item_id
    if platform is not None:
        payload["platform"] = platform
    if source_url is not None:
        payload["source_url"] = source_url
    if image_url is not None:
        payload["image_url"] = image_url
    if items is not None:
        payload["items"] = items
    if progress is not None:
        payload["progress"] = progress
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    if message is not None:
        payload["message"] = message

    print("__EVENT__:" + json.dumps(payload, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# make_event_cb — Phase 48 Spec 6: 构造 stdout 写入的 callback
# (从原 publisher/__init__.py 合并, 跟 event_emitter 紧密相关)
# ---------------------------------------------------------------------------


def make_event_cb(pipe_path=None):
    """构造 event_cb: 把每个事件 dict 转成 `__EVENT__:` JSON 行写到 stdout.

    Args:
        pipe_path: 当前实现忽略 (保留 API 兼容后续 named pipe 优化).
                   全部走 stdout — 简单且跨平台.

    Returns:
        callable(payload: dict) -> None: 同步 print + flush, 失败时 stderr 报错不抛.
    """
    import sys

    stdout = sys.stdout

    def cb(payload):
        try:
            # Phase 48: entry_id threading — 从 event_emitter module-level 读
            if "entry_id" not in payload:
                payload["entry_id"] = get_current_entry_id()
            line = "__EVENT__:" + json.dumps(payload, ensure_ascii=False)
            stdout.write(line + "\n")
            stdout.flush()
        except Exception as exc:
            print(f"[event_cb] write failed: {exc}", file=sys.stderr)

    return cb


__all__ = [
    "VALID_IMAGE_PHASES",
    "VALID_PLATFORMS",
    "VALID_STATUSES",
    "VALID_RESOURCE_STAGES",
    "VALID_RESOURCE_TYPES",
    "emit_image_fetch_status",
    "emit_platform_status",
    "emit_resource_fetch_status",
    "get_current_entry_id",
    "make_event_cb",
    "now_iso",
    "set_current_entry_id",
]

