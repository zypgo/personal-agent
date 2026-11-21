# 0.3.1 发布记录

日期：2026-11-17。基于 V3 主分支 `89ce88cb155a262ba3b30f6c4e632df639d7d42b`。

## 改了什么

- V3 API 用 FastAPI 和严格的 Pydantic DTO，提供经过鉴权的 OpenAPI；读取 body 之前和之后的授权检查，都保留了。
- 浏览器用 TanStack Query Core、markdown-it、TypeScript/Vite；带生成的类型、固定的 bundle、漂移检查和第三方许可证。
- 网页正文用本地的 Trafilatura；基础读取器保留；限制输入大小和并发，避开表格跨行跨列造成的膨胀，禁止依赖库的日志输出正文，并清理缓存。
- Runner 默认用官方的 Docker SDK，CLI 后端保留；修了“启动响应不确定时，可能重复启动”的风险，收据、资源归属和取消核对保持不变。
- restic 加密归档，接在已经校验过的一致快照后面；恢复仍然会撤权并冻结旧的工作，保留策略默认只预览。
- Railway 镜像带着固定 digest 的 restic 和 OpenSSH，运行不需要 Node；没有新增数据库 schema 或云资源。

实现和操作步骤：[OPEN_SOURCE_ADAPTERS.md](OPEN_SOURCE_ADAPTERS.md)。
