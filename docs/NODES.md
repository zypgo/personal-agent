# 自有 Runner：执行、恢复和撤销

Runner 是跑在你自己的 Mac 或 Linux 上的一个小程序，它**主动**去找服务端领任务：只往外发起连接，不接受别人的连接。服务端管身份、空间、模型 API、任务规划、审批和投递；Runner 只收到要执行的命令、输入文件、输出文件名和截止时间。

## 配对和运行

在服务端的管理员终端里，生成一个短时、只能用一次的节点邀请：

```bash
pa node invite --space personal-owner --label laptop
```

在你的电脑上安装同版本的项目，准备好正在运行的 Docker 和工作镜像，然后运行：

```bash
docker pull python:3.12-slim
pa node pair --url https://agent.example.test
pa node run
pa node status
```

配对码通过隐藏输入的提示读入。默认的节点目录是 `~/.personal-agent-node`；`pair`、`run`、`status` 都支持 `--node-home`。

`run` 支持 `--image`、`--memory-mb`、`--poll-s`、`--backend sdk|cli`。从 0.3.1 起，默认用官方的 Docker SDK；要回退，用 `pa node run --backend cli`，SSH Docker context 也必须显式选 CLI。镜像由操作者事先拉取，Runner 不会自动下载；镜像里要有 `/bin/sh`、`date` 和 `/usr/bin/timeout`。生产环境可以指定镜像的 digest，免得同名 tag 悄悄变掉。Docker 用的是本机（或本机 Docker Desktop）的文件系统：如果远端的 Docker context 访问不到你本机的工作目录，这个 backend 就用不了。

SDK 后端不会继承环境里的 HTTP 代理和 netrc。取消时，先中断日志读取；还没结束的同步 SDK 调用仍然占着工作槽位，原来的收据和固定的资源还要继续核对。实现和回退的说明见 [OPEN_SOURCE_ADAPTERS.md](OPEN_SOURCE_ADAPTERS.md)。

电脑睡眠或离线时，还没派发的任务保持等待；服务端不会把状态未知的尝试，自动换到另一个节点上重跑。让 `pa node run` 一直开着，或者用你已有的进程管理工具来启动这条命令。按 Ctrl-C 退出 Runner，不会丢收据；已经启动的容器仍受自己截止时间的约束，重新打开 Runner 时，会检查原来的容器。

### 自托管：局域网或 Tailscale

节点默认要求 HTTPS。如果服务端在你家里的电脑上，可以通过已有的 Tailscale Serve 地址连接，比如 `https://computer.example-tailnet.ts.net`；网页端也要把 `web.public_url` 设成同一个地址。同一台机器上开发时，支持 `http://127.0.0.1:8765` 和 `http://localhost:8765`。跨机器的裸 HTTP 不接受；局域网里可以用你已有的 HTTPS 反向代理。手机浏览器的连接方式，见现有的网页 / Tailscale 文档。

## 为什么响应丢了，也不会重复执行

1. 服务端先把尝试 ID、generation 和计划摘要持久化，再把工作交给节点。
2. 节点先把固定的资源名和输入计划写进本地的 SQLite，再向服务端确认“受理了”。
3. 确认受理之后，才创建名字固定的 Docker 容器；真正开始之前，再记一笔“启动意图”。
4. 重启之后，只检查同一个资源的标签、状态和日志。已经退出的容器不会再 `start`；记了启动意图、却没法证明结果的资源，进入 `uncertain`，哪怕观察到它还是 `created` 状态，也不会再 `start` 一次，更不会新建替代品。取消这类资源，必须确认原来的容器已经删掉，免得迟到的启动请求继续执行。
5. 最终结果先落到本地的 SQLite，然后反复上传，直到服务端返回“已持久接受”的确认。
6. 拿到确认之后，才删除容器和工作目录；清理失败，就保留记录继续重试，成功之后，留一份轻量的去重收据。

默认的任务没有网络，不能直接向外部系统发请求。

## 容器和资源限制

| 项目 | 怎么做的 |
| --- | --- |
| 同时执行 | 每个节点 1 个；没确认清理的、或不确定的尝试，仍然占着名额 |
| 网络 | Docker `--network none` |
| 身份 | 容器里用非 root 的 UID/GID，drop all capabilities，no-new-privileges |
| 文件系统 | 容器根目录只读；只挂载当前 attempt 的工作目录；不挂主目录，也不挂 Docker socket |
| CPU / 内存 | 默认 1 个 CPU、512 MiB；最多 128 个进程；`/tmp` 受限 |
| 时间 | 最长 3600 秒；容器里用 `timeout`，节点掉线或退出之后仍然有效 |
| 输入 / 输出 | 显式的 manifest；最多 64 个文件，每个文件 2 MiB，合计 8 MiB |
| 传输 | JSON 上限 16 MiB；检查相对路径、大小、SHA-256；拒绝符号链接和凭据目录 |
| 日志 | 有界的快照加单调递增的序号；发送前先脱敏；Docker local 日志按 5 MiB × 2 个文件轮转 |
| 磁盘 | 剩余空间低于 128 MiB 时，停止领任务，并请求停止执行；单个文件的进程写入上限 64 MiB |

工作目录是 Docker 的 bind mount；低磁盘水位只是检查，不是文件系统层面的硬配额。要防止大量小文件占满磁盘，请用专用的磁盘目录或操作系统配额。

日志轮转用的是 [Docker local driver](https://docs.docker.com/engine/logging/drivers/local/) 的容器级配置，不会修改宿主机 Docker daemon 的默认设置。

上传的产物只来自计划里列出的路径；绝对路径、`..`、符号链接、校验和对不上、超过上限，都会让收集失败。输出不会自动覆盖服务端工作区里的文件，要在产物流程里查看并应用。

## 撤销和故障处理

```bash
pa node revoke NODE_ID
pa node status
```

撤销之后，服务端立刻不再接受这个 token 的请求；在线的节点下一次请求被拒绝时，会请求停止容器。网络完全断开时，靠本地的截止时间和容器超时来停止。服务器没确认的结果和容器记录会保留，不会标成“已清理”。

怀疑 token 泄露时，立刻 `pa node revoke`，并核对撤销之前这个节点上报的结果。

`uncertain` 的意思是：服务端或节点，没法证明原来那次执行的最终结果。先恢复 Docker 和网络，用原来的节点目录启动 Runner，核对原来的容器和收据；不要删掉 `receipts.db` 再去领新任务。凭据泄露之后，先撤销，手动检查仍在运行的固定容器，再重新给新的节点目录配对。配对不会覆盖已有的身份或收据。Runner 不提供跨机器恢复同一个本地 SQLite 和 bind mount 的自动迁移。

### 从服务端快照恢复

`pa db restore-all` 会撤销快照里的节点授权和还没用的节点邀请，免得较早的快照让后来被撤销的设备重新生效。恢复之前，先停掉 Runner，并核对原来的容器；恢复之后，重新生成邀请，用新的 `--node-home` 配对。旧目录和收据保留下来用于核对，旧的 attempt 不会自动转给新节点。旧 token 被拒绝时，仍然在线的旧 Runner 会请求停止它原来的容器，没确认的结果不会被标成已清理。
