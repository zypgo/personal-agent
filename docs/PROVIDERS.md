# 模型、语音和搜索服务

agent 跑在你自己的电脑上。有三样东西会发到你选的服务：模型、语音转文字，和托管的联网搜索。这份指南讲怎么配这三样，以及它们还共用哪些设置。

## 已经有能用的配置怎么办

除非你加了新的设置，否则什么都不会变：

- 文本模型的地址，来自 `[model] base_url`；没设的话，用 `OPENAI_BASE_URL`；再没有，就用 OpenAI 的默认地址。
- 密钥来自 `OPENAI_API_KEY`。
- 语音转文字，用和文本模型相同的地址和密钥。它的模型名，仍然来自 `[media] transcribe_model`。
- `model.main`、`model.fast` 和 `context.model`，只是在同一个地址上选模型名，不会换成别的服务商。

有两项检查，比以前更严：

- 如果你自己写了 OpenAI 官方的地址，现在必须有密钥。
- 托管搜索，不再提供给每一个只是实现了 Responses API 的服务器。

## 选择模型的密钥

`api_key_env` 是环境变量的**名字**，绝不是密钥本身：

```toml
[model]
base_url = "https://gateway.example/v1"
api_key_env = "MODEL_GATEWAY_KEY"
api = "responses"
main = "your-main-model"
fast = "your-fast-model"
```

把 `MODEL_GATEWAY_KEY` 放进 agent 自己的 `.env` 文件，或者进程的环境变量里。如果你指定了自定义的变量名，但它没设，就是错误：agent 绝不会悄悄退回去用 `OPENAI_API_KEY`。`pa doctor`、`pa service install` 和 `pa eval run --live`，找密钥的办法都一样。

为了兼容，用默认密钥名的自定义地址，可以不带密钥运行。这时 agent 发送的是 `not-needed`，本地服务器可以不理它。如果你的本地服务器不需要登录，但你又有云端的密钥，就给本地服务器单独起一个变量，值随便填。这样，不相干的云端密钥，不会跟着请求发给它。

密钥名只能用大写字母、数字和下划线，最长 64 个字符，不能以数字开头。密钥属于 agent 的 `.env`，`pa service install --pass-env` 会拒绝传这些变量。

## 本地文本模型，加单独的语音服务

```toml
[model]
base_url = "http://127.0.0.1:8000/v1"
api = "chat_completions"
api_key_env = "LOCAL_MODEL_AUTH"
main = "your-local-model"
fast = "your-local-model"

[media]
voice = true
transcribe_base_url = "https://api.openai.com/v1"
transcribe_api_key_env = "OPENAI_API_KEY"
transcribe_model = "gpt-transcribe"
```

agent 自己的 `.env` 里：

```dotenv
# 只有本地服务器不需要认证的时候，才能填个假值。
LOCAL_MODEL_AUTH=not-needed
OPENAI_API_KEY=put-your-speech-service-key-here
```

把模型名和地址，换成你的服务真正提供的。上面的语音模型，是本项目的默认值，不代表每个账号或兼容服务都有它。

语音设置怎么组合：

- `transcribe_base_url` 优先于模型的地址，也优先于 `OPENAI_BASE_URL`。
- `transcribe_api_key_env` 单独覆盖模型的密钥名。
- 没写的设置，继承对应的模型设置，所以已有的安装照常工作。
- 两个服务商不一样的时候，**两项**覆盖都要写，这样两者的对应关系才一目了然。

兼容 OpenAI 的语音服务，必须实现 `POST /audio/transcriptions`，并返回转写出来的 `text`。把文本模型指向一台新服务器，并不会让这台服务器有语音识别。`pa doctor` 会报告语音走哪条路、缺不缺密钥，但不会发送音频。

## 告诉它托管搜索能不能用

托管搜索，要模型端点和工具设置都允许才行：

```toml
[model]
base_url = "https://gateway.example/v1"
api_key_env = "MODEL_GATEWAY_KEY"
api = "responses"

[model.capabilities]
hosted_web_search = true

[tools]
web_search = "auto"
```

| `model.capabilities.hosted_web_search` | 效果 |
| --- | --- |
| 没设 | 只对 OpenAI 官方的 `https://api.openai.com/v1` 自动打开，默认配置也算在内。 |
| `true` | 你是在告诉 agent：这个兼容 Responses 的服务，支持托管的 `web_search` 工具。agent 不会去测。 |
| `false` | 这个端点永远不提供托管搜索。 |

自动判断看的是最终地址，包括 `OPENAI_BASE_URL`。长得像的域名、别的端口、随便什么网关路径，都不算 OpenAI。Chat Completions 永远拿不到这个工具，它只存在于 Responses API 里，就算你把这项能力设成 `true`，也一样。

平时的限制，照样有效。`tools.web_search = "off"`、`deny` 规则和这次 run 的工具画像（profile），都能让搜索消失。`cached` 仍然会去查服务商的搜索索引，不是你本机上的离线缓存。`web_fetch` 和 `http_request` 是另外两个本地的 HTTP 客户端，托管搜索用不了的时候，它们照样开着。想加别的搜索服务，用 MCP 服务器，或者 Python 工具插件。现在还没有通用的 `SearchProvider` 注册表。

## 数据、费用和兼容性

- 模型端点会收到一次请求需要的东西：指令、历史、记住的上下文，和工具结果。当前上下文里的图片，随请求一起上传。
- 语音转写，会把音频字节发给你选的语音端点。转写出来的文字，再进入和模型的对话。在本地用 `ffmpeg` 转换音频，并不会让转写变成本地的。
- `store = false` 要求用无状态的 Responses 处理方式，SDK 的追踪是关的。
- 主模型和快速模型，仍然共用一套服务商设置。模型故障切换、按服务商分别定价，以及在服务商之间搬检查点，都不在范围内。如果一个模型的价格未知，agent 沿用 `pa doctor` 里说明的保守估价。所以，一个免费的本地模型，也可能用光 agent 估算出来的预算。
- 你选的端点，必须支持工具、历史回放、推理参数、图片，以及 Guardian（`auto` 模式的审阅模型）的结构化输出。`pa doctor --live` 会发两次要花钱的模型请求，来试工具和回放。
- 网页上的交互聊天，可以请求答案的流式预览。如果你的兼容端点只会返回完整的响应，就设 `[web] stream_responses = false`。最终答案仍然走可靠的投递路径。

如果你的语音服务用别的协议，就实现公开的 `Transcriber` 接口（`async transcribe(data, *, mime, name) -> str`），再用 `build_app(transcriber=...)` 传进去。如果你再加一个 `async aclose()`，服务停止的时候会调用它。这是代码层面的接入点；还不能安装一个语音服务，再在设置里选它。
