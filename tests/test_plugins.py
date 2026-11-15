"""Python plugins: what loads, what is refused, and that their tools obey the same rules as all others."""

from __future__ import annotations

import sys

import pytest
from agents import function_tool
from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import invoke, owner_message, webhook
from personal_agent.config import Config
from personal_agent.doctor import capability_checks
from personal_agent.plugins.registry import PluginApi, PluginError, installed, load_plugins
from personal_agent.tools.base import ToolRegistry
from tooling import drain, outputs


class EntryPoint:
    """What `importlib.metadata` hands out: a name, and `load()` returning the register function."""

    value = "fake_package:register"

    def __init__(self, name, target):
        self.name, self._target = name, target

    def load(self):
        if isinstance(self._target, BaseException):
            raise self._target
        return self._target


@function_tool(name_override="lookup", description_override="Look something up.")
def lookup(query: str) -> str:
    return f"found {query}"


@function_tool(name_override="save", description_override="Save something.")
def save(text: str) -> str:
    return f"saved {text}"


def register_demo(api: PluginApi) -> None:
    api.add_tool(lookup, category="read", taints=True)
    api.add_tool(save, category="write", detail=lambda args: args.get("text", ""))


def cfg(*enabled: str, **settings) -> Config:
    return Config.model_validate({"plugins": {"enabled": list(enabled), "settings": settings}})


# ---- loading ---------------------------------------------------------------------------------------------
def test_an_enabled_plugin_adds_its_tools_under_its_own_prefix(paths):
    registry = ToolRegistry()
    report = load_plugins(
        cfg("demo").plugins,
        registry=registry,
        paths=paths,
        entry_points=[EntryPoint("demo", register_demo)],
    )
    [info] = report
    assert (info.name, info.status) == ("demo", "loaded")
    assert info.tools == ("plugin__demo__lookup", "plugin__demo__save")
    read, write = (registry.get(n) for n in info.tools)
    assert (read.category.value, read.taints, read.source) == ("read", True, "plugin:demo")
    assert write.category.value == "write" and write.detail_of({"text": "x"}) == "x"
    assert read.tool.name == "plugin__demo__lookup"  # the model sees the public name
    assert registry.get("lookup") is None  # nothing is registered under the bare name


def test_plugin_tools_are_for_runs_the_owner_is_part_of_and_never_for_outside_content(paths):
    registry = ToolRegistry()
    load_plugins(
        cfg("demo").plugins,
        registry=registry,
        paths=paths,
        entry_points=[EntryPoint("demo", register_demo)],
    )
    tool = registry.get("plugin__demo__lookup")
    assert tool.profiles == frozenset({"full", "scheduled"})


def test_installing_is_not_enabling(paths):
    called = []
    registry = ToolRegistry()
    report = load_plugins(
        cfg().plugins,
        registry=registry,
        paths=paths,
        entry_points=[EntryPoint("demo", lambda api: called.append(api))],
    )
    assert called == [] and registry.names() == []
    assert [(i.name, i.status) for i in report] == [("demo", "off")]


def test_an_enabled_plugin_that_is_not_installed_is_reported(paths):
    [info] = load_plugins(
        cfg("ghost").plugins, registry=ToolRegistry(), paths=paths, entry_points=[]
    )
    assert (info.status, "no installed package provides" in info.detail) == ("missing", True)


def test_a_plugin_that_crashes_costs_only_itself_and_leaves_none_of_its_tools_behind(paths):
    def half_done(api: PluginApi) -> None:
        api.add_tool(lookup, category="read")
        raise RuntimeError("could not reach the weather service")

    registry = ToolRegistry()
    report = load_plugins(
        cfg("broken", "demo").plugins,
        registry=registry,
        paths=paths,
        entry_points=[EntryPoint("broken", half_done), EntryPoint("demo", register_demo)],
    )
    by_name = {i.name: i for i in report}
    assert by_name["broken"].status == "failed"
    assert "RuntimeError: could not reach the weather service" in by_name["broken"].detail
    assert by_name["demo"].status == "loaded"
    assert registry.names() == ["plugin__demo__lookup", "plugin__demo__save"]


@pytest.mark.parametrize(
    "failure", [ImportError("no module named x"), SystemExit(3), ValueError("bad")]
)
def test_whatever_a_plugin_raises_while_loading_is_contained(paths, failure):
    [info] = load_plugins(
        cfg("p").plugins,
        registry=ToolRegistry(),
        paths=paths,
        entry_points=[EntryPoint("p", failure)],
    )
    assert info.status == "failed"


def test_two_plugins_cannot_clash_because_every_name_carries_the_plugin_prefix(paths):
    registry = ToolRegistry()
    report = load_plugins(
        cfg("one", "two").plugins,
        registry=registry,
        paths=paths,
        entry_points=[EntryPoint("one", register_demo), EntryPoint("two", register_demo)],
    )
    assert [i.status for i in report] == ["loaded", "loaded"]  # different prefixes: no clash
    again = PluginApi("one", registry, paths)
    with pytest.raises(ValueError, match="already registered"):
        again.add_tool(lookup, category="read")


@pytest.mark.parametrize(
    ("call", "why"),
    [
        (lambda api: api.add_tool(lookup, category="teleport"), "unknown category"),
        (lambda api: api.add_tool("not a tool", category="read"), "takes a FunctionTool"),
    ],
)
def test_misuse_of_the_api_is_explained(paths, call, why):
    api = PluginApi("p", ToolRegistry(), paths)
    with pytest.raises(PluginError, match=why):
        call(api)


def test_a_very_long_tool_name_is_refused_rather_than_cut(paths):
    long = function_tool(lambda: "x", name_override="n" * 60)
    with pytest.raises(PluginError, match="64 characters"):
        PluginApi("p", ToolRegistry(), paths).add_tool(long, category="read")


@pytest.mark.parametrize("name", ["../x", "a b", "1x", "x" * 30])
def test_a_badly_named_plugin_is_not_loaded(paths, name):
    [info] = load_plugins(
        cfg(name).plugins,
        registry=ToolRegistry(),
        paths=paths,
        entry_points=[EntryPoint(name, register_demo)],
    )
    assert info.status == "failed"


def test_a_plugin_reads_its_own_settings_as_a_copy_and_gets_a_private_folder(paths):
    seen = {}

    def register(api: PluginApi) -> None:
        seen["settings"] = api.settings
        api.settings["units"] = "tampered"
        seen["again"] = api.settings
        seen["dir"] = api.data_dir

    load_plugins(
        cfg("demo", demo={"units": "metric"}).plugins,
        registry=ToolRegistry(),
        paths=paths,
        entry_points=[EntryPoint("demo", register)],
    )
    assert seen["settings"] == {"units": "metric"} and seen["again"] == {"units": "metric"}
    assert (
        seen["dir"] == paths.home / "plugins" / "demo"
        and seen["dir"].stat().st_mode & 0o777 == 0o700
    )


def test_a_package_that_declares_the_entry_point_is_found_and_loaded(paths, tmp_path, monkeypatch):
    """The real path: package metadata on sys.path, no stand-in for importlib.metadata."""
    site = tmp_path / "site"
    (site / "pa_demo_plugin-1.0.dist-info").mkdir(parents=True)
    (site / "pa_demo_plugin.py").write_text(
        "from agents import function_tool\n"
        "@function_tool(name_override='hello', description_override='Say hello.')\n"
        "def hello() -> str:\n    return 'hello'\n"
        "def register(api):\n    api.add_tool(hello, category='read')\n"
    )
    info = site / "pa_demo_plugin-1.0.dist-info"
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: pa-demo-plugin\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text(
        "[personal_agent.plugins]\ndemo = pa_demo_plugin:register\n"
    )
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delitem(sys.modules, "pa_demo_plugin", raising=False)

    assert installed()["demo"].value == "pa_demo_plugin:register"
    registry = ToolRegistry()
    [report] = load_plugins(cfg("demo").plugins, registry=registry, paths=paths)
    assert (report.status, report.tools) == ("loaded", ("plugin__demo__hello",))
    sys.modules.pop("pa_demo_plugin", None)


# ---- inside runs -----------------------------------------------------------------------------------------
async def test_plugin_tools_go_through_the_guard_like_every_other_tool(make_app):
    model = ScriptedModel(
        [
            [function_call("plugin__demo__lookup", {"query": "rain"}, call_id="c1")],
            [function_call("plugin__demo__save", {"text": "rain"}, call_id="c2")],
            [assistant_message("done")],
        ]
    )
    app = make_app(
        model, config=cfg("demo"), plugin_entry_points=[EntryPoint("demo", register_demo)]
    )
    app.inbox.add(owner_message("what about rain?"))

    [result] = await drain(app)
    # The read ran and its text is outside content, so the write that followed asked first.
    assert result.outcome.status == "awaiting_approval"
    assert "found rain" in outputs(app)["c1"]
    rows = {r.call_id: r for r in app.audit.recent()}
    assert (rows["c1"].verdict, rows["c1"].category) == ("allow", "read")
    assert rows["c2"].verdict == "ask" and rows["c2"].tool == "plugin__demo__save"


async def test_runs_woken_by_outside_content_are_not_given_plugin_tools(make_app):
    model = ScriptedModel([[assistant_message("ok")]])
    app = make_app(
        model, config=cfg("demo"), plugin_entry_points=[EntryPoint("demo", register_demo)]
    )
    app.inbox.add(webhook("github", {"action": "opened"}))
    await drain(app)
    assert not [t.name for t in model.calls[0].tools if t.name.startswith("plugin__")]


async def test_a_deny_rule_hides_a_plugin_tool_from_the_model(make_app):
    model = ScriptedModel([[assistant_message("ok")]])
    config = Config.model_validate(
        {"plugins": {"enabled": ["demo"]}, "policy": {"deny": ["plugin__demo__save"]}}
    )
    app = make_app(model, config=config, plugin_entry_points=[EntryPoint("demo", register_demo)])
    app.inbox.add(owner_message("hi"))
    await drain(app)
    assert [t.name for t in model.calls[0].tools if t.name.startswith("plugin__")] == [
        "plugin__demo__lookup"
    ]


# ---- pa plugins and pa doctor ----------------------------------------------------------------------------
@pytest.fixture
def wire(make_app, monkeypatch):
    from personal_agent import cli

    def go(config: Config, *entry_points):
        app = make_app(config=config, plugin_entry_points=list(entry_points))
        monkeypatch.setattr(app, "close", lambda: None)
        monkeypatch.setattr(cli, "_build", lambda: app)
        return app

    return go


def test_the_command_lists_what_loaded_what_failed_and_what_is_off(wire):
    wire(
        cfg("demo", "broken", "ghost"),
        EntryPoint("demo", register_demo),
        EntryPoint("broken", RuntimeError("boom")),
        EntryPoint("spare", register_demo),
    )
    out = " ".join(invoke("plugins").output.split())
    assert "demo loaded plugin__demo__lookup, plugin__demo__save" in out
    assert "broken failed RuntimeError: boom" in out
    assert "ghost missing no installed package provides a plugin called 'ghost'" in out
    assert "spare off installed, not listed in [plugins] enabled" in out


def test_without_plugins_the_command_says_how_to_add_one(wire):
    wire(cfg())
    out = " ".join(invoke("plugins").output.split())
    assert "No plugins installed" in out and "personal_agent.plugins" in out


def test_doctor_notices_an_enabled_plugin_that_is_not_installed(wire):
    app = wire(cfg())
    app.paths.config_file.write_text('[plugins]\nenabled = ["ghost"]\n', encoding="utf-8")
    check = {c.name: c for c in capability_checks(app.paths)}["plugin ghost"]
    assert check.status == "warn" and "no installed package provides it" in check.detail


def test_the_hint_for_no_plugins_shows_the_section_name_literally(wire):
    wire(cfg())
    assert "`[plugins] enabled`" in " ".join(invoke("plugins").output.split())
