# references/

这里放第三方开源项目的**只读参考克隆**。在做不熟悉的功能之前，先到这里看成熟的设计：思路、依赖、已知的问题。

- 这些目录**不会**提交到本仓库（见根目录的 `.gitignore`），也**不会**复制进 `src/`。
- 只提交这个文件和 `SOURCES.lock`（记录每个项目的 URL、锁定的 commit、日期和许可证文件），保证来源可追溯。
- 重新拉取：`scripts/fetch_references.sh`（加 `--update` 可以快进到最新）。
- 原则：借鉴设计，**不直接复制**代码。各项目的许可证不一样（MIT / Apache-2.0 / 其他）；任何借鉴，都在 `docs/research/reference-analysis.md` 里写明来源和取舍。

对比的结论见 [`docs/research/reference-analysis.md`](../docs/research/reference-analysis.md)。
