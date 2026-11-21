# 浏览器适配层

现有的页面代码，仍然放在 `src/personal_agent/channels/web/static/`。这个文件夹放的是 TypeScript 的部分：请求怎么发出、Markdown 怎么转成安全的显示数据，以及任务表单怎么构造。TypeScript 是逐步加进来的。整个界面，并不是用 TypeScript 或 React 写的。

## 构建和检查

你需要 Node 22.12 或更新的版本，以及项目的 Python 开发环境：

```sh
uv sync --frozen --group dev
npm ci
npm run build
npm run check
```

`npm run build` 会检查生成的 API 类型、给适配层做类型检查，然后运行 Vite。它只需要 Node 和已提交的 schema。它写出 `static/query.js`、`static/markdown-parser.js` 和 `static/THIRD_PARTY_NOTICES.txt`；三个都要提交。Python 原样提供编译好的 JavaScript，包括从 wheel 里提供。服务启动的时候，既不需要 Node，也不需要 CDN。

`npm run check` 做得更多。它把正在运行的 Python API 契约，导出到一个临时文件夹，再和已提交的 OpenAPI JSON 比较。它逐字节地检查生成的 TypeScript 和两个 bundle，检查实际打进 bundle 的模块的许可证，并运行全部浏览器 JavaScript 测试。它依次通过 `PYTHON`、`.venv`、`python3` 找 Python。它从不下载依赖，也从不打开 agent 的数据。`npm test` 只对已发布的资源，运行 JavaScript 测试。

改了 API 模型或者应用版本之后：

```sh
uv run --frozen python scripts/export_api_schema.py
npm run generate:api
npm run build
npm run check
```

`frontend/generated/api.d.ts` 由 openapi-typescript 生成。`contracts.ts` 用这些类型，构造真实的任务、步骤和答复的 payload。较早的、详细的任务视图模型，还在 `static/workbench.d.ts` 里。这并不表示每个旧页面，都经过了严格的类型检查。

## 请求和隐私

`RequestScope` 用 TanStack Query Core，合并同时发出的 GET 请求、跟踪请求状态，并取消请求。每个查询都带着 principal 和空间，外加一个会话 epoch。过期时间，和没人用的数据的缓存保留时间，都是零，所以之后的每一次读取，都会回服务器重新授权。什么都不持久化。没有离线重放，没有获得焦点时的重新获取，也没有自动重试。如果调用方传了自己的 abort 信号，它得到的是一个独立的查询，所以取消它，不会取消另一个使用者。

写入和二进制下载，绕过查询缓存和变更缓存。一次写入只发一次，端点支持幂等键的话，就带上调用方自己的幂等键。响应丢了，也永远不能证明写入没有发生。一次写入结束的时候，哪怕是错误响应，更早的读取也会在界面下一次刷新之前被取消。会话或空间变了，下载会和读取一起被撤销。返回任何值之前，会再对照 epoch 检查一次响应体。

401 或 403 会清掉私有的请求，并立刻通知页面。这会清掉屏幕上已有的内容，即使取消在并行的 `Promise.all` 里赢了竞争。工作台随后可以退回到剩下的个人空间。Service Worker 只缓存新的公开资源，仍然不缓存 `/api/` 的响应。浏览器存储里，仍然只存显示主题。

## Markdown

markdown-it 解析 CommonMark 的 token，原始 HTML 是关掉的，它的 HTML 渲染器从不被调用。适配层产出一棵小的数据树，里面只有允许的元素。现有的 DOM 渲染器，只用 DOM 节点和文字来构建页面。它会再次检查 URL，只允许 HTTP、HTTPS 和 mailto 链接，图片只显示成标签或链接，不加载。

限制没变：每篇文档 60,000 个字符，每个行内片段 20,000 个字符，DOM 嵌套六层，表格正文 200 行、20 列。超出限制，或者嵌套太深的内容，退回成纯文本。引用式链接和实体解码，现在用标准解析器。嵌套错乱的链接语法，遵循 CommonMark：可能产生各自独立的自动链接，但绝不会出现嵌套的锚点。

依赖版本固定在 `package.json` 和 `package-lock.json` 里。TypeScript 5.9.3，符合 openapi-typescript 7.13 声明的 TypeScript 5 peer 范围。等生成器支持更新的大版本，再把这两个一起升级。不要把 peer 依赖的冲突压下去不管。
