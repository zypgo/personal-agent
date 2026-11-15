"""Channels that plugins provide: what the plugin API accepts, and that a faulty one costs only itself."""

from __future__ import annotations

import pytest
from agents.testing import ScriptedModel, assistant_message

from personal_agent.channels.ingress import Ingress
from personal_agent.channels.registry import build_channels
from personal_agent.config import Config, PluginsConfig, WebConfig
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.plugins.registry import (
    CHANNEL_NAME,
    RESERVED_CHANNELS,
    ChannelSpec,
    PluginApi,
    PluginError,
    load_plugins,
)
from personal_agent.tools.base import ToolRegistry
from tooling import drain


class Sink:
    """What a plugin's channel object looks like: a name and `send`."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[str] = []

    async def send(self, message) -> None:
        self.sent.append(message.text)


class Listening(Sink):
    async def run(self, daemon) -> None: ...


class EntryPoint:
    value = "fake_package:register"

    def __init__(self, name, target):
        self.name, self._target = name, target

    def load(self):
        return self._target


def api(paths, channels: dict | None = None, name: str = "demo") -> PluginApi:
    return PluginApi(name, ToolRegistry(), paths, channels=channels if channels is not None else {})


# ---- what the plugin API accepts --------------------------------------------------------------
def test_a_channel_is_recorded_with_its_factory_its_push_flag_and_its_plugin(paths):
    channels: dict[str, ChannelSpec] = {}
    plugin = api(paths, channels, "signal-bridge")

    def factory(app):
        return Sink("signal")

    assert plugin.add_channel("signal", factory) == "signal"
    spec = channels["signal"]
    assert spec == ChannelSpec("signal", factory, True, "signal-bridge")
    assert plugin.added_channels == ["signal"]


def test_a_channel_that_cannot_ping_says_so(paths):
    channels: dict[str, ChannelSpec] = {}
    api(paths, channels).add_channel("board", lambda app: Sink("board"), push=False)
    assert channels["board"].push is False


@pytest.mark.parametrize(
    "name", ["", "a", "Signal", "1signal", "has space", "under_score", "x" * 25, "sig/nal", "señal"]
)
def test_a_channel_name_is_two_to_twenty_four_lowercase_letters_digits_and_hyphens(paths, name):
    plugin = api(paths)
    with pytest.raises(PluginError, match="2-24 lowercase"):
        plugin.add_channel(name, lambda app: Sink(name))
    assert plugin.added_channels == []


@pytest.mark.parametrize("name", ["signal", "ab", "my-channel-2", "x" * 24])
def test_ordinary_names_are_accepted(paths, name):
    assert CHANNEL_NAME.fullmatch(name)
    assert api(paths).add_channel(name, lambda app: Sink(name)) == name


@pytest.mark.parametrize("name", sorted(RESERVED_CHANNELS))
def test_a_plugin_cannot_take_a_built_in_name(paths, name):
    plugin = api(paths)
    with pytest.raises(PluginError, match="built-in channel name"):
        plugin.add_channel(name, lambda app: Sink(name))
    assert plugin.added_channels == []


def test_the_built_in_names_are_exactly_the_ones_the_agent_uses_itself():
    assert {"cli", "web", "telegram", "webhooks", "auto"} == RESERVED_CHANNELS


def test_two_plugins_cannot_provide_the_same_channel(paths):
    channels: dict[str, ChannelSpec] = {}
    api(paths, channels, "first").add_channel("signal", lambda app: Sink("signal"))
    second = api(paths, channels, "second")
    with pytest.raises(PluginError, match="another plugin already provides"):
        second.add_channel("signal", lambda app: Sink("signal"))
    assert channels["signal"].plugin == "first"  # the first one keeps it
    assert second.added_channels == []


def test_the_same_plugin_cannot_register_a_name_twice(paths):
    plugin = api(paths)
    plugin.add_channel("signal", lambda app: Sink("signal"))
    with pytest.raises(PluginError, match="already provides"):
        plugin.add_channel("signal", lambda app: Sink("signal"))


@pytest.mark.parametrize("factory", [None, "Sink", 42, Sink("x")])
def test_the_factory_must_be_callable(paths, factory):
    plugin = api(paths)
    with pytest.raises(PluginError, match="function that builds the channel"):
        plugin.add_channel("signal", factory)  # type: ignore[arg-type]
    assert plugin.added_channels == []


def test_the_factory_is_not_called_when_the_channel_is_registered(paths):
    calls: list[object] = []

    def factory(app):
        calls.append(app)
        return Sink("signal")

    api(paths).add_channel("signal", factory)
    assert calls == []  # it is built when the daemon starts, with the app


def test_the_push_flag_is_taken_as_a_plain_yes_or_no(paths):
    channels: dict[str, ChannelSpec] = {}
    api(paths, channels).add_channel("signal", lambda app: Sink("signal"), push=0)  # type: ignore[arg-type]
    assert channels["signal"].push is False


# ---- loading plugins that offer channels -------------------------------------------------------
def test_a_loaded_plugin_reports_the_channels_it_added(paths):
    channels: dict[str, ChannelSpec] = {}

    def register(plugin: PluginApi) -> None:
        plugin.add_channel("signal", lambda app: Sink("signal"))
        plugin.add_channel("board", lambda app: Sink("board"), push=False)

    report = load_plugins(
        PluginsConfig(enabled=["demo"]),
        registry=ToolRegistry(),
        paths=paths,
        entry_points=[EntryPoint("demo", register)],
        channels=channels,
    )
    [info] = report
    assert info.status == "loaded" and info.channels == ("signal", "board")
    assert set(channels) == {"signal", "board"}


def test_a_plugin_that_fails_after_adding_a_channel_leaves_none_behind(paths):
    channels: dict[str, ChannelSpec] = {}

    def good(plugin: PluginApi) -> None:
        plugin.add_channel("good-one", lambda app: Sink("good-one"))

    def bad(plugin: PluginApi) -> None:
        plugin.add_channel("half-done", lambda app: Sink("half-done"))
        raise RuntimeError("could not finish")

    report = load_plugins(
        PluginsConfig(enabled=["bad", "good"]),
        registry=ToolRegistry(),
        paths=paths,
        entry_points=[EntryPoint("bad", bad), EntryPoint("good", good)],
        channels=channels,
    )
    by_name = {info.name: info for info in report}
    assert by_name["bad"].status == "failed" and "could not finish" in by_name["bad"].detail
    assert by_name["bad"].channels == ()
    assert by_name["good"].status == "loaded"
    assert set(channels) == {"good-one"}  # only the plugin that worked


def test_a_plugin_that_is_not_switched_on_offers_no_channel(paths):
    channels: dict[str, ChannelSpec] = {}
    report = load_plugins(
        PluginsConfig(enabled=[]),
        registry=ToolRegistry(),
        paths=paths,
        entry_points=[EntryPoint("demo", lambda plugin: plugin.add_channel("signal", Sink))],
        channels=channels,
    )
    assert channels == {} and report[0].status == "off"


def test_a_plugin_that_wants_a_name_another_one_has_fails_alone(paths):
    channels: dict[str, ChannelSpec] = {}

    def first(plugin: PluginApi) -> None:
        plugin.add_channel("signal", lambda app: Sink("signal"))

    def second(plugin: PluginApi) -> None:
        plugin.add_channel("other", lambda app: Sink("other"))
        plugin.add_channel("signal", lambda app: Sink("signal"))

    report = load_plugins(
        PluginsConfig(enabled=["first", "second"]),
        registry=ToolRegistry(),
        paths=paths,
        entry_points=[EntryPoint("first", first), EntryPoint("second", second)],
        channels=channels,
    )
    by_name = {info.name: info for info in report}
    assert by_name["first"].status == "loaded"
    assert by_name["second"].status == "failed" and "already provides" in by_name["second"].detail
    assert set(channels) == {"signal"} and channels["signal"].plugin == "first"


def test_loading_through_the_app_fills_app_plugin_channels(make_app):
    def register(plugin: PluginApi) -> None:
        plugin.add_channel("signal", lambda app: Sink("signal"))

    app = make_app(
        config=Config(plugins=PluginsConfig(enabled=["demo"])),
        plugin_entry_points=[EntryPoint("demo", register)],
    )
    assert set(app.plugin_channels) == {"signal"}
    assert app.plugin_channels["signal"].plugin == "demo"


# ---- building the channels the daemon runs -----------------------------------------------------
def with_channels(make_app, **specs: tuple) -> tuple:
    app = make_app()
    for name, (factory, push) in specs.items():
        app.plugin_channels[name] = ChannelSpec(name, factory, push, "demo")
    return app, build_channels(app)


def test_a_plugin_channel_is_built_with_the_app_and_joins_the_others(make_app):
    seen = []

    def factory(app):
        seen.append(app)
        return Sink("signal")

    app, chosen = with_channels(make_app, signal=(factory, True))
    assert seen == [app]
    assert set(chosen.channels) == {"signal"} and chosen.problems == []
    assert chosen.services == []  # it only sends: nothing to supervise


def test_a_plugin_channel_that_listens_is_supervised(make_app):
    _app, chosen = with_channels(make_app, signal=(lambda app: Listening("signal"), True))
    assert chosen.services == [chosen.channels["signal"]]


def test_a_factory_that_raises_costs_only_its_own_channel(make_app):
    def broken(app):
        raise RuntimeError("no network driver")

    app = make_app(config=Config(web=WebConfig(enabled=True)))
    app.plugin_channels["signal"] = ChannelSpec("signal", broken, True, "demo")
    app.plugin_channels["board"] = ChannelSpec("board", lambda a: Sink("board"), False, "demo")
    chosen = build_channels(app)
    assert set(chosen.channels) == {"web", "board"}  # the page and the other plugin carry on
    [problem] = chosen.problems
    assert problem.startswith("signal: RuntimeError: no network driver")


def test_a_channel_whose_name_differs_from_the_one_it_registered_is_refused(make_app):
    _app, chosen = with_channels(make_app, signal=(lambda app: Sink("something-else"), True))
    assert chosen.channels == {}
    [problem] = chosen.problems
    assert problem.startswith("signal:") and "'something-else'" in problem


def test_an_object_without_a_name_is_refused(make_app):
    _app, chosen = with_channels(make_app, signal=(lambda app: object(), True))
    assert chosen.channels == {}
    assert "None" in chosen.problems[0]


def test_a_plugin_channel_cannot_replace_a_built_in_one_that_is_running(make_app):
    # The API refuses reserved names, but the registry must hold even if one got through.
    app = make_app(config=Config(web=WebConfig(enabled=True)))
    impostor = Sink("web")
    app.plugin_channels["web"] = ChannelSpec("web", lambda a: impostor, True, "demo")
    chosen = build_channels(app)
    assert chosen.channels["web"] is not impostor
    assert chosen.problems and "'web'" in chosen.problems[0]


def test_a_long_error_is_cut_short_in_the_report(make_app):
    def broken(app):
        raise RuntimeError("x" * 2000)

    _app, chosen = with_channels(make_app, signal=(broken, True))
    assert len(chosen.problems[0]) <= 300


# ---- a plugin channel in use -------------------------------------------------------------------
async def test_the_owner_writes_through_a_plugin_channel_and_the_answer_comes_back_there(make_app):
    app = make_app(ScriptedModel([[assistant_message("hello from the agent")]]))
    sink = Sink("signal")
    app.plugin_channels["signal"] = ChannelSpec("signal", lambda a: sink, True, "demo")
    chosen = build_channels(app)

    result = await Ingress(app).owner_message(channel="signal", text="hi there")
    assert result.status == "queued"
    await drain(app)
    report = await Dispatcher(app, chosen.channels).deliver_once()

    assert sink.sent == ["hello from the agent"] and len(report.sent) == 1
    assert app.transcript.last_in_channel(among={"signal"}) == "signal"


async def test_unprompted_news_goes_to_a_plugin_channel_that_can_ping_after_the_owner_used_it(
    make_app,
):
    app = make_app()
    app.plugin_channels["signal"] = ChannelSpec("signal", lambda a: Sink("signal"), True, "demo")
    assert app.notify_channel() == "cli"  # they never wrote from it
    app.transcript.add_in("signal", "hello")
    assert app.notify_channel() == "signal"
