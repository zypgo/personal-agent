"""The scenario file format: what is accepted, what is refused (and said clearly), what is found."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from personal_agent.evals import spec
from personal_agent.evals.spec import (
    Scenario,
    ScenarioError,
    builtin_dir,
    load_catalog,
    load_file,
    parse,
    user_dir,
)
from personal_agent.paths import Paths

BASE = {
    "id": "demo",
    "title": "Demo",
    "steps": [{"id": "one", "message": "hello"}],
    "checks": [{"kind": "max_messages", "limit": 1}],
}


def make(**changes) -> dict:
    data = copy.deepcopy(BASE)
    data.update(copy.deepcopy(changes))
    return data


def build(**changes) -> Scenario:
    return Scenario.model_validate(make(**changes))


def refuses(match: str, **changes) -> None:
    with pytest.raises(ValueError, match=match):
        build(**changes)


@pytest.fixture
def paths(tmp_path):
    value = Paths(home=tmp_path / "home")
    value.ensure()
    return value


# ---- the smallest scenario, and what makes one wrong ------------------------------------------
def test_the_smallest_scenario_is_valid_and_has_no_surprises():
    scenario = build()
    assert scenario.seed.memory_files() == {} and scenario.mock is None and scenario.tags == []
    assert scenario.checks[0].id is None
    no_pattern = build(
        checks=[{"kind": "says", "any_of": ["x"]}, {"kind": "never_says", "any_of": ["y"]}]
    )
    assert [c.pattern for c in no_pattern.checks] == [None, None]
    spelled_out = build(checks=[{"kind": "says", "any_of": ["x"], "pattern": None}])
    assert spelled_out.checks[0].pattern is None
    assert scenario.steps[0].ends == ["completed", "silent"]


def test_a_misspelt_key_is_an_error_not_a_check_that_is_silently_skipped():
    refuses("extra|Extra", chekcs=[])
    refuses("extra|Extra", steps=[{"id": "one", "message": "x", "mesage": "typo"}])
    refuses("extra|Extra", checks=[{"kind": "max_messages", "limit": 1, "limt": 2}])


def test_an_unknown_kind_of_check_is_refused():
    refuses("kind", checks=[{"kind": "vibes"}])


@pytest.mark.parametrize("bad", ["Demo", "has space", "-lead", "", "a" * 64, "ünï"])
def test_ids_are_lowercase_words(bad):
    refuses("lowercase", id=bad)
    refuses("lowercase", steps=[{"id": bad, "message": "x"}])


def test_an_id_of_the_longest_allowed_length_is_fine():
    assert build(id="a" * 63).id == "a" * 63


@pytest.mark.parametrize("tags", [["Bad"], ["ok", "no spaces"]])
def test_tags_are_lowercase_words(tags):
    refuses("lowercase", tags=tags)


def test_a_scenario_needs_a_step_and_a_check():
    refuses("at least 1", steps=[])
    refuses("at least 1", checks=[])


# ---- steps -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "step",
    [
        {"id": "s"},
        {"id": "s", "message": "x", "timer": {"name": "t", "prompt": "p"}},
        {"id": "s", "timer": {"name": "t", "prompt": "p"}, "webhook": {"name": "w"}},
    ],
)
def test_a_step_is_exactly_one_event(step):
    refuses("exactly one", steps=[step])


def test_each_kind_of_step_knows_its_conversation():
    timer = build(steps=[{"id": "a", "timer": {"name": "parcel.check", "prompt": "p"}}]).steps[0]
    hook = build(steps=[{"id": "a", "webhook": {"name": "github", "body": {"x": 1}}}]).steps[0]
    plain = build(steps=[{"id": "a", "message": "hi"}]).steps[0]
    named = build(steps=[{"id": "a", "message": "hi", "session": "chat-b"}]).steps[0]
    assert (timer.kind, timer.session_key) == ("timer", "timer:parcel.check")
    assert (hook.kind, hook.session_key) == ("webhook", "webhook:github")
    assert (plain.kind, plain.session_key) == ("message", "main")
    assert (named.kind, named.session_key) == ("message", "chat-b")


def test_a_conversation_can_only_be_chosen_for_a_message():
    refuses(
        "only applies to a `message`",
        steps=[{"id": "a", "timer": {"name": "t", "prompt": "p"}, "session": "x"}],
    )
    refuses("lowercase", steps=[{"id": "a", "message": "hi", "session": "Chat B"}])


@pytest.mark.parametrize("name", ["Upper", "with space", "", "../x", "a/b", "a" * 64])
def test_schedules_and_webhooks_have_plain_names(name):
    refuses("lowercase", steps=[{"id": "a", "timer": {"name": name, "prompt": "p"}}])
    refuses("lowercase", steps=[{"id": "a", "webhook": {"name": name}}])


def test_the_ways_a_run_may_end_are_a_closed_list():
    ok = build(steps=[{"id": "a", "message": "x", "ends": ["paused_budget", "failed"]}])
    assert ok.steps[0].ends == ["paused_budget", "failed"]
    refuses("`ends` takes", steps=[{"id": "a", "message": "x", "ends": ["vanished"]}])
    refuses("`ends` takes", steps=[{"id": "a", "message": "x", "ends": []}])


def test_step_ids_must_be_unique():
    refuses(
        "unique",
        steps=[{"id": "a", "message": "x"}, {"id": "a", "message": "y"}],
        checks=[{"kind": "max_messages", "limit": 1}],
    )


# ---- checks --------------------------------------------------------------------------------------
def test_a_check_may_only_name_a_step_that_exists():
    refuses("step 'ghost', which does not exist", checks=[{"kind": "silent", "step": "ghost"}])
    refuses("step 'ghost'", checks=[{"kind": "says", "step": "ghost", "any_of": ["x"]}])


def test_a_check_that_looks_for_nothing_is_refused():
    refuses("`says` needs", checks=[{"kind": "says", "step": "one"}])
    refuses("`never_says` needs", checks=[{"kind": "never_says"}])


def test_a_pattern_must_be_a_regular_expression_of_sane_length():
    refuses("not a regular expression", checks=[{"kind": "says", "pattern": "(unclosed"}])
    refuses("not a regular expression", checks=[{"kind": "never_says", "pattern": "["}])
    refuses("at most 300", checks=[{"kind": "says", "pattern": "a" * 301}])
    ok = build(checks=[{"kind": "says", "pattern": "a" * 300}])
    assert ok.checks[0].pattern == "a" * 300


def test_numbers_must_make_sense():
    refuses("greater than or equal to 0", checks=[{"kind": "max_messages", "limit": -1}])
    refuses("greater than 0", checks=[{"kind": "spend_under", "usd": 0}])
    assert build(checks=[{"kind": "max_messages", "limit": 0}]).checks[0].limit == 0


def test_tool_checks_need_tools():
    refuses("at least 1", checks=[{"kind": "tools_used", "tools": []}])
    refuses("at least 1", checks=[{"kind": "tools_not_used", "tools": []}])
    assert build(checks=[{"kind": "only_tools", "allowed": []}]).checks[0].allowed == []


def test_check_names_are_derived_from_kind_and_step_and_made_unique():
    scenario = build(
        checks=[
            {"kind": "silent", "step": "one"},
            {"kind": "max_messages", "limit": 1},
            {"kind": "max_messages", "limit": 2},
            {"kind": "says", "step": "one", "any_of": ["x"], "id": "greets"},
            {"kind": "says", "step": "one", "any_of": ["y"]},
        ]
    )
    assert [name for name, _ in scenario.labelled_checks()] == [
        "silent-one",
        "max_messages",
        "max_messages-2",
        "greets",
        "says-one",
    ]
    assert scenario.check_ids()[0] == "ends-one"  # every step has its implicit check, first


def test_any_number_of_checks_of_one_kind_get_one_name_each():
    scenario = build(checks=[{"kind": "max_messages", "limit": n} for n in range(1, 5)])
    assert [name for name, _ in scenario.labelled_checks()] == [
        "max_messages",
        "max_messages-2",
        "max_messages-3",
        "max_messages-4",
    ]


def test_explicit_check_ids_must_be_unique_and_plain():
    refuses(
        "unique",
        checks=[
            {"kind": "max_messages", "limit": 1, "id": "same"},
            {"kind": "not_urgent", "id": "same"},
        ],
    )
    refuses("lowercase", checks=[{"kind": "not_urgent", "id": "Not Plain"}])


@pytest.mark.parametrize(
    "bad", ["NotPlain", "UPPER", "quiet-Day", "-lead", "has.dot", "a" * 64, ""]
)
def test_a_check_id_follows_the_same_plain_naming_as_everything_else(bad):
    refuses("lowercase", checks=[{"kind": "not_urgent", "id": bad}])


def test_a_given_id_is_kept_even_when_it_looks_like_a_derived_one():
    scenario = build(
        checks=[
            {"kind": "max_messages", "limit": 1},
            {"kind": "not_urgent", "id": "max_messages"},
        ]
    )
    assert [name for name, _ in scenario.labelled_checks()] == ["max_messages-2", "max_messages"]


def test_an_id_may_not_pose_as_the_implicit_check_of_a_step():
    refuses(
        "implicit `ends-<step>`",
        checks=[{"kind": "not_urgent", "id": "ends-one"}],
    )
    ok = build(checks=[{"kind": "not_urgent", "id": "ends-two"}])  # no step called `two`
    assert ok.labelled_checks()[0][0] == "ends-two"


@pytest.mark.parametrize(
    "bad", ["/etc/passwd", "../out", "a/../../b", "a\\b", "", " ", "a\x00b", "x" * 201]
)
def test_paths_in_a_scenario_stay_inside_its_own_folder(bad):
    refuses("plain relative path", seed={"files": {bad: "x"}})
    refuses("plain relative path", seed={"memory": {bad: "x"}})
    refuses("plain relative path", seed={"preferences": {bad: "x"}})
    refuses("plain relative path", checks=[{"kind": "memory_has", "path": bad}])
    refuses("plain relative path", checks=[{"kind": "memory_untouched", "paths": [bad]}])


def test_a_path_of_the_longest_allowed_length_is_fine():
    longest = "a" * 196 + ".txt"
    assert len(longest) == 200
    assert build(seed={"files": {longest: "x"}}).seed.files == {longest: "x"}


def test_nested_paths_are_fine_and_are_normalised():
    scenario = build(seed={"files": {"inbox/report.txt": "x", "a//b.txt": "y"}})
    assert set(scenario.seed.files) == {"inbox/report.txt", "a/b.txt"}


@pytest.mark.parametrize("name", ["sub/dir.md", "plain.txt", ".hidden.md"])
def test_progress_files_are_plain_markdown_files(name):
    refuses("plain file name ending in .md", seed={"tasks": {name: "x"}})


def test_seeded_memory_is_listed_by_its_place_in_the_memory_folder():
    seed = build(
        seed={
            "preferences": {"style.md": "p"},
            "memory": {"notes/a.md": "m"},
            "tasks": {"job.md": "t"},
        }
    ).seed
    assert seed.memory_files() == {
        "preferences/style.md": "p",
        "state/notes/a.md": "m",
        "state/tasks/job.md": "t",
    }


def test_an_untouched_path_is_normalised_like_the_seeded_one():
    sc = build(
        seed={"preferences": {"style.md": "p"}},
        checks=[{"kind": "memory_untouched", "paths": ["preferences//style.md"]}],
    )
    assert sc.checks[0].paths == ["preferences/style.md"]


def test_untouched_files_must_be_ones_the_seed_creates():
    ok = build(
        seed={"preferences": {"style.md": "p"}},
        checks=[{"kind": "memory_untouched", "paths": ["preferences/style.md"]}],
    )
    assert ok.checks[0].paths == ["preferences/style.md"]
    refuses(
        "which the seed does not create",
        checks=[{"kind": "memory_untouched", "paths": ["preferences/other.md"]}],
    )
    refuses("at least 1", checks=[{"kind": "memory_untouched", "paths": []}])


# ---- scripts for the scripted model ---------------------------------------------------------------
def with_mock(mock: dict) -> dict:
    return {"mock": mock}


def test_an_action_is_exactly_one_thing():
    refuses("exactly one", **with_mock({"good": {"one": [{}]}}))
    refuses("exactly one", **with_mock({"good": {"one": [{"send": "a", "say": "b"}]}}))
    ok = build(
        **with_mock(
            {
                "good": {
                    "one": [
                        {"send": "a", "urgency": "low"},
                        {"memory": {"command": "view", "path": "/memories"}},
                        {"tool": {"name": "x", "args": {"k": 1}}},
                        {"say": "done"},
                    ]
                }
            }
        )
    )
    assert len(ok.mock.good["one"]) == 4


def test_urgency_is_one_of_the_three_the_tool_takes():
    refuses("urgency", **with_mock({"good": {"one": [{"send": "a", "urgency": "panic"}]}}))


def test_scripts_may_only_speak_of_steps_that_exist():
    refuses("good script has a step 'ghost'", **with_mock({"good": {"ghost": []}}))
    refuses(
        "script has a step 'ghost'",
        **with_mock({"bad": [{"name": "b", "fails": ["max_messages"], "script": {"ghost": []}}]}),
    )


def test_a_bad_run_must_name_checks_that_exist_and_at_least_one():
    ok = build(
        **with_mock(
            {
                "bad": [
                    {"name": "a", "fails": ["max_messages"], "script": {}},
                    {"name": "b", "fails": ["ends-one"], "script": {}},
                ]
            }
        )
    )
    assert [b.name for b in ok.mock.bad] == ["a", "b"]
    refuses(
        "must fail 'nope', which is no check here",
        **with_mock({"bad": [{"name": "a", "fails": ["nope"], "script": {}}]}),
    )
    refuses("at least 1", **with_mock({"bad": [{"name": "a", "fails": [], "script": {}}]}))


def test_bad_runs_need_distinct_names():
    refuses(
        "distinct names",
        **with_mock(
            {
                "bad": [
                    {"name": "a", "fails": ["max_messages"], "script": {}},
                    {"name": "a", "fails": ["max_messages"], "script": {}},
                ]
            }
        ),
    )


# ---- reading files ---------------------------------------------------------------------------------------
YAML = """\
id: from-yaml
title: From YAML
steps:
  - id: one
    message: hello
checks:
  - {kind: max_messages, limit: 1}
"""


def test_yaml_text_becomes_a_scenario():
    assert parse(YAML).id == "from-yaml"


def test_text_that_is_not_yaml_or_not_a_mapping_is_refused_by_name():
    with pytest.raises(ScenarioError, match=r"x\.yaml: not valid YAML"):
        parse("a: [unclosed", source="x.yaml")
    with pytest.raises(ScenarioError, match=r"x\.yaml: a scenario is a mapping.*list"):
        parse("- a\n- b\n", source="x.yaml")
    with pytest.raises(ScenarioError, match=r"x\.yaml: a scenario is a mapping.*NoneType"):
        parse("", source="x.yaml")


def test_a_mistake_is_reported_with_the_place_it_is_in():
    with pytest.raises(ScenarioError) as raised:
        parse(YAML.replace("limit: 1", "limit: -1"), source="x.yaml")
    assert "x.yaml: checks.0" in str(raised.value) and "limit" in str(raised.value)


def test_many_mistakes_are_summarised_not_dumped():
    data = "id: BAD\ntitle: t\nsteps: []\nchecks: []\nextra1: 1\nextra2: 2\nextra3: 3\nextra4: 4\n"
    with pytest.raises(ScenarioError) as raised:
        parse(data, source="x.yaml")
    assert "and " in str(raised.value) and "more" in str(raised.value)
    assert str(raised.value).count(";") <= 6


def test_yaml_cannot_build_python_objects():
    with pytest.raises(ScenarioError, match="not valid YAML"):
        parse("id: !!python/object/apply:os.system ['true']\n", source="x.yaml")


def test_a_file_is_read_as_utf8_and_its_size_is_limited(tmp_path):
    good = tmp_path / "ok.yaml"
    good.write_text(YAML.replace("hello", "你好"), encoding="utf-8")
    assert load_file(good).steps[0].message == "你好"
    big = tmp_path / "big.yaml"
    big.write_text(YAML + "# " + "x" * spec.MAX_FILE_BYTES)
    with pytest.raises(ScenarioError, match=r"big\.yaml: larger than 256 KiB"):
        load_file(big)
    bad = tmp_path / "bad.yaml"
    bad.write_bytes(b"id: \xff\xfe\n")
    with pytest.raises(ScenarioError, match=r"bad\.yaml: cannot be read"):
        load_file(bad)
    with pytest.raises(ScenarioError, match=r"missing\.yaml: cannot be read"):
        load_file(tmp_path / "missing.yaml")


def test_a_file_exactly_at_the_limit_is_still_read(tmp_path):
    file = tmp_path / "edge.yaml"
    padding = spec.MAX_FILE_BYTES - len((YAML + "#").encode()) - 1
    file.write_text(YAML + "#" + "x" * padding + "\n")
    assert file.stat().st_size == spec.MAX_FILE_BYTES
    assert load_file(file).id == "from-yaml"


# ---- the catalog -------------------------------------------------------------------------------------
def test_the_built_in_scenarios_are_all_valid_distinct_and_scripted():
    catalog = load_catalog()
    assert catalog.problems == []
    files = sorted(builtin_dir().glob("*.yaml"))
    assert len(catalog.scenarios) == len(files) >= 6
    assert set(catalog.origin.values()) == {"built-in"}
    for scenario in catalog:
        assert scenario.mock is not None, f"{scenario.id} has no scripts to test its checks with"
        assert scenario.mock.bad, (
            f"{scenario.id} has no bad run: its checks were never shown to fail"
        )
        assert scenario.about.strip(), f"{scenario.id} does not say what it is about"


def test_the_owners_own_scenarios_are_found_in_their_folder(paths):
    folder = user_dir(paths)
    folder.mkdir()
    (folder / "mine.yaml").write_text(YAML)
    (folder / "other.yml").write_text(YAML.replace("from-yaml", "from-yml"))
    (folder / "notes.txt").write_text("not a scenario")
    catalog = load_catalog(paths)
    assert catalog.problems == []
    assert catalog.origin["from-yaml"] == "evals/mine.yaml"
    assert catalog.origin["from-yml"] == "evals/other.yml"
    assert "notes" not in catalog.scenarios and len(catalog.scenarios) >= 8


def test_a_broken_file_is_reported_and_does_not_hide_the_others(paths):
    folder = user_dir(paths)
    folder.mkdir()
    (folder / "broken.yaml").write_text("id: x\n")
    (folder / "fine.yaml").write_text(YAML)
    catalog = load_catalog(paths)
    assert "from-yaml" in catalog.scenarios
    [problem] = catalog.problems
    assert problem.startswith("broken.yaml:")


def test_an_id_that_is_already_taken_is_refused_for_the_second_file_only(paths):
    taken = next(iter(load_catalog().scenarios))
    folder = user_dir(paths)
    folder.mkdir()
    (folder / "clash.yaml").write_text(YAML.replace("from-yaml", taken))
    catalog = load_catalog(paths)
    assert catalog.origin[taken] == "built-in"
    [problem] = catalog.problems
    assert "clash.yaml" in problem and "already used by built-in" in problem


def test_two_files_of_the_owner_with_one_id_keep_the_first(paths):
    folder = user_dir(paths)
    folder.mkdir()
    (folder / "a.yaml").write_text(YAML)
    (folder / "b.yaml").write_text(YAML)
    catalog = load_catalog(paths)
    assert catalog.origin["from-yaml"] == "evals/a.yaml"
    assert "b.yaml" in catalog.problems[0] and "evals/a.yaml" in catalog.problems[0]


def test_a_link_is_not_followed_out_of_the_folder(paths, tmp_path):
    folder = user_dir(paths)
    folder.mkdir()
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text(YAML)
    (folder / "link.yaml").symlink_to(outside)
    (folder / "folder.yaml").mkdir()
    catalog = load_catalog(paths)
    assert "from-yaml" not in catalog.scenarios and catalog.problems == []


def test_without_a_home_only_the_built_in_scenarios_exist():
    assert set(load_catalog(None).origin.values()) == {"built-in"}
    assert [s.id for s in load_catalog()] == [s.id for s in load_catalog(None)]


def test_a_catalog_can_be_iterated_in_the_order_it_was_read(paths):
    folder = user_dir(paths)
    folder.mkdir()
    (folder / "a.yaml").write_text(YAML)
    ids = [s.id for s in load_catalog(paths)]
    assert ids[-1] == "from-yaml" and ids[:-1] == sorted(ids[:-1])


def test_the_folder_may_be_missing_altogether(paths):
    assert not Path(user_dir(paths)).exists()
    assert load_catalog(paths).problems == []


def test_the_owners_scenarios_live_in_the_evals_folder_of_the_home(paths):
    assert user_dir(paths) == paths.home / "evals"
    assert builtin_dir().name == "scenarios" and builtin_dir().is_dir()


def test_a_scripted_message_is_normal_unless_it_says_otherwise():
    action = spec.Action(send="hello")
    assert action.urgency == "normal"
    assert spec.Action(send="fire", urgency="urgent").urgency == "urgent"


def _with_extra_keys(count: int) -> str:
    lines = ["id: demo", "title: Demo", "steps: [{id: one, message: hi}]"]
    lines.append("checks: [{kind: max_messages, limit: 1}]")
    lines.extend(f"extra{n}: 1" for n in range(count))
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("count", "tail"), [(1, ""), (6, ""), (7, "; and 1 more"), (9, "; and 3 more")]
)
def test_only_the_first_six_problems_of_a_file_are_listed(count, tail):
    with pytest.raises(ScenarioError) as raised:
        parse(_with_extra_keys(count), source="x.yaml")
    message = str(raised.value)
    assert message.count("Extra inputs are not permitted") == min(count, 6)
    assert message.endswith(tail) if tail else "more" not in message
