# 0.3.1：换用成熟的开源组件

这一版在 V3 主分支上，换用了一批成熟的开源组件，来处理 API 契约、浏览器请求和 Markdown、网页正文提取、自有的 Docker Runner，以及加密备份。服务端、执行节点和持久状态各管什么，仍然沿用 [V3](V3.md)：一个现成的 Railway 服务加一块卷、你自己的电脑、个人或少数可信的协作者。这一版没有增加数据库 schema，也不需要你再买云沙箱或托管中间件。

## 换了哪些

| 层 | 用了什么（锁定的版本） | 本项目仍然自己负责的部分 |
| --- | --- | --- |
| V3 HTTP API | FastAPI 0.163.1、Pydantic 2.14.3 | 身份、空间授权、读完请求体后的再次鉴权、Guard、错误格式、no-store |
| 浏览器请求 | TanStack Query Core 5.108.0 | 会话和空间隔离、撤权清屏、写请求不重试、产物不缓存 |
| Markdown | markdown-it 15.0.3 | token → DOM 渲染、URL 白名单、禁用原始 HTML 和外部图片、复杂度限制 |
| 前端构建 | Vite 8.3.8、TypeScript 5.9.3、openapi-typescript 7.13.0 | 可重现的静态产物、契约漂移检查、现有的 PWA 和 CSP |
| 本地正文提取 | Trafilatura 2.3.1 | 受控下载、SSRF、长度和并发限制、安全格式化、代码页和故障回退、日志与缓存的隐私 |
| 自有执行节点 | Docker SDK for Python 7.2.0 | 固定的资源 ID、收据、计划摘要、截止时间、未知状态、停止和清理 |
| 加密归档 | restic 0.19.1 | 一致的快照、文件清单校验、恢复后撤销授权、冻结旧任务 |

版本来自这次的锁文件和官方稳定发行版。TypeScript 用的是和当前类型生成器兼容的 5.9.3，没有靠强行忽略 peer dependency 冲突。Python 依赖由 `uv.lock` 锁定，前端由 `package-lock.json` 锁定；升级依赖时，必须重新生成并验证契约和静态产物。

### 仍然自己维护的部分

OpenAI Agents SDK 仍然放在 `harness/` 端口后面。Inbox、Timers、Memory、Permissions、Outbox，以及 SQLite 里的任务尝试和授权版本，继续由应用自己维护。新组件只接手通用的解析、传输、构建和存储工作：它们不自己决定审批，不改任务计划，也不会在网络出错之后重复执行有副作用的操作。

原来的聊天接口、SSE 和渠道协议，继续用现有的 Starlette 路由；这次 FastAPI 只覆盖 V3 的协作和节点 API。整个产品没有被改写成另一种 Agent 框架。

## API 契约

`channels/web/contracts.py` 定义请求 DTO、响应文档和路由清单；`openapi.py` 从同一份清单构建路由和 OpenAPI。所有 V3 的 JSON 写接口，都实际使用严格的 DTO 校验，拒绝把字符串悄悄转换成布尔值或整数。

调用顺序没变：

1. 根据现有的会话、空间角色或节点凭据授权。
2. 有上限地读取 JSON 请求体。
3. 再检查一次会话和成员资格，免得读取期间被撤权。
4. 严格校验 DTO，再执行业务操作。

为了保持这个顺序，接口显式调用 DTO 校验，不用会提前读取 body 的自动参数注入。未知的附加字段仍然被忽略；客户端没法靠伪造 actor、approver 或授权版本，来获得权限。已有的错误结构、状态码、HEAD、修订别名和节点协议都保留。

登录之后，可以读取 `GET /api/v3/openapi.json`；这个接口返回 `private, no-store`，不开放 Swagger / ReDoc。离线导出不会创建应用、不打开数据库，也不读取实例凭据：

```bash
uv run --frozen python scripts/export_api_schema.py
```

契约里有 36 个操作、32 条路径。响应仍然是原来的 `JSONResponse`；模型只用来生成文档和类型。带扩展字段的任务和节点记录，没有被硬裁成封闭的结构。

## 浏览器与 Markdown

`frontend/query.ts` 把 GET 的并发合并、状态和取消，交给 Query Core。查询按身份、空间和会话代次分开；已经完成的数据马上算过期，后面再读，还是会请求服务端。退出、切换空间，或者遇到 401/403 时，会取消旧请求并清除私有数据；权限丢失的通知会同步触发清屏，免得并发请求里先返回的“取消”错误，盖住撤权。

写请求直接发一次，不进入 Query 的持久化或离线重放机制；响应丢了，就显示失败或未知，不能据此自动重发。文件下载不进查询缓存。浏览器存储和 Service Worker 仍然只保留允许的公开外壳资源。

`frontend/markdown-parser.ts` 用 markdown-it 生成 token，原有的 DOM 层只通过安全的节点 API 来构造页面。原始 HTML 不执行；链接会校验协议；外部图片不会自动加载；输入长度、嵌套层数和表格规模仍然有上限。CommonMark 和旧解析器，在个别嵌套语法上有差异，现在按标准语义显示，并用原有的注入和资源限制用例来验证。

新增的请求、解析、表单边界，用严格的 TypeScript；现有的 DOM 页面继续用 JavaScript。生产包带着 `static/query.js`、`static/markdown-parser.js` 两个独立的 bundle，**运行 Python 服务，不需要 Node、npm 或 CDN**。

维护前端时，用 Node 22.12 或更高版本：

```bash
npm ci
npm run build
npm run check
```

`check` 会比较运行时的 OpenAPI、提交的 schema、生成的类型和实际的 bundle，并运行 JS 回归测试。改了契约之后，要重新导出 schema 和生成类型；生成的产物必须和源码一起提交。第三方许可证随静态产物一起保留。

## 正文提取

配置项：

```toml
[tools]
html_extractor = "auto" # auto | basic | trafilatura
```

`auto` 对小片段，还是用原来的格式化器；对文章页，会尝试 Trafilatura。它只解析已经通过了现有 SSRF、DNS、重定向和下载大小检查的 HTML；不调用这个库自带的下载器，也不会把源 URL 交给它。解析放在线程里，免得阻塞事件循环；同时最多跑两个文章解析任务，容量不够就直接回退。

遇到下面这些情况，就改用原来的基础读取器：超过 512,000 个字符、超过 5,000 个标签起始符、带 `<pre>` 的代码页、带 rowspan/colspan 的表格，或者提取结果太短。表格的跨行跨列，可能把很小的源文件展开成巨大的内部网格，所以必须在进入文章解析器之前就回退。文章结果仍然会经过原来的安全 HTML 格式化器，保留代码缩进和链接策略。

关掉了跨文档去重；每次解析结束，都会调用上游提供的缓存清理接口，清掉可能留着正文的缓存。上游的日志不会传到应用日志里，应用只记录通用的失败原因，不记录网页正文或源地址。`basic` 可以随时切回旧的读取器。

## 自有 Docker 节点

```bash
pa node run                  # 默认 SDK 后端
pa node run --backend cli    # 显式兼容后端
```

镜像仍然由操作者事先拉取，两个后端都不会自动下载。SDK 只换了操作 Docker 的方式，`Backend` 协议和原来的收据库都保留：资源名、标签、计划摘要和启动意图，先持久化；断线和重启之后，检查的还是同一个容器。已经记下启动意图之后，如果没法证明它没执行，就进入 `uncertain`，不会再次 `start`。

同步的 SDK 调用使用有上限的工作槽位。取消 async 的等待，并不表示底层调用已经停了；要等真实的线程结束，才释放槽位。日志流单独关闭，用来打断阻塞的读取，不会因此把容器误标成已经停止。容器隔离、无网络、非 root、资源限额和本地截止时间，和 V3 一样。

SDK 后端不继承环境里的代理凭据，也不会自动把 Docker 客户端的代理配置注入工作容器。SSH Docker context 要用显式的 CLI 后端；任何远端的 daemon，都必须能访问同一个 bind mount 路径。这里只支持你自己的、可信的 Docker 主机，不提供第三方云沙箱。完整的节点操作见 [NODES.md](NODES.md)。

## 加密备份与恢复

restic 位于一致快照的后面，不能直接对运行中的 agent 主目录，执行本项目的归档命令。自托管的主机，先停掉服务端，再按 [V3](V3.md) 创建整个实例的快照。数据库、空间文件、工作区和产物，仍由原来的快照机制统一校验。Railway 上，PID 1 一停，容器就会退出，所以这个版本没有在线的整实例快照；正常部署，优先用已有的卷备份，restic 处理的是已经安全取得的离线副本，或已经生成好的快照，见[部署限制](RAILWAY.md#4-备份升级与回滚)。

在本机安装[官方 restic](https://restic.readthedocs.io/en/stable/020_installation.html)。Railway 镜像已经内置了固定版本的官方 binary 和 OpenSSH 客户端。下面是自托管或离线副本的操作示例，需要先建好可写的备份目录和恢复父目录；这些路径不是镜像自动挂载的位置。密码文件放在快照和仓库之外，权限设成 `0600`，不要把密码写进命令行：

```bash
export RESTIC_REPOSITORY=/trusted-backups/encrypted
export RESTIC_PASSWORD_FILE=/trusted-secrets/restic-password

pa db backup-all /trusted-backups/snapshot-0.3.1
pa db restic --namespace personal init
pa db restic --namespace personal backup /trusted-backups/snapshot-0.3.1
pa db restic --namespace personal snapshots
pa db restic --namespace personal check
pa db restic --namespace personal restore FULL_64_CHARACTER_SNAPSHOT_ID --to /trusted-restores/home
```

`init` 对新仓库只运行一次。只支持本地路径，或者 `sftp://user@host/path`，用的是你已有的磁盘或自己的 SFTP 主机；没有去申请对象存储。SFTP 依赖操作者已有的 SSH 密钥或 agent，以及已经验证过的 host key，不会关掉主机校验。镜像里服务账号的 home 是 `/data`，SSH 文件需要让这个账号能读，并限制好权限。Railway 的管理命令，通过 `python -m personal_agent.deploy admin db restic ...` 执行。

上传只包含 manifest 里列出的文件，上传前后都会校验快照；上传期间，不能修改离线快照。归档超时之后，先查 `snapshots`，不要以为服务端没有保存。备份命名空间用来区分实例和保留策略，代替不了独立的仓库密码或访问控制。

恢复时，先下载到临时目录，验证归档和 manifest，再调用现有的 `restore-all` 逻辑，创建新的主目录。旧的浏览器会话、邀请、节点和渠道授权会失效；旧任务和计时器保持冻结，重新配对并核对之后才能用。不能绕过这一步，直接启动 restic 下载下来的目录。

保留策略默认只预览，要显式加 `--apply`，才会删除当前命名空间里旧的快照引用：

```bash
pa db restic --namespace personal forget --keep-last 7
pa db restic --namespace personal forget --keep-last 7 --apply
```

不会自动执行 `prune`、解锁、定时上传，也不会删除其他命名空间。restic 的密码丢了就恢复不了。只备份在同一块 Railway 卷里，能帮你回滚，但抵御不了整块卷丢失；需要这种保障，就用已有的另一块磁盘，或者你自己的 SFTP 位置。

## 升级、回退与验证

1. 停止接收新任务，把正在执行的任务做完，或者明确取消；不确定的任务，保留它的收据和容器，留着核对。
2. 创建整个实例的一致快照。
3. 更新源码，执行 `uv sync --frozen`。现有的 Railway 用仓库里的 Dockerfile 构建；保持一个副本和同一块 `/data` 卷。
4. 启动服务端，检查 `/healthz`、网页登录、空间和模型配置；自有节点更新之后，用原来的节点目录启动。
5. 用真实的手机和已有的渠道，确认登录、审批、产物下载都正常。新增的组件，不会替你配置模型密钥、渠道凭据或 Docker daemon。

正文提取可以切回 `basic`，Runner 可以切回 `--backend cli`。API 和前端回退时，要把版本和锁文件成套回退；不要把旧的 bundle 和新的契约混着用。原来的 `backup-all` / `restore-all` 仍然可以单独使用。恢复历史快照，必须遵守撤权和任务核对的流程。

开发质量门：

```bash
npm ci
scripts/check.sh
scripts/check.sh --python 3.11
# 可选真实本地加密归档测试，无外部服务和费用：
PA_TEST_RESTIC_BINARY=/absolute/path/to/restic uv run --frozen pytest tests/test_v3_restic.py -k real_restic
```

内存有限的开发机，可以设置 `PA_TEST_WORKERS=2`，只是减少测试的并行数，不会跳过用例；不同 Python 版本的全量检查，要一个接一个地跑。

离线测试证明不了真实的模型额度、Railway 已经部署好，或者 Docker 主机已经配好。发布时，分别记录测试结果、实际的启动状态和外部环境的检查；没有观察到的部署，不要标成已完成。

## 上游来源

- [FastAPI：自定义 OpenAPI](https://fastapi.tiangolo.com/advanced/extending-openapi/)、[Pydantic：严格模式](https://docs.pydantic.dev/latest/concepts/strict_mode/)
- [TanStack Query Core](https://github.com/TanStack/query)、[markdown-it](https://github.com/markdown-it/markdown-it)、[Vite](https://vite.dev/guide/)
- [Trafilatura 用法](https://trafilatura.readthedocs.io/en/latest/usage-python.html)、[官方缓存清理实现](https://github.com/adbar/trafilatura/blob/v2.3.1/trafilatura/meta.py)
- [Docker SDK](https://docker-py.readthedocs.io/en/stable/)、[容器操作](https://docker-py.readthedocs.io/en/stable/containers.html)
- [restic 0.19.1 发行](https://github.com/restic/restic/releases/tag/v0.19.1)、[restic 恢复](https://restic.readthedocs.io/en/stable/050_restore.html)

来源核对日期：2026-11-17。Docker 里的 restic，用的是官方多平台镜像的固定 digest，启动时不下载，也不自动更新。
