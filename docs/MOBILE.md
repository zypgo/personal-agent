# 在手机上使用栖序

用手机浏览器就能聊天、发图片和语音、看审批和最近的任务。0.2.0 起，回复生成时能先看到正文预览，生成的文件也能下载。agent 跑在电脑上，手机不需要模型 API key。

你也可以用[钉钉私聊](DINGTALK.md)：由电脑主动连钉钉，不需要公网回调地址。

## 选一种连接方式

| | 局域网 | Tailscale |
|---|---|---|
| 适合 | 手机和电脑连着同一个可信的 Wi-Fi | 在家、出门、用移动网络都行 |
| 地址 | `http://192.168.x.x:8765` | `https://电脑名.tailnet.ts.net` |
| 加密 | HTTP，只能在可信的局域网里用 | Tailscale 隧道加 HTTPS |
| 手机上要装什么 | 什么都不用，用 Safari 或 Chrome | 装 Tailscale，并登录有权访问这台电脑的 tailnet |
| 语音 | 上传已有的录音文件 | 上传录音，或直接用网页麦克风 |
| 配对 | 电脑生成的一次性链接或二维码 | 同样要配对 |

## 方式一：同一个 Wi-Fi

在电脑上，进入项目目录运行：

```bash
uv run pa web connect --via lan
```

如果电脑只有一个私网 IPv4 地址，会自动选它。有多个网卡时，会列出候选地址，你用 `--host` 指定 Wi-Fi 的那个：

```bash
uv run pa web connect --via lan --host 192.168.1.23
```

这条命令会：打开 `[web] enabled`；只监听你指定的那个私网地址；保留其余配置和注释；打印手机配对用的二维码。端口默认沿用配置里的，想换就加 `--port 9000`。

然后重启 agent：

- 如果你在前台运行 `pa serve`，重启它；
- 如果已经装了后台服务，运行：

```bash
uv run pa service restart
```

- 如果是第一次用、服务还没启动：

```bash
uv run pa serve
```

用手机扫二维码，或者打开打印出来的完整链接。配对链接默认 10 分钟有效，只能用一次；过期了就重新运行 `uv run pa web pair --label phone`。

局域网用的是 HTTP，浏览器的麦克风和部分 PWA 功能用不了。文字、图片、上传录音文件、审批、看任务状态都不受影响。想直接录音，或者想加密访问，请选 Tailscale。

## 方式二：Tailscale HTTPS

1. 在电脑和手机上都安装并登录 Tailscale，确认两台设备的访问策略允许互相连接。
2. 在电脑上运行：

```bash
uv run pa web connect --via tailscale
```

这条命令会依次：检查 Tailscale 的登录状态和 HTTPS 名称；核对已有的 Serve 配置；设置一个指向本机 agent 的私有 HTTPS 代理；把 HTTPS 地址写进配置；打印一次性二维码。macOS 的 Tailscale App 和 PATH 里的 `tailscale` 命令行，两种装法都支持。

如果你的 tailnet 还没开 MagicDNS / HTTPS / Serve，先照 Tailscale 的提示开好，再重试。如果 443 端口已被占用，换一个端口：

```bash
uv run pa web connect --via tailscale --https-port 8443
```

重启 agent 之后，手机保持 Tailscale 在连接状态，扫二维码就行。网页麦克风还需要浏览器授权。

Safari 里用“分享 → 添加到主屏幕”。主屏幕 App 的 cookie 可能和 Safari 是分开的，要在新窗口里重新配对一次。Android Chrome 用浏览器自带的安装或添加到主屏幕入口。

上面的命令设置的是 [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)，只有 tailnet 里允许的设备能访问。

## 连不上怎么办

```bash
uv run pa web check
uv run pa web
uv run pa doctor
```

`check` 会分别检查本机监听和配置里的手机地址，不会改任何设置。

- **局域网打不开**：确认两台设备在同一个网络；访客 Wi-Fi 常会把设备互相隔离；检查电脑的防火墙有没有放行这个端口；换了 Wi-Fi 或 IP 之后，重新运行 `connect --via lan --host ...`。
- **Tailscale 打不开**：检查两端的连接状态、tailnet 的访问规则、MagicDNS / HTTPS，以及 `tailscale serve status`。
- **配置保存了，还是打不开**：重启 agent。正在运行的进程不会自己重新加载监听地址。
- **提示配对失效**：在电脑上重新生成链接。
- **合上电脑盖子后连不上**：服务跑在电脑上，电脑睡眠或关机时它没法响应。要保持电脑唤醒，或者把它部署到一台一直开着的设备上。

## 看任务，管设备

网页里的“设置 → Recent tasks”会显示：排队中、执行中、等待审批、因预算暂停、下次重试时间、最终停止。点 Refresh status 会从保存的状态重新读一遍。

语音消息先把收到的记录存下来，再调用转写服务。同一条消息重复投递时，直接用已经完成的转写结果，不会再调一次。转写在拿到结果之前被打断时，会提示你重新发一条（再调一次要花钱，所以不自动重试）。

```bash
uv run pa web sessions          # 查看已配对的浏览器
uv run pa web revoke 3          # 撤销指定的浏览器
uv run pa web revoke --all      # 全部退出
uv run pa web unpair            # 撤销还没用过的配对链接
```

想关掉这条命令配置的 Tailscale 443 入口，运行 `tailscale serve --bg --https=443 off`；用了其他端口就替换成那个端口。

手机页面断线后会自动用 SSE 重连。关掉网页之后，不会有系统级的 Web Push。想在后台收到主动提醒，请启用 Telegram 或钉钉渠道。

## 流式回复和下载

默认 `[web] stream_responses = true`：在网页上发起的对话，回复生成时就能看到正文。推理过程、工具参数、后台任务的正文不显示。手机断线不会取消任务，重连后会重新读取当前的预览和已经提交的历史。预览最多显示 32,000 个字符，完成后显示完整的回复。

如果你用的兼容端点不支持流式，在 `[web]` 里设置 `stream_responses = false`，再重启。

让 agent 用 `publish_artifact` 发布工作区里的文件后，历史里会出现一张下载卡片。下载用的是当前配对的 cookie，局域网和 Tailscale 都能用。有效期、额度、Telegram 上传和备份要求，见 [ARTIFACTS.md](ARTIFACTS.md)。过期的文件要重新生成；撤销了网页会话之后要重新配对。
