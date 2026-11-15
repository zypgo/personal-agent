"""The `[web]` and `[webhooks]` settings: what is accepted, what is refused, and that the message helps
without ever repeating a secret that was pasted into the wrong place."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from personal_agent.config import (
    Config,
    ConfigError,
    WebConfig,
    WebhookRoute,
    WebhooksConfig,
    load_config,
)
from personal_agent.paths import Paths

GOOD_ROUTE = {"scheme": "github", "secret_env": "GITHUB_WEBHOOK_SECRET"}


def refused(model, **values) -> str:
    """The message a refused value gives."""
    with pytest.raises(ValidationError) as caught:
        model(**values)
    return str(caught.value)


# ---- [web] ------------------------------------------------------------------------------------
def test_the_page_is_off_and_private_by_default():
    web = Config().web
    assert web.enabled is False
    assert web.host == "127.0.0.1" and web.port == 8765
    assert web.public_url is None and web.allowed_hosts == []
    assert web.session_days == 30 and web.invite_minutes == 10


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("0.0.0.0", "0.0.0.0"),
        ("::1", "::1"),
        ("[::1]", "::1"),
        ("  100.101.102.103 ", "100.101.102.103"),
        ("localhost", "localhost"),
        ("my-mac.local", "my-mac.local"),
    ],
)
def test_a_listen_address_is_an_ip_or_a_plain_name(given, kept):
    assert WebConfig(host=given).host == kept
    assert WebhooksConfig(host=given).host == kept


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "http://127.0.0.1",
        "127.0.0.1:8765",  # the port has its own setting
        "127.0.0.1/",
        "my mac",
        "-leading.example",
        "[::1",
    ],
)
def test_a_listen_address_with_a_scheme_a_port_or_a_path_is_refused(bad):
    assert "without http:// or a port" in refused(WebConfig, host=bad)
    assert "without http:// or a port" in refused(WebhooksConfig, host=bad)


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_a_port_must_be_a_real_port(port):
    refused(WebConfig, port=port)
    refused(WebhooksConfig, port=port)


@pytest.mark.parametrize("port", [1, 8765, 65535])
def test_ordinary_ports_are_accepted(port):
    assert WebConfig(port=port).port == port


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("https://my-mac.tailnet.ts.net", "https://my-mac.tailnet.ts.net"),
        ("https://My-Mac.Tailnet.TS.net/", "https://my-mac.tailnet.ts.net"),
        ("https://phone.example.com:8443", "https://phone.example.com:8443"),
        ("http://localhost:8765", "http://localhost:8765"),
        ("  https://a.example  ", "https://a.example"),
    ],
)
def test_the_public_address_is_an_origin_in_its_plain_form(given, kept):
    assert WebConfig(public_url=given).public_url == kept
    assert WebhooksConfig(public_url=given).public_url == kept


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_an_empty_public_address_means_there_is_none(empty):
    assert WebConfig(public_url=empty).public_url is None


@pytest.mark.parametrize(
    "bad",
    [
        "my-mac.tailnet.ts.net",  # no scheme
        "ftp://example.com",
        "https://",
        "https://example.com/chat",  # a path
        "https://example.com?x=1",
        "https://example.com/#frag",
    ],
)
def test_a_public_address_that_is_not_an_origin_is_refused(bad):
    refused(WebConfig, public_url=bad)
    refused(WebhooksConfig, public_url=bad)


def test_credentials_in_the_public_address_are_refused():
    assert "do not put credentials" in refused(WebConfig, public_url="https://frank:x@example.com")


def test_extra_names_the_page_may_be_reached_under_are_plain_host_names():
    cfg = WebConfig(allowed_hosts=["Mac.Local", "localhost", "my-mac.tailnet.ts.net"])
    assert cfg.allowed_hosts == ["mac.local", "localhost", "my-mac.tailnet.ts.net"]


@pytest.mark.parametrize("bad", ["http://mac.local", "mac.local:8765", "mac local", "", "a/b"])
def test_an_allowed_host_with_a_scheme_or_port_is_refused(bad):
    assert "not a host name" in refused(WebConfig, allowed_hosts=[bad])


@pytest.mark.parametrize(
    ("field", "low", "high"), [("session_days", 1, 365), ("invite_minutes", 1, 1440)]
)
def test_lifetimes_have_sensible_limits(field, low, high):
    assert getattr(WebConfig(**{field: low}), field) == low
    assert getattr(WebConfig(**{field: high}), field) == high
    refused(WebConfig, **{field: low - 1})
    refused(WebConfig, **{field: high + 1})


def test_a_misspelt_setting_is_refused_instead_of_ignored():
    assert "Extra inputs are not permitted" in refused(WebConfig, enable=True)


# ---- [webhooks] -------------------------------------------------------------------------------
def test_webhooks_are_off_by_default_and_listen_beside_the_page_not_on_it():
    hooks = Config().webhooks
    assert hooks.enabled is False and hooks.routes == {}
    assert hooks.host == "127.0.0.1" and hooks.port == 8766
    assert hooks.port != Config().web.port
    assert hooks.max_body_bytes == 262_144 and hooks.rate_per_minute == 30


@pytest.mark.parametrize("name", ["github", "ci-main", "a_b", "0day", "x" * 32])
def test_route_names_that_are_safe_in_a_url_are_accepted(name):
    assert name in WebhooksConfig(routes={name: GOOD_ROUTE}).routes


@pytest.mark.parametrize(
    "name", ["", "GitHub", "-first", "_first", "with space", "a/b", "dot.dot", "x" * 33, "naïve"]
)
def test_other_route_names_are_refused(name):
    assert "lowercase letters, digits" in refused(WebhooksConfig, routes={name: GOOD_ROUTE})


@pytest.mark.parametrize("scheme", ["github", "gitlab", "standard", "generic"])
def test_the_four_schemes_are_accepted(scheme):
    assert WebhookRoute(scheme=scheme, secret_env="SECRET_X").scheme == scheme


@pytest.mark.parametrize("scheme", ["githb", "GitHub", "auto", "", "hmac"])
def test_a_scheme_is_never_guessed_or_misspelt(scheme):
    refused(WebhookRoute, scheme=scheme, secret_env="SECRET_X")


def test_a_route_needs_both_a_scheme_and_the_name_of_its_secret():
    assert "scheme" in refused(WebhookRoute, secret_env="SECRET_X")
    assert "secret_env" in refused(WebhookRoute, scheme="github")


@pytest.mark.parametrize("name", ["GITHUB_WEBHOOK_SECRET", "_X", "A1", "X" * 64])
def test_the_secret_is_named_by_an_environment_variable(name):
    assert WebhookRoute(scheme="github", secret_env=name).secret_env == name


@pytest.mark.parametrize("name", ["github_secret", "Mixed_Case", "X" * 65, "WITH-HYPHEN", "1ABC"])
def test_a_name_in_any_other_shape_is_refused(name):
    assert "in capital letters" in refused(WebhookRoute, scheme="github", secret_env=name)


@pytest.mark.parametrize(
    "pasted",
    [
        "whsec_" + "Zm9v" * 8 + "==",
        "sk-" + "a1b2c3d4" * 4,
        "my secret value",
        "abc-123-def",
        "9starts_with_digit",
        "",
    ],
)
def test_a_secret_pasted_where_its_name_belongs_is_refused(pasted):
    # (What the person sees when this happens, and that it never repeats the value, is tested
    # below through `load_config`, which is what reads config.toml.)
    assert "NAME of an environment variable" in refused(
        WebhookRoute, scheme="github", secret_env=pasted
    )


@pytest.mark.parametrize(
    ("field", "low", "high"),
    [("tolerance_s", 30, 3600)],
)
def test_the_allowed_age_of_a_signature_has_limits(field, low, high):
    for good in (low, high):
        assert getattr(WebhookRoute(**GOOD_ROUTE, **{field: good}), field) == good
    refused(WebhookRoute, **GOOD_ROUTE, **{field: low - 1})
    refused(WebhookRoute, **GOOD_ROUTE, **{field: high + 1})


def test_the_instructions_for_a_route_have_a_length_limit():
    assert WebhookRoute(**GOOD_ROUTE, prompt="x" * 4000).prompt
    refused(WebhookRoute, **GOOD_ROUTE, prompt="x" * 4001)


def test_a_route_can_name_the_events_that_wake_the_agent():
    route = WebhookRoute(**GOOD_ROUTE, events=["push", "pull_request"])
    assert route.events == ["push", "pull_request"]
    assert WebhookRoute(**GOOD_ROUTE).events == []  # none named: all of them


def test_a_misspelt_route_setting_is_refused():
    assert "Extra inputs are not permitted" in refused(WebhookRoute, **GOOD_ROUTE, secret="x")


@pytest.mark.parametrize(
    ("field", "low", "high"),
    [("max_body_bytes", 1024, 5_000_000), ("rate_per_minute", 1, 600)],
)
def test_the_listener_limits_cannot_be_set_to_nothing_or_to_everything(field, low, high):
    for good in (low, high):
        assert getattr(WebhooksConfig(**{field: good}), field) == good
    refused(WebhooksConfig, **{field: low - 1})
    refused(WebhooksConfig, **{field: high + 1})


# ---- what a person sees when config.toml is wrong ---------------------------------------------
def config_error(tmp_path, toml: str) -> str:
    paths = Paths.resolve(tmp_path / "home")
    paths.ensure()
    paths.config_file.write_text(toml)
    with pytest.raises(ConfigError) as caught:
        load_config(paths)
    return str(caught.value)


def test_the_message_says_which_setting_is_wrong(tmp_path):
    text = config_error(
        tmp_path,
        '[webhooks]\nenabled = true\n[webhooks.routes.github]\nscheme = "github"\n'
        'secret_env = "not a name"\n',
    )
    assert "config.toml" in text
    assert "webhooks.routes.github.secret_env" in text
    assert "NAME of an environment variable" in text


def test_a_secret_pasted_into_config_toml_is_not_printed_back(tmp_path):
    secret = "whsec_" + "Zm9vYmFy" * 5
    text = config_error(
        tmp_path,
        "[webhooks]\nenabled = true\n[webhooks.routes.github]\n"
        f'scheme = "github"\nsecret_env = "{secret}"\n',
    )
    assert secret not in text and "Zm9vYmFy" not in text
    assert "webhooks.routes.github.secret_env" in text


def test_a_token_pasted_into_the_telegram_section_is_not_printed_back(tmp_path):
    token = "123456789" + ":" + "A" * 35
    text = config_error(tmp_path, f'[telegram]\nenabled = true\ntoken_env = "{token}"\n')
    assert token not in text and "telegram.token_env" in text


def test_credentials_in_a_public_address_are_not_printed_back(tmp_path):
    text = config_error(
        tmp_path, '[web]\nenabled = true\npublic_url = "https://frank:hunter2@mac.example.com"\n'
    )
    assert "hunter2" not in text and "web.public_url" in text


def test_every_problem_is_listed_once_on_its_own_line(tmp_path):
    text = config_error(tmp_path, '[web]\nport = 0\nenable = true\nhost = "http://x"\n')
    lines = [line.strip() for line in text.splitlines()[1:]]
    assert len(lines) == 3
    assert any(line.startswith("web.port:") for line in lines)
    assert any(line.startswith("web.enable:") and "not permitted" in line for line in lines)
    assert any(line.startswith("web.host:") for line in lines)


@pytest.mark.parametrize("toml", ["[web]\nport = 0\n", '[web]\nhost = "http://x"\n'])
def test_the_message_has_no_library_noise(tmp_path, toml):
    text = config_error(tmp_path, toml)
    assert "pydantic" not in text and "errors.pydantic.dev" not in text
    assert "Value error," not in text and "input_value" not in text


def test_a_file_that_is_not_toml_says_so(tmp_path):
    assert "cannot read" in config_error(tmp_path, "[web\nenabled = true\n")


# ---- the same rule wherever a setting names a secret ------------------------------------------
@pytest.mark.parametrize("pasted", ["123456789:" + "A" * 35, "sk-" + "a1b2" * 8, "lower_case"])
def test_every_setting_that_names_a_secret_wants_a_name_and_not_a_value(pasted):
    from personal_agent.config import SecretConfig, TelegramConfig

    assert "NAME of an environment variable" in refused(TelegramConfig, token_env=pasted)
    assert "NAME of an environment variable" in refused(
        SecretConfig, env=pasted, hosts=["api.example.com"]
    )
    assert "NAME of an environment variable" in refused(
        WebhookRoute, scheme="github", secret_env=pasted
    )


def test_a_secret_made_of_capitals_digits_and_underscores_cannot_be_told_from_a_name():
    # The one shape the check cannot catch: it is the shape of a name. Documented, not hidden.
    assert WebhookRoute(scheme="github", secret_env="ABCD1234" * 4).secret_env
