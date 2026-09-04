# Multimedia Parsing — Progress Log

项目根: `D:\XiaoProject\Multimedia Parsing`
权威规范: `docs/superpowers/specs/2026-09-03-universal-resource-fetch-design.md`
脚手架起点: 2026-09-03 Phase 1-6 收口 (从 Multi-Platform Auto-Publish 抽出)

## Session 2026-09-04 — xhs 智能 dispatch 修复

### 问题
- 用户报: "xhs #1 (6a727c8d) 是图片,不是 video" — 实际是 9 张图的小红书图集
- 实际 e2e 表现: 走 `VideoResolver` → yt-dlp xiaohongshu extractor 按 video formats 探测 → 图集无 video formats → 抛 `DownloadError: No video formats found!` → `VideoResolver.parse` 吞异常返 `[]` → cell skipped
- 根因: router 阶段把 xhs 注册到 `VideoResolver` (假设 "视频平台"),但 xhs explore URL 实际可能是 video **或** imageList (F0.1 二级判定缺失)

### 决策 — A 方案 (MixedXhsResolver 智能 dispatch)
- 用户原文: "A,pipeline 的确有 bug,即使 yt-dlp extractor 内部能拿到 imageList,但图片解析下载也不应该由 yt-dlp 负责"
- 实现路径: 调 yt-dlp 拿 `note_info` (绕过 format check) → 判 `note.video.media.stream` (video) 还是 `note.imageList` (image) → dispatch 到 `VideoResolver` 或 `ImageResolver`
- 优先级: mixed entry (video + image) 优先 video
- 兜底: note_info 拿不到 → `ImageResolver` (gallery-dl 也能直接处理 xhs URL)
- 错误模式: 跨类型 fetch (audio / model D2 预留) 返 error, 跟 `VideoResolver.fetch` 同模式

### 改动清单
1. **新文件** `multimedia_parsing/resource_fetcher/resolvers/xiaohongshu.py` (7083 bytes)
   - `XiaohongshuResolver` 类,实现 `parse()` + `fetch()` 双重 dispatch
   - `_fetch_xhs_note_info()`: 内部调 yt-dlp `_search_json` + extractor instance 拿 `note_info`,跳过 process/format check
   - F0.1 二级判定: `note.video.media.stream` 存在 → video, `note.imageList` 非空 → image
2. **改** `multimedia_parsing/resource_fetcher/router.py:66`
   - `ResolverRule("mixed", ("xiaohongshu.com", "xhslink.com"), XiaohongshuResolver)` (替换原 `VideoResolver` 注册)
3. **改** `multimedia_parsing/resource_fetcher/resolvers/__init__.py:22,27`
   - 导出 `XiaohongshuResolver`
4. **新文件** `tests/test_xiaohongshu_resolver.py` (9 tests)
   - image-only note → image resolver
   - video-only note → video resolver
   - mixed video+image → 优先 video
   - note_info 不可用 → image resolver 兜底
   - empty note → image resolver 兜底
   - fetch video item → video resolver dispatch
   - fetch image item → image resolver dispatch
   - fetch unsupported resource_type → error
   - router 路由 xhs → XiaohongshuResolver
5. **改** `tests/test_resource_fetcher_router.py` 反映新 dispatch 行为
   - VIDEO_URLS 移除 2 个 xhs URL (现在不再走 video)
   - `test_resolve_xiaohongshu_picks_first_in_registry` → 改名 `test_resolve_xiaohongshu_uses_mixed_resolver`,验证 xhs 走 `XiaohongshuResolver`
   - `r.resource_type in {"video", "image", "audio", "model"}` → 加上 `"mixed"`
   - `test_resolver_rules_video_covers_common_platforms` 移除 `xiaohongshu.com` (现在不在 video rule)
6. **改** `tests/test_event_emitter_resource_status.py:101` (顺带 fix pre-existing xdist bug)
   - `list(VALID_STATUSES)` → `sorted(VALID_STATUSES)` (frozenset iteration order 在 xdist worker 间不一致,导致 `Different tests were collected between gw0 and gw1` 错误)

### 验证
- `pytest -m "not slow"` 单线程: **470 passed** (5.6s)
- `pytest -n 4 --dist=loadscope -m "not slow"`: **470 passed** (10.8s) — 修 xdist fix 后 ✅
- `pytest -n auto --dist=loadscope -m "not slow"`: **470 passed** (14s) ✅
- `python scripts/smoke_e2e.py --base-url http://127.0.0.1:8001`:
  - `/health` ok
  - bilibili parse → skipped (沙箱无 cookies 预期)
  - `/cancel` ok
- **真 e2e** `POST /parse` xhs #1 (6a727c8d) + `GET /events/{batch_id}`:
  - batch done, 1/1 success
  - `resource_type=image` ✅ (之前是 skipped)
  - 2 items with 真 xhs 图床 URL:
    - `https://sns-webpic-qc.xhscdn.com/202609041629/.../...nd_dft_wlteh_jpg_3` (1242×1656)
    - `//picasso-static.xiaohongshu.com/fe-platform/e6214e4fbfae2cf14d634d4296916e8a5eaefdf4.png`
  - 沙箱内只拿 2/9 张 (xhs 反爬限制,需 cookies 拿完整 imageList) — dispatch 逻辑正确, 实际 9 张需要带 cookies 的真 e2e

### 后续
- 用户真机 + xhs cookies 实测可拿 9 张图 (不在本 session 范围,需要用户配 cookies.txt)
- cookie 注入机制已就位 (`run_fetch` 透传 `cookie_file` 到 resolver + downloader),无需代码改动

---

## Session 2026-09-04 (续) — Resolver 扩展样板 + 扩展 Skill

### 需求
- 用户报: "然后还有 BilibiliResolver, YouTubeResolver 等, 主要是方便同事扩展其它平台的图片,
  视频, 音频等资源的下载, 然后还需要准备一个该项目的 Resolver 扩展技能, 方便同事拿到
  skill 快速扩展其它平台"

### 决策 — 4 种模板 + 1 个 skill
- **重在扩展模板, 不在功能堆砌** — 用户原话"方便同事扩展", 关键交付是**骨架 + skill 文档**
- 4 种模板覆盖 90% 扩展场景:
  1. **Template 0 (One-liner)**: 平台能用 yt-dlp / gallery-dl → 改 `router.py` 一行注册即可
  2. **Template 1 (Thin extension)**: 平台能用 yt-dlp + 想加少量 meta → 继承 `VideoResolver` / `ImageResolver`
  3. **Template 2 (Platform dispatcher)**: 跨类型 URL (e.g. xhs 视频/图集) → F0.1 终判
  4. **Template 3 (Real resource-type)**: 新资源类型 (audio) → 自己实现 parse + fetch
  5. **Template 4 (Stub)**: 资源类型预留 (model) → 接口就位, parse 返 `[]`, fetch 返 not_implemented

### 改动清单
1. **新文件** `multimedia_parsing/resource_fetcher/resolvers/bilibili.py` (2.6 KB)
   - `BilibiliResolver(VideoResolver)` — 加 `is_bilibili` meta, cookie_file 透传父类
2. **新文件** `multimedia_parsing/resource_fetcher/resolvers/youtube.py` (2.2 KB)
   - `YouTubeResolver(VideoResolver)` — 加 `is_youtube` meta, 识别 `platform='yt'` (yt-dlp 短链)
3. **新文件** `multimedia_parsing/resource_fetcher/resolvers/audio.py` (9.0 KB)
   - `AudioResolver` — 真实现, parse 走 `_resolve_audio_meta` (复用 video_fetcher),
     fetch 自己调 yt-dlp `format='bestaudio/best'` + `FFmpegExtractAudio` postprocessor
4. **新文件** `multimedia_parsing/resource_fetcher/resolvers/model.py` (3.8 KB)
   - `ModelResolver` — D2 决策 stub, parse 返 `[]`, fetch 返错误提示同事用 skill 扩展
5. **改** `multimedia_parsing/resource_fetcher/resolvers/__init__.py` (alphabetical 重写)
6. **改** `multimedia_parsing/resource_fetcher/router.py`:
   - `bilibili.com` / `b23.tv` 注册到 `BilibiliResolver` (替换原 `VideoResolver`)
   - `youtube.com` / `youtu.be` 注册到 `YouTubeResolver` (替换原 `VideoResolver`)
   - 新增 `soundcloud.com` / `snd.sc` → `AudioResolver`
7. **新文件** `C:\Users\xiaoge\.minimax\skills\resolver-extension\SKILL.md` (13.9 KB)
   - 全局 skill, 跨项目可用. 包含:
     - frontmatter description 列出 9 个 trigger phrases
     - 4 种模板决策树 (按"用户需求"映射到"模板")
     - 8 步完整工作流 (验证工具 → 选模板 → 写文件 → 注册 → 写测试 → 验证)
     - 7 个关键约束 (parse/fetch 不抛 / cookie_file 透传 / item_id 格式 / etc.)
     - 8 个常见陷阱 (含 xdist frozenset pitfall fix)
     - 14 个参考文件路径
8. **改** `AGENTS.md`:
   - layout 段加 5 个新 resolver (bilibili / youtube / xiaohongshu / audio / model)
   - 新增 "扩展新平台 / 新资源类型" 章节 (7 个 resolver 总表 + 5 个同事典型场景)
   - References 段加 skill 路径

### 新增测试 (16 个, 总 470 → 486)
- `tests/test_bilibili_resolver.py` (3 tests): subclass / parse 加 meta / cookie_file 透传
- `tests/test_youtube_resolver.py` (3 tests): subclass / parse 加 meta / 识别 platform='yt'
- `tests/test_audio_resolver.py` (4 tests): parse 成功 / parse 失败 → [] / fetch_wrong_type / missing_audio_id
- `tests/test_model_resolver.py` (3 tests): parse 返 [] / fetch not_implemented / fetch_wrong_type
- `tests/test_resource_fetcher_router.py` (+1): VIDEO_URLS 拆 BILIBILI/YOUTUBE/GENERIC, 加 AUDIO_URLS matrix

### 验证
- 单跑新 tests: **13/13 pass** (1 个 Windows path separator 微调后过)
- 单线程全量 `pytest -m "not slow"`: **486 passed** (6.7s) — 0 回归
- xdist `-n auto --dist=loadscope`: **486 passed** (13s) — 0 errors
- 已有 xdist fix (上一 session 修了 `list(VALID_STATUSES) → sorted`) 持续生效

### 后续
- 同事用 `skill resolver-extension` 加载 skill → 按 8 步工作流加新平台
- 真 e2e 验证 audio / model 待同事加真实 URL 时跑 (本 session 不在范围)
- cookie 注入已就位 (soundcloud / huggingface 等需要登录态的, run_fetch 透传 `cookie_file`)
