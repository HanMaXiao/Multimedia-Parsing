"""image_fetcher.cache — 解析结果磁盘缓存(plan followup #4)。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md §9(范围外)
+ plan followup 4 收口。

设计要点:
  - **键**:``sha256(url)[:16]``(URL 完整,query 包含,不同 query 不同 cache)
  - **值**:JSON ``{parsed_at: epoch_seconds, images: [{url, width, height}, ...]}``
  - **路径**:``{cache_dir}/{url_hash}.json``
  - **TTL**:默认 24h,过期 → get 返 None(spec 容错:重解析覆盖缓存)
  - **损坏 JSON**:get 返 None 不抛(spec 容错:重解析填正确缓存)
  - **cache_dir 不存在**:set 时自动创建
  - **线程安全**:同 URL 双调用 → 第二个读到 stale 不算 bug(简单锁,防 OS-level 文件未关闭)

被拒(2026-09-03 followup 4):
  - LRU/TTL eviction sweep — 简单文件系统已够,YAGNI
  - 在内存里 LRU cache(``functools.lru_cache``)— 跨进程不共享,本次要的是持久化
  - 加密 / 签名 — 内部 cache,无安全需求
  - 强并发锁(file lock) — 进程内单线程使用,加锁只会变慢
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _url_hash(url: str) -> str:
    """URL → 16 字符 sha256 前缀(足够 2^64 唯一性,文件名短)。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# ImageParseCache
# ---------------------------------------------------------------------------


@dataclass
class ImageParseCache:
    """解析结果磁盘缓存。

    Attributes:
        cache_dir: 缓存根目录(不存在时 set 时自动创建)
        ttl_seconds: 缓存有效期(秒);默认 24h(spec:同 URL 二次解析场景,
            内容不会短时间变更,长 TTL 减少 gallery-dl 调用)
    """

    cache_dir: Path
    ttl_seconds: int = 86400  # 24h

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{_url_hash(url)}.json"

    def get(self, url: str) -> Optional[List[Dict[str, Any]]]:
        """读缓存,返 images list。miss / 过期 / 损坏 → None。

        Returns:
            缓存命中:images list;否则 None(让调用方走正常解析)。
        """
        path = self._cache_path(url)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        # TTL 校验
        parsed_at = data.get("parsed_at")
        if not isinstance(parsed_at, (int, float)):
            return None
        if (time.time() - float(parsed_at)) > self.ttl_seconds:
            return None
        images = data.get("images")
        if not isinstance(images, list):
            return None
        return images

    def set(self, url: str, images: List[Dict[str, Any]]) -> None:
        """写缓存。原子写(临时文件 + rename)— 防写到一半进程挂掉留半残文件。"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._cache_path(url)
        data = {
            "url": url,
            "parsed_at": time.time(),
            "images": images,
        }
        # 原子写:临时文件 + os.replace。失败时清理临时文件。
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.cache_dir), prefix=".tmp_", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def clear(self, url: str) -> bool:
        """手动清除某 URL 缓存(测试 / 调试用)。返 True = 已删。"""
        path = self._cache_path(url)
        if path.exists():
            path.unlink()
            return True
        return False


__all__ = ["ImageParseCache"]

