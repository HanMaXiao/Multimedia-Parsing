"""image_fetcher — 图片解析下载 → 本地/OSS 子系统。

权威定义:docs/superpowers/specs/2026-09-02-image-fetch-oss-design.md。
镜像 video_fetcher 的包结构,差异:
  - resolver:gallery-dl 优先 + BS4 通用解析兜底(视频是 yt-dlp)
  - downloader:requests 流式(视频是 yt-dlp 内置)
  - run_fetch:parse_urls(解析) + fetch_images(下载/上传)两阶段

被拒(2026-09-02 D1 决策):
  - 只用 gallery-dl — 文章页(头条/知乎/公众号)不在其站点列表,核心场景缺失。
  - 只用 BS4 — 平台相册需要登录态 / 接口签名,自研 = 重写 gallery-dl extractor 生态。
  - Playwright 渲染所有页面再解析 — 慢 + 登录态耦合,JS 渲染兜底延后。
"""

