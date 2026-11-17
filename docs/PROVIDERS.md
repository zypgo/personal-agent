# Model, speech and search providers

The agent runs locally. Model inference, speech transcription and hosted search use the configured
services. This guide describes their separate configuration boundaries and what remains shared.

## Existing configurations keep working

Without new settings:

- Text models use `[model] base_url`, then `OPENAI_BASE_URL`, then the OpenAI default.
- Credentials come from `OPENAI_API_KEY`.
- Speech inherits the text-model endpoint and credential. Its model name still comes from
  `[media] transcribe_model`.
- `model.main`, `model.fast` and `context.model` select model names at the same endpoint. They do
  not select separate providers.

Two checks are stricter: explicitly writing the native OpenAI URL still requires a key, and hosted
search is no longer offered to every server that implements Responses. Both prevent requests that
the configured service has not advertised it can handle.

## Select a model credential

`api_key_env` is the **name** of an environment variable, never the secret itself:

```toml
[model]
base_url = "https://gateway.example/v1"
api_key_env = "MODEL_GATEWAY_KEY"
api = "responses"
main = "your-main-model"
fast = "your-fast-model"
```

Set `MODEL_GATEWAY_KEY` in the agent's own `.env` file or its process environment. A missing
explicitly named custom variable is an error; the client never substitutes `OPENAI_API_KEY` for it.
`pa doctor`, `pa service install` and `pa eval run --live` use the same credential resolution.

For compatibility, a custom endpoint with the default credential name can still run without a key;
the client supplies `not-needed`, which local servers may ignore. If a local server needs no
authentication but you also hold cloud credentials, give it its own variable with a dummy value.
That keeps an unrelated cloud key out of its requests.

Credential names use uppercase letters, digits and underscores, up to 64 characters, and cannot
start with a digit. Configured credential values participate in the agent's output and log
redaction even when their variable names do not contain `KEY`, `TOKEN` or `SECRET`.
`pa service install --pass-env` also refuses these configured variables: credentials belong in the
agent's `.env`, not in a LaunchAgent job definition or its dry-run output.

## Use a local text model and a separate speech service

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

In the agent's `.env`:

```dotenv
# A dummy is suitable only when your local server does not require authentication.
LOCAL_MODEL_AUTH=not-needed
OPENAI_API_KEY=put-your-speech-service-key-here
```

Replace the model names and addresses with those your services actually provide. The speech model
above is the project's configured default, not a promise that every account or compatible service
offers it.

`transcribe_base_url` takes precedence over the model URL and `OPENAI_BASE_URL`.
`transcribe_api_key_env` independently overrides the model credential name. An omitted setting
inherits its corresponding model setting, preserving existing installations. When providers
differ, set **both** overrides so their relationship is explicit.

An OpenAI-compatible speech service must implement `POST /audio/transcriptions` and return the
transcribed `text`. Changing the text-model endpoint alone does not give that server speech
recognition. `pa doctor` reports the speech route and missing credentials without sending audio;
it does not claim to have probed speech compatibility.

## Declare hosted-search support

The model endpoint and tool settings must both permit hosted search:

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

| `model.capabilities.hosted_web_search` | Behaviour |
| --- | --- |
| Omitted | Enabled automatically only for the native `https://api.openai.com/v1` endpoint, including its default configuration. |
| `true` | The owner declares that this Responses-compatible service supports the hosted `web_search` tool. This is not a capability probe. |
| `false` | Never offer hosted search at this endpoint. |

The automatic check uses the resolved endpoint, including `OPENAI_BASE_URL`; lookalike domains,
alternate ports and arbitrary gateway paths are not treated as OpenAI. Chat Completions never
receives this Responses-only tool, even when the capability is declared `true`.

Existing restrictions still apply: `tools.web_search = "off"`, deny rules and the run's tool profile
can remove search. `cached` still queries the provider's search index; it is not an offline local
cache. The `web_fetch` and `http_request` tools remain separate local HTTP clients and are not
disabled when hosted search is unavailable. Another search provider can be added through MCP or a
Python tool plugin; there is no generic `SearchProvider` registry yet.

## Data, costs and compatibility

- The model endpoint receives the instructions, history, remembered context and tool results used
  in a request. Images in the active context are uploaded with model requests.
- Speech sends audio bytes to the selected speech endpoint. Transcribed text then enters the model
  conversation. Local `ffmpeg` conversion does not make transcription local.
- `store = false` requests stateless Responses handling; SDK tracing is disabled. Neither setting
  means that inference data stays on the computer or determines every provider's retention policy.
- Main and fast models still share one provider configuration. Model failover, provider-specific
  pricing and provider-independent checkpoint migration are outside this change. Unknown model
  prices retain the conservative estimate documented by `pa doctor`; a free local model can
  therefore still consume the agent's estimated budget.
- A compatible URL alone does not guarantee compatibility. Tools, history replay, reasoning
  parameters, images and Guardian structured output must be supported by the chosen endpoint.
  `pa doctor --live` makes two paid model requests to exercise tools and replay; it does not test
  every speech, multimodal or approval-resume path.
- Interactive Web chats can request streaming answer previews. For a compatible endpoint that
  only implements complete responses, set `[web] stream_responses = false`; final answers still
  use the durable delivery path.

For a speech implementation with another protocol, implement the public `Transcriber` interface
(`async transcribe(data, *, mime, name) -> str`) and inject it with `build_app(transcriber=...)`.
An optional `async aclose()` is called when services stop. This is a code-level extension point;
it is not yet an install-and-select speech-provider registry.
