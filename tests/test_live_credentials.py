from __future__ import annotations

import pytest

import live_credentials


def test_injected_live_key_never_reads_or_writes_the_test_home(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", " injected-test-key ")

    def unexpected_read(*args, **kwargs):
        raise AssertionError("an injected credential must not read a stale file")

    monkeypatch.setattr(live_credentials, "dotenv_values", unexpected_read)
    home = tmp_path / "not-created"
    assert live_credentials.live_key(home) == "injected-test-key"
    assert not home.exists()


def test_live_key_can_use_an_existing_operator_owned_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=file-test-key\n", encoding="utf-8")
    assert live_credentials.live_key(tmp_path) == "file-test-key"


def test_explicit_live_run_without_credentials_fails_instead_of_silently_skipping(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Live tests were requested"):
        live_credentials.live_key(tmp_path / "missing")


def test_empty_live_key_in_file_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", " ")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is missing"):
        live_credentials.live_key(tmp_path)
