import pytest
from pydantic import ValidationError

from personal_agent.config import PolicyConfig
from personal_agent.security.policy import HARMLESS, Policy, ToolCall, Verdict, matches
from personal_agent.tools.base import ALL_PROFILES, FULL, SCHEDULED, UNTRUSTED, Category

RISKY = [Category.WRITE, Category.EXEC, Category.NET_WRITE, Category.SCHEDULE, Category.CONFIG]


def call(
    tool="shell",
    category=Category.EXEC,
    detail="",
    profile=FULL,
    tainted=False,
    allowed=ALL_PROFILES,
    views=(),
):
    return ToolCall(tool, category, detail, profile, tainted, frozenset(allowed), tuple(views))


def policy(**kw) -> Policy:
    if kw.get("mode") == "autonomous":
        kw["i_understand_autonomous_risk"] = True
    return Policy(PolicyConfig(**kw))


def verdict(p: Policy, **kw) -> Verdict:
    return p.evaluate(call(**kw)).verdict


@pytest.mark.parametrize("category", sorted(HARMLESS))
def test_harmless_actions_never_need_approval(category):
    assert verdict(policy(), category=category) is Verdict.ALLOW


@pytest.mark.parametrize("category", RISKY)
def test_supervised_mode_asks_before_anything_that_changes_the_world(category):
    d = policy().evaluate(call(category=category))
    assert d.verdict is Verdict.ASK and d.layer == "mode"


def test_the_default_mode_is_supervised():
    assert PolicyConfig().mode == "supervised"


def test_deny_rules_beat_allow_rules_and_every_mode():
    for mode in ("supervised", "auto", "autonomous"):
        p = policy(mode=mode, allow=["shell"], deny=["shell:rm *"])
        d = p.evaluate(call(detail="rm -rf ~"))
        assert d.verdict is Verdict.DENY and d.layer == "deny_rule"
        assert verdict(p, detail="ls") is not Verdict.DENY


def test_a_tool_outside_the_run_profile_is_refused():
    d = policy(mode="autonomous").evaluate(call(profile=UNTRUSTED, allowed={FULL, SCHEDULED}))
    assert d.verdict is Verdict.DENY and d.layer == "profile"


def test_read_only_mode_refuses_everything_but_harmless_actions():
    p = policy(mode="read_only", allow=["*"])
    assert all(verdict(p, category=c) is Verdict.DENY for c in RISKY)
    assert all(verdict(p, category=c) is Verdict.ALLOW for c in HARMLESS)


def test_always_ask_survives_allow_rules_and_autonomous_mode():
    for mode in ("supervised", "auto", "autonomous"):
        p = policy(mode=mode, allow=["shell"], always_ask=["shell:git push*"])
        assert verdict(p, detail="git push origin main") is Verdict.ASK
        assert verdict(p, detail="git status") is Verdict.ALLOW


def test_allow_rules_pre_approve_matching_calls_only():
    p = policy(allow=["shell:git status*", "files_*"])
    assert verdict(p, detail="git status --short") is Verdict.ALLOW
    assert verdict(p, detail="git push") is Verdict.ASK
    assert verdict(p, tool="files_write", category=Category.WRITE) is Verdict.ALLOW
    assert verdict(p, tool="files", category=Category.WRITE) is Verdict.ASK


def test_a_run_that_read_untrusted_content_asks_even_for_pre_approved_actions():
    p = policy(allow=["shell:git status*"])
    d = p.evaluate(call(detail="git status", tainted=True))
    assert d.verdict is Verdict.ASK and d.layer == "taint"


def test_allow_when_tainted_is_the_explicit_way_to_keep_a_rule_in_tainted_runs():
    p = policy(allow_when_tainted=["shell:git status*"])
    assert verdict(p, detail="git status", tainted=True) is Verdict.ALLOW
    assert verdict(p, detail="git push", tainted=True) is Verdict.ASK


def test_taint_does_not_make_harmless_actions_ask():
    assert verdict(policy(), category=Category.READ, tainted=True) is Verdict.ALLOW
    assert verdict(policy(), category=Category.MEMORY, tainted=True) is Verdict.ALLOW


def test_auto_mode_sends_risky_calls_to_the_reviewer_unless_tainted():
    p = policy(mode="auto")
    assert verdict(p) is Verdict.REVIEW
    assert verdict(p, category=Category.READ) is Verdict.ALLOW
    tainted = p.evaluate(call(tainted=True))
    assert tainted.verdict is Verdict.ASK and tainted.layer == "taint"


def test_autonomous_mode_allows_without_asking_even_when_tainted():
    p = policy(mode="autonomous")
    assert verdict(p, tainted=True) is Verdict.ALLOW


def test_autonomous_mode_needs_an_explicit_acknowledgement():
    with pytest.raises(ValidationError, match="autonomous"):
        PolicyConfig(mode="autonomous")
    assert PolicyConfig(mode="autonomous", i_understand_autonomous_risk=True).mode == "autonomous"


@pytest.mark.parametrize(
    ("rule", "tool", "detail", "expected"),
    [
        ("shell", "shell", "anything", True),
        ("shell", "shell2", "", False),
        ("mcp_*", "mcp_github_create_issue", "", True),
        ("shell:git status*", "shell", "git status -s", True),
        ("shell:git status*", "shell", "git push", False),
        ("shell:git status*", "shell", "", False),
        ("shell:*", "shell", "", True),
        ("web_fetch:https://example.com/*", "web_fetch", "https://example.com/a", True),
        (
            "web_fetch:https://example.com/*",
            "web_fetch",
            "https://evil.test/https://example.com/",
            False,
        ),
        ("Shell", "shell", "", False),
    ],
)
def test_rule_matching(rule, tool, detail, expected):
    assert matches(rule, tool, detail) is expected


def test_rules_that_stop_a_call_are_tried_on_every_view_of_it():
    chained = dict(
        detail="[compound] cd b && rm -rf .", views=("cd b && rm -rf .", "cd b", "rm -rf .")
    )
    deny = policy(allow=["shell"], deny=["shell:rm -rf *"]).evaluate(call(**chained))
    assert (deny.verdict, deny.layer) == (Verdict.DENY, "deny_rule")
    ask = policy(mode="autonomous", always_ask=["shell:rm *"]).evaluate(call(**chained))
    assert (ask.verdict, ask.layer) == (Verdict.ASK, "always_ask")


def test_rules_that_let_a_call_through_are_tried_on_its_detail_alone():
    """A rule for `git status*` must not be satisfied by one command inside a longer line."""
    views = ("git status", "rm -rf ~")
    detail = "[compound] git status; rm -rf ~"
    assert verdict(policy(allow=["shell:git status*"]), detail=detail, views=views) is Verdict.ASK
    assert verdict(policy(allow=["shell:git status*"]), detail="git status") is Verdict.ALLOW
    tainted = policy(allow_when_tainted=["shell:git status*"])
    assert verdict(tainted, detail=detail, views=views, tainted=True) is Verdict.ASK
