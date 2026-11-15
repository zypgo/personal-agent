"""The credential broker: secrets are filled in only for the hosts they were meant for."""

from __future__ import annotations

import pytest

from personal_agent.config import SecretConfig
from personal_agent.security.broker import SecretBroker, SecretProblem, host_matches

TOKEN = "ghp_" + "A1b2C3d4" * 4


def broker(**env) -> SecretBroker:
    return SecretBroker(
        {
            "github": SecretConfig(
                env="GITHUB_TOKEN", hosts=["api.github.com", "*.githubusercontent.com"]
            ),
            "weather": SecretConfig(env="WEATHER_KEY", hosts=["api.weather.example"]),
        },
        {"GITHUB_TOKEN": TOKEN, "WEATHER_KEY": "w-123", **env},
    )


@pytest.mark.parametrize(
    ("pattern", "host", "expected"),
    [
        ("api.github.com", "api.github.com", True),
        ("api.github.com", "API.GitHub.com.", True),
        ("api.github.com", "evil.api.github.com", False),
        ("api.github.com", "github.com", False),
        ("api.github.com", "api.github.com.evil.com", False),
        ("*.example.com", "a.example.com", True),
        ("*.example.com", "a.b.example.com", True),
        ("*.example.com", "example.com", False),  # a wildcard is for subdomains, not the apex
        ("*.example.com", "evilexample.com", False),
        ("*.example.com", "example.com.evil.com", False),
        ("*.example.com", "xexample.com", False),
    ],
)
def test_host_patterns(pattern, host, expected):
    assert host_matches(pattern, host) is expected


def test_names_used_are_found_across_all_the_texts():
    b = broker()
    found = b.used_in(
        "https://x/?k={{secret:weather}}", None, "Bearer {{ secret:github }}", "no secrets here"
    )
    assert found == {"github", "weather"}
    assert b.used_in(None, "", "plain {braces} and {{notasecret}}") == set()


def test_a_secret_may_go_to_its_own_hosts_over_https():
    b = broker()
    b.check({"github"}, host="api.github.com", https=True)
    b.check({"github"}, host="raw.githubusercontent.com", https=True)
    b.check({"weather"}, host="api.weather.example", https=True)


def test_a_secret_is_refused_for_any_other_host():
    with pytest.raises(
        SecretProblem, match=r"only be sent to api\.github\.com.*not to evil\.example"
    ):
        broker().check({"github"}, host="evil.example", https=True)


def test_two_secrets_in_one_request_must_both_fit_the_host():
    with pytest.raises(SecretProblem, match="weather"):
        broker().check({"github", "weather"}, host="api.github.com", https=True)


def test_a_secret_is_not_sent_over_plain_http():
    with pytest.raises(SecretProblem, match="https"):
        broker().check({"github"}, host="api.github.com", https=False)


def test_plain_http_is_acceptable_for_hosts_the_owner_listed_as_private():
    broker().check({"github"}, host="api.github.com", https=False, private_ok=True)


def test_an_unknown_secret_says_what_exists():
    with pytest.raises(SecretProblem, match=r"no secret named 'nope'.*github, weather"):
        broker().check({"nope"}, host="api.github.com", https=True)
    with pytest.raises(SecretProblem, match="none are configured"):
        SecretBroker({}, {}).check({"x"}, host="a.example", https=True)


def test_a_secret_without_a_value_tells_the_owner_what_to_set():
    with pytest.raises(SecretProblem, match="GITHUB_TOKEN"):
        broker(GITHUB_TOKEN="").check({"github"}, host="api.github.com", https=True)
    b = SecretBroker({"g": SecretConfig(env="MISSING_VAR", hosts=["a.example"])}, {})
    with pytest.raises(SecretProblem, match="MISSING_VAR"):
        b.check({"g"}, host="a.example", https=True)


def test_fill_replaces_every_placeholder_and_nothing_else():
    b = broker()
    text = "Bearer {{secret:github}} and {{ secret:weather }} but {{other}} and {x}"
    assert b.fill(text) == f"Bearer {TOKEN} and w-123 but {{{{other}}}} and {{x}}"


@pytest.mark.parametrize(
    "text",
    [
        "{{secret:}}",
        "{{secret:1abc}}",
        "{{secret:with-dash}}",
        "{{secret:two words}}",
        "{{secret:a.b}}",
    ],
)
def test_a_placeholder_that_does_not_parse_is_rejected_instead_of_being_sent_as_text(text):
    with pytest.raises(SecretProblem, match="not a valid secret placeholder"):
        broker().reject_malformed("fine", text)


def test_well_formed_placeholders_and_plain_text_pass_the_malformed_check():
    broker().reject_malformed("{{secret:github}}", None, "", "{{ secret:weather }}", "x {y}")


def test_the_description_names_secrets_and_hosts_but_never_values():
    text = broker().describe()
    assert (
        "{{secret:github}}" in text
        and "api.github.com" in text
        and "*.githubusercontent.com" in text
    )
    assert TOKEN not in text and "w-123" not in text
    assert broker().names() == ["github", "weather"]
