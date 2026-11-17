"""Provider boundaries: credentials, speech routing and advertised hosted capabilities agree."""

from __future__ import annotations

import httpx
import pytest
from openai import AsyncOpenAI

from personal_agent.channels.transcribe import OpenAITranscriber
from personal_agent.config import Config, ConfigError, MediaConfig, ModelConfig, load_config
from personal_agent.doctor import _key_check, _speech_checks, _web_checks
from personal_agent.harness.endpoints import model_endpoint, supports_hosted_search
from personal_agent.harness.errors import ModelUnavailable, classify
from personal_agent.harness.models import make_client
from personal_agent.security.redact import known_secret_values, redact


@pytest.mark.parametrize(
    "section, field", [("model", "api_key_env"), ("media", "transcribe_api_key_env")]
)
def test_a_secret_pasted_instead_of_an_environment_name_is_refused_without_echoing_it(
    paths, section, field
):
    secret = "sk-" + "not-the-name-of-an-environment-variable"
    paths.config_file.write_text(f'[{section}]\n{field} = "{secret}"\n')
    with pytest.raises(ConfigError) as error:
        load_config(paths)
    assert "NAME of an environment variable" in str(error.value)
    assert secret not in str(error.value)


@pytest.mark.parametrize(
    "base_url, supported",
    [
        (None, True),
        ("https://api.openai.com/v1", True),
        ("https://API.OPENAI.COM:443/v1/", True),
        ("http://127.0.0.1:8000/v1", False),
        ("https://gateway.example/v1", False),
        ("https://api.openai.com.evil.example/v1", False),
        ("https://api.openai.com@evil.example/v1", False),
        ("https://user@api.openai.com/v1", False),
        ("http://api.openai.com/v1", False),
        ("https://api.openai.com:8443/v1", False),
        ("https://api.openai.com/other", False),
        ("https://api.openai.com/v1?forward=elsewhere", False),
        ("https://api.openai.com/v1#other", False),
        ("https://api.openai.com:invalid/v1", False),
    ],
)
def test_hosted_search_auto_detection_requires_the_native_api(base_url, supported):
    assert supports_hosted_search(ModelConfig(base_url=base_url)) is supported


def test_hosted_search_uses_the_effective_endpoint_and_respects_explicit_capabilities(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1")
    assert not supports_hosted_search(ModelConfig())
    assert supports_hosted_search(ModelConfig(base_url="https://api.openai.com/v1"))
    assert supports_hosted_search(ModelConfig(capabilities={"hosted_web_search": True}))
    assert not supports_hosted_search(
        ModelConfig(api="chat_completions", capabilities={"hosted_web_search": True})
    )
    assert not supports_hosted_search(
        ModelConfig(base_url="https://api.openai.com/v1", capabilities={"hosted_web_search": False})
    )


def test_a_missing_custom_credential_is_not_replaced_by_the_openai_key(paths, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-credential")
    cfg = Config(
        model=ModelConfig(base_url="https://gateway.example/v1", api_key_env="PA_MODEL_AUTH")
    )
    assert model_endpoint(cfg.model).missing_key
    with pytest.raises(ModelUnavailable, match="PA_MODEL_AUTH is not set"):
        make_client(cfg.model)
    check = _key_check(paths, cfg)
    assert check.status == "fail" and "PA_MODEL_AUTH" in check.detail
    assert "unrelated-openai-credential" not in str(check)


async def test_legacy_local_endpoints_can_still_work_without_a_key(paths):
    cfg = Config(model=ModelConfig(base_url="http://127.0.0.1:8000/v1"))
    client = make_client(cfg.model)
    try:
        assert client.api_key == "not-needed"
        assert _key_check(paths, cfg).status == "warn"
    finally:
        await client.close()


async def test_native_openai_needs_a_key_even_when_the_url_was_written_explicitly(paths):
    cfg = Config(model=ModelConfig(base_url="https://api.openai.com/v1"))
    assert _key_check(paths, cfg).status == "fail"
    with pytest.raises(ModelUnavailable, match="OPENAI_API_KEY is not set"):
        make_client(cfg.model)


async def test_actual_requests_keep_speech_and_model_endpoints_and_keys_separate(monkeypatch):
    """Exercise the real OpenAI clients; the transport records HTTP without opening a socket."""
    seen: list[tuple[str, str, bytes]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers["Authorization"], request.read()))
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={"text": "remember the appointment"})
        return httpx.Response(200, json={"id": "resp_test", "output": [], "status": "completed"})

    def client_factory(**options):
        return AsyncOpenAI(
            **options, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )

    monkeypatch.setattr("personal_agent.harness.models.AsyncOpenAI", client_factory)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unrelated.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-credential")
    monkeypatch.setenv("PA_MODEL_AUTH", "model-credential")
    monkeypatch.setenv("PA_SPEECH_AUTH", "speech-credential")
    model = ModelConfig(base_url="https://model.example/v1", api_key_env="PA_MODEL_AUTH")
    media = MediaConfig(
        transcribe_base_url="https://speech.example/v1", transcribe_api_key_env="PA_SPEECH_AUTH"
    )
    text_client = make_client(model)
    speech = OpenAITranscriber(model, media)
    try:
        await text_client.responses.create(model="my-model", input="hello")
        assert (
            await speech.transcribe(b"audio-bytes", mime="audio/mpeg") == "remember the appointment"
        )
    finally:
        await text_client.close()
        await speech.aclose()
    assert [(url, auth) for url, auth, _ in seen] == [
        ("https://model.example/v1/responses", "Bearer model-credential"),
        ("https://speech.example/v1/audio/transcriptions", "Bearer speech-credential"),
    ]
    assert b"audio-bytes" in seen[1][2]
    assert b"audio-bytes" not in seen[0][2]


async def test_omitted_speech_overrides_preserve_the_model_endpoint_and_custom_key(monkeypatch):
    monkeypatch.setenv("PA_MODEL_AUTH", "model-credential")
    model = ModelConfig(base_url="http://127.0.0.1:8000/v1", api_key_env="PA_MODEL_AUTH")
    speech = OpenAITranscriber(model, MediaConfig())
    try:
        client = speech._client_or_raise()
        assert str(client.base_url) == "http://127.0.0.1:8000/v1/"
        assert client.api_key == "model-credential"
    finally:
        await speech.aclose()


def test_doctor_checks_the_independent_speech_key_without_printing_values(paths, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "model-secret-value")
    cfg = Config(media=MediaConfig(transcribe_api_key_env="PA_SPEECH_AUTH"))
    assert _key_check(paths, cfg).status == "ok"
    checks = _speech_checks(paths, cfg)
    assert checks[0].status == "fail" and "PA_SPEECH_AUTH" in checks[0].detail
    monkeypatch.setenv("PA_SPEECH_AUTH", "speech-secret-value")
    checks = _speech_checks(paths, cfg)
    assert checks[0].status == "ok"
    assert "speech-secret-value" not in str(checks)
    assert _speech_checks(paths, Config(media=MediaConfig(voice=False))) == []


def test_doctor_does_not_advertise_hosted_search_at_a_compatible_endpoint():
    cfg = Config(
        model=ModelConfig(base_url="http://127.0.0.1:8000/v1"),
        tools={"web_fetch": False, "http_request": False},
    )
    assert "unavailable" in _web_checks(cfg)[0].detail


def test_custom_credential_variables_are_scrubbed_even_without_a_secret_looking_name(
    make_app, monkeypatch
):
    monkeypatch.setenv("PA_MODEL_AUTH", "model-credential-value")
    monkeypatch.setenv("PA_SPEECH_AUTH", "speech-credential-value")
    make_app(
        config=Config(
            model=ModelConfig(api_key_env="PA_MODEL_AUTH"),
            media=MediaConfig(transcribe_api_key_env="PA_SPEECH_AUTH"),
        )
    )
    cleaned = redact("model-credential-value / speech-credential-value", known_secret_values())
    assert "model-credential-value" not in cleaned
    assert "speech-credential-value" not in cleaned
    assert "model-credential-value" not in classify(RuntimeError("model-credential-value")).summary
