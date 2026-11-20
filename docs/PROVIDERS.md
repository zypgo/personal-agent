# Model, speech and search providers

The agent runs on your own computer. Three things go out to services you choose: the model, speech transcription and hosted web search. This guide shows how to set up each one, and which settings they still share.

## If you already have a working setup

Nothing changes unless you add new settings:

- The text model's address comes from `[model] base_url`. If that is not set, `OPENAI_BASE_URL`. If that is not set either, the OpenAI default.
- The key comes from `OPENAI_API_KEY`.
- Speech uses the same address and key as the text model. Its model name still comes from `[media] transcribe_model`.
- `model.main`, `model.fast` and `context.model` only pick model names at the same address. They do not pick a different provider.

Two checks are stricter than before:

- If you write the native OpenAI URL yourself, a key is now required.
- Hosted search is no longer offered to every server that merely implements the Responses API.

Both stop the agent from sending requests that the service never said it could handle.

## Choose the model's key

`api_key_env` is the **name** of an environment variable. It is never the secret itself:

```toml
[model]
base_url = "https://gateway.example/v1"
api_key_env = "MODEL_GATEWAY_KEY"
api = "responses"
main = "your-main-model"
fast = "your-fast-model"
```

Put `MODEL_GATEWAY_KEY` in the agent's own `.env` file, or in the process environment. If you name a custom variable and it is missing, that is an error. The agent never quietly falls back to `OPENAI_API_KEY`. `pa doctor`, `pa service install` and `pa eval run --live` all find the key the same way.

For compatibility, a custom endpoint that still uses the default key name can run without a key. The agent then sends `not-needed`, which local servers can ignore. If your local server needs no login but you also have cloud keys, give the local server its own variable with a dummy value. That keeps an unrelated cloud key out of its requests.

A key name uses uppercase letters, digits and underscores, is at most 64 characters long, and cannot start with a digit. Any key value you configure is hidden from the agent's output and logs, even if its name does not contain `KEY`, `TOKEN` or `SECRET`. `pa service install --pass-env` also refuses these variables. Keys belong in the agent's `.env`, not in a LaunchAgent job file or in its dry-run output.

## A local text model with a separate speech service

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

Replace the model names and addresses with the ones your services really offer. The speech model above is this project's default. It does not mean every account or compatible service has it.

How the speech settings combine:

- `transcribe_base_url` wins over the model URL and over `OPENAI_BASE_URL`.
- `transcribe_api_key_env` separately overrides the model's key name.
- A setting you leave out inherits the matching model setting, so existing installs keep working.
- When the two providers differ, set **both** overrides so the link between them is explicit.

An OpenAI-compatible speech service must implement `POST /audio/transcriptions` and return the transcribed `text`. Pointing the text model at a new server does not give that server speech recognition. `pa doctor` reports the speech route and any missing key without sending audio. It does not claim to have tested speech compatibility.

## Say whether hosted search is supported

Hosted search needs both the model endpoint and the tool settings to allow it:

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

| `model.capabilities.hosted_web_search` | What happens |
| --- | --- |
| Not set | Turned on automatically only for the native `https://api.openai.com/v1` endpoint, including the default setup. |
| `true` | You are telling the agent that this Responses-compatible service supports the hosted `web_search` tool. The agent does not test this. |
| `false` | Hosted search is never offered at this endpoint. |

The automatic check looks at the final address, including `OPENAI_BASE_URL`. Lookalike domains, other ports and arbitrary gateway paths do not count as OpenAI. Chat Completions never gets this tool, which exists only in the Responses API, even if you set the capability to `true`.

The usual limits still apply. `tools.web_search = "off"`, deny rules and the run's tool profile can all remove search. `cached` still queries the provider's search index; it is not an offline cache on your machine. The `web_fetch` and `http_request` tools are separate local HTTP clients, and they stay on when hosted search is unavailable. To add another search provider, use an MCP server or a Python tool plugin. There is no general `SearchProvider` registry yet.

## Data, cost and compatibility

- The model endpoint receives what a request needs: instructions, history, remembered context and tool results. Images in the current context are uploaded with the request.
- Speech sends the audio bytes to the speech endpoint you chose. The transcribed text then goes into the model conversation. Converting audio locally with `ffmpeg` does not make the transcription local.
- `store = false` asks for stateless Responses handling, and SDK tracing is off. Neither setting means the data stays on your computer, and neither decides how long a provider keeps it.
- The main and fast models still share one provider setup. Model failover, per-provider pricing and moving checkpoints between providers are not covered. If a model's price is unknown, the agent keeps the cautious estimate that `pa doctor` describes. So a free local model can still use up the agent's estimated budget.
- A URL that looks compatible is not a guarantee. The endpoint you choose has to support tools, history replay, reasoning parameters, images and Guardian's structured output. `pa doctor --live` makes two paid model requests to try tools and replay. It does not test every speech, image or approval-resume path.
- Interactive web chats can ask for a streaming preview of the answer. If your compatible endpoint only returns complete responses, set `[web] stream_responses = false`. Final answers still go through the durable delivery path.

If your speech service uses another protocol, implement the public `Transcriber` interface (`async transcribe(data, *, mime, name) -> str`) and pass it in with `build_app(transcriber=...)`. If you add an `async aclose()`, it is called when services stop. This is a code-level hook. You cannot yet install a speech provider and pick it in the settings.
