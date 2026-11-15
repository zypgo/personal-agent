# references/

第三方开源项目的**只读参考克隆**，用于在实现不熟悉的能力之前先研究成熟设计（思路、依赖、已知问题）。

- 这些目录**不会**提交到本仓库（见根目录 `.gitignore`），也**不会**被 vendoring 进 `src/`。
- 只有本文件和 `SOURCES.lock`（每个项目的 URL、锁定 commit、日期、许可证文件）被提交，保证可追溯。
- 复现：`scripts/fetch_references.sh`（`--update` 可快进到最新）。
- 原则：借鉴设计，**不直接复制**代码；各项目许可证不同（MIT / Apache-2.0 / 其他），任何借鉴都在 `docs/research/reference-analysis.md` 中说明来源与取舍。

对比结论见 [`docs/research/reference-analysis.md`](../docs/research/reference-analysis.md)。
