from __future__ import annotations

import os
from pathlib import Path

import pytest

from personal_agent.config import Config, ConfigError, apply_environment, load_config
from personal_agent.deploy import prepare_home, validate_control_config
from personal_agent.paths import Paths


def test_deployment_environment_is_explicit_preserves_input_and_local_defaults():
    original = Config()
    assert apply_environment(original, {"PORT": "9000"}) is original
    cfg = apply_environment(
        original,
        {
            "PA_CONTROL_ONLY": "1",
            "RAILWAY_PUBLIC_DOMAIN": "example.up.railway.app",
            "PORT": "9000",
        },
    )
    assert not cfg.tools.shell
    assert cfg.web.enabled and cfg.web.port == 9000
    assert cfg.web.host == "0.0.0.0"
    assert cfg.web.address == "https://example.up.railway.app"
    assert original.tools.shell and not original.web.enabled
    validate_control_config(cfg)


def test_explicit_public_origin_wins_and_does_not_widen_local_bind():
    cfg = apply_environment(
        Config(),
        {
            "PA_WEB_PUBLIC_URL": "https://agent.example.test",
            "RAILWAY_PUBLIC_DOMAIN": "unused.up.railway.app",
            "PORT": "9999",
        },
    )
    assert cfg.web.public_url == "https://agent.example.test"
    assert cfg.web.host == "127.0.0.1" and cfg.web.port == 8765
    assert cfg.web.allowed_hosts == []


def test_config_overlay_never_persists_environment_or_prints_misplaced_secret(
    tmp_path, monkeypatch
):
    paths = Paths(tmp_path)
    before = '[agent]\nname = "test control"\n'
    paths.config_file.write_text(before)
    monkeypatch.setenv("PA_CONTROL_ONLY", "1")
    monkeypatch.setenv("PA_WEB_PUBLIC_URL", "https://agent.example.test")
    assert load_config(paths).web.enabled
    assert paths.config_file.read_text() == before
    monkeypatch.setenv("PA_WEB_PUBLIC_URL", "https://user:do-not-print@example.test")
    with pytest.raises(ConfigError) as error:
        load_config(paths)
    assert "do-not-print" not in str(error.value)
    assert paths.config_file.read_text() == before


@pytest.mark.parametrize("port", ["0", "65536", "not-a-port"])
def test_deployment_rejects_invalid_port(port):
    with pytest.raises(ValueError):
        apply_environment(
            Config(),
            {"PA_CONTROL_ONLY": "1", "PA_WEB_PUBLIC_URL": "https://example.test", "PORT": port},
        )


def control_config(**changes):
    return apply_environment(
        Config.model_validate(changes),
        {"PA_CONTROL_ONLY": "1", "PA_WEB_PUBLIC_URL": "https://example.test"},
    )


def test_deployment_rejects_plaintext_local_processes_and_accepts_remote_mcp():
    with pytest.raises(ValueError, match="HTTPS"):
        validate_control_config(
            apply_environment(
                Config(), {"PA_CONTROL_ONLY": "1", "PA_WEB_PUBLIC_URL": "http://example.test"}
            )
        )
    with pytest.raises(ValueError, match="plugins"):
        validate_control_config(control_config(plugins={"enabled": ["operator-plugin"]}))
    with pytest.raises(ValueError, match="stdio"):
        validate_control_config(
            control_config(mcp={"servers": [{"name": "local", "command": "some-server"}]})
        )
    validate_control_config(
        control_config(
            mcp={
                "servers": [
                    {"name": "remote", "transport": "http", "url": "https://tools.example.test"}
                ]
            }
        )
    )


def test_volume_initializer_drops_privileges_before_application_load(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: calls.append(("chown", args)))
    monkeypatch.setattr(os, "setgroups", lambda value: calls.append(("setgroups", value)))
    monkeypatch.setattr(os, "setgid", lambda value: calls.append(("setgid", value)))
    monkeypatch.setattr(os, "setuid", lambda value: calls.append(("setuid", value)))
    home = tmp_path / "volume"
    assert prepare_home(home) == home
    assert [name for name, _ in calls] == ["chown", "setgroups", "setgid", "setuid"]
    assert calls[-2:] == [("setgid", 10001), ("setuid", 10001)]
    assert home.stat().st_mode & 0o777 == 0o700


def test_volume_initializer_refuses_symlink_and_relative_path(tmp_path):
    home = tmp_path / "real"
    home.mkdir()
    link = tmp_path / "link"
    link.symlink_to(home, target_is_directory=True)
    for invalid in (Path("relative"), Path("/"), link):
        with pytest.raises(ValueError, match="absolute"):
            prepare_home(invalid)


@pytest.mark.parametrize("railway", [False, True])
def test_railway_health_hostname_is_not_an_api_host_or_a_trusted_origin(
    make_app, monkeypatch, railway
):
    from web_fake import Web

    if railway:
        monkeypatch.setenv("RAILWAY_SERVICE_ID", "test-service")
    else:
        monkeypatch.delenv("RAILWAY_SERVICE_ID", raising=False)
    web = Web(make_app())
    health_host = {"Host": "healthcheck.railway.app"}
    try:
        expected = 200 if railway else 421
        assert web.client.get("/healthz", headers=health_host).status_code == expected
        assert web.client.head("/healthz", headers=health_host).status_code == expected
        assert web.client.get("/api/status", headers=health_host).status_code == 421
        assert web.client.post("/healthz", headers=health_host).status_code == 421
        assert (
            web.client.get(
                "/healthz", headers={"Origin": "https://healthcheck.railway.app"}
            ).status_code
            == 403
        )
    finally:
        web.client.close()
