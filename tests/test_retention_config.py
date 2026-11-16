"""`[retention]` in config.toml: how long the record of what happened is kept."""

from __future__ import annotations

import pytest

from personal_agent.config import Config, ConfigError, RetentionConfig, load_config
from personal_agent.paths import Paths

FIELDS = ("records_days", "runs_days", "audit_days", "memory_history_days")


def test_the_defaults_keep_bookkeeping_for_months_and_the_audit_trail_for_a_year():
    keep = Config().retention
    assert keep.auto is True
    assert (keep.records_days, keep.runs_days, keep.audit_days, keep.memory_history_days) == (
        90,
        180,
        365,
        365,
    )


def test_each_window_answers_with_its_own_setting():
    keep = RetentionConfig(records_days=1, runs_days=2, audit_days=3, memory_history_days=4)
    assert [keep.days(w) for w in ("records", "runs", "audit", "memory_history")] == [1, 2, 3, 4]


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("bad", [-1, 36_501, 1.5, "soon", None])
def test_a_window_is_a_whole_number_of_days_from_zero_to_a_hundred_years(field, bad):
    with pytest.raises(ValueError):
        RetentionConfig(**{field: bad})


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("good", [0, 1, 36_500])
def test_the_edges_are_allowed_and_zero_means_for_ever(field, good):
    assert RetentionConfig(**{field: good}).days(field.removesuffix("_days")) == good


def test_a_misspelt_key_is_refused_rather_than_silently_ignored():
    with pytest.raises(ValueError, match="record_days"):
        RetentionConfig(record_days=10)


def test_it_is_read_from_the_file(home):
    paths = Paths.resolve(home)
    paths.ensure()
    paths.config_file.write_text("[retention]\nauto = false\nruns_days = 0\naudit_days = 30\n")
    keep = load_config(paths).retention
    assert (keep.auto, keep.runs_days, keep.audit_days, keep.records_days) == (False, 0, 30, 90)


def test_a_bad_value_in_the_file_is_reported_with_where_it_is(home):
    paths = Paths.resolve(home)
    paths.ensure()
    paths.config_file.write_text("[retention]\nruns_days = -5\n")
    with pytest.raises(ConfigError) as caught:
        load_config(paths)
    assert "retention.runs_days" in str(caught.value)
