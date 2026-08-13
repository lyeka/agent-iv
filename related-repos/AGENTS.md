# 参考仓库目录规则

- 以只读方式存放第三方源码，两种形式：
  - Git submodule：首选，保留历史、可跟踪上游。
  - 源码快照：上游远端不可用时，手动复制源码，剔除 `.git` 与本地配置。
- 默认只读分析：不修改 tracked 文件，不在子模块中提交或推送。
- 新增 / 删除参考仓库、更换固定 commit 必须由用户明确要求。
- 学习过程中产生的疑问和探索记录写入 `../draft/`，正式结论或文章写入 `../output/`。
- 引用代码时记录仓库名、相对路径、关键符号和 commit（快照则记录来源 commit）。

## 现有参考仓库

### claude-code/（源码快照）

- 源码仅本地保留，未纳入版本控制。
- 快照 commit：`09f43552c76cb8856c4a5414f9aa9c9cda6ee035`
- 快照日期：2026-08-13
- 内容：`src/`（1902 文件）+ `README.md`，已剔除 `.git` / `.claude`

### pi/（Git submodule）

- 来源：`git@github.com:earendil-works/pi.git`（pi.dev）
- 固定 commit：`46bb9a2c3bdb296b0d2179f7309ec6b79a7f3106`
- 内容：TypeScript monorepo，`packages/`（agent / ai / client / coding-agent / evals / protocol / server / session-backends / telemetry / tui）+ `scripts/`，1371 tracked 文件

[PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
