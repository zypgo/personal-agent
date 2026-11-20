"""The page's own files: all there, small enough to read, obeying the page's own security rules, and
safe from whatever the agent happens to be called. The JavaScript is exercised by Node's test runner
(`tests/js`), which this file runs when Node is installed."""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from personal_agent.channels.web.api import STATIC_DIR, load_assets
from personal_agent.channels.web.security import CSP

ROOT = Path(__file__).resolve().parent.parent
HOSTILE = "\"><script>alert(1)</script>&'\\ \u00e9\u4e2d {{version}} \n"


@pytest.fixture(scope="module")
def assets():
    found, version = load_assets()
    return found, version


def url_to_name(url: str) -> str | None:
    """Which asset a root-relative URL asks for, the way the server routes them."""
    path = url.split("#")[0].split("?")[0]
    if path == "/workspace":
        return "workbench.html"
    if path == "/":
        return "index.html"
    if path in ("/sw.js", "/manifest.webmanifest"):
        return path[1:]
    if path.startswith("/static/"):
        return path[len("/static/") :]
    return None


class Page(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.title = ""
        self._in_title = False
        self.script_text = ""
        self._in_script = False
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._in_title = tag == "title"
        self._in_script = tag == "script"

    def handle_endtag(self, tag):
        self._in_title = self._in_script = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_script:
            self.script_text += data

    def named(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags if name == tag]


def page_of(assets):
    return Page(assets[0]["index.html"].body.decode())


# ---- the look is something the page promises, so it is checked like anything else -----------------
def stylesheet(assets, name: str) -> str:
    return assets[0][name].body.decode()


def rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(character * 2 for character in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def luminance(colour: tuple[int, int, int]) -> float:
    def channel(level: int) -> float:
        level /= 255
        return level / 12.92 if level <= 0.03928 else ((level + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(level) for level in colour)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def palette(tokens: str) -> dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]]:
    """Every colour token as (light, dark), read from the stylesheet the pages actually load."""
    colours: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {}
    for name, light, dark in re.findall(
        r"--([\w-]+): light-dark\((#[0-9a-fA-F]{3,6}), (#[0-9a-fA-F]{3,6})\);", tokens
    ):
        colours[name] = (rgb(light), rgb(dark))
    return colours


READABLE_TEXT = [  # text colour, what it is set on: ordinary text needs 4.5 to 1
    ("ink", "canvas"),
    ("ink", "surface"),
    ("ink", "surface-2"),
    ("ink-2", "canvas"),
    ("ink-2", "surface"),
    ("ink-2", "surface-2"),
    ("pencil", "canvas"),
    ("pencil", "surface"),
    ("pencil", "surface-2"),
    ("accent", "canvas"),
    ("accent", "surface"),
    ("accent-ink", "accent"),
    ("danger", "surface"),
    ("danger", "surface-2"),
    ("ok", "surface"),
    ("warn", "warn-bg"),
]
TINTS = ["pencil", "accent", "tint-knowledge", "tint-agents", "tint-spaces", "tint-devices"]


@pytest.mark.parametrize("theme", [0, 1], ids=["light", "dark"])
def test_the_palette_is_readable_in_both_themes(assets, theme):
    colours = palette(stylesheet(assets, "tokens.css"))
    for text, ground in READABLE_TEXT:
        ratio = contrast(colours[text][theme], colours[ground][theme])
        assert ratio >= 4.5, f"{text} on {ground} is {ratio:.2f} to 1"
    for ground in ("canvas", "surface", "surface-2"):  # placeholders and timestamps
        ratio = contrast(colours["ink-3"][theme], colours[ground][theme])
        assert ratio >= 4.0, f"ink-3 on {ground} is {ratio:.2f} to 1"
    for tint in TINTS:  # a white (or, at night, near-black) icon on a coloured tile
        ratio = contrast(colours["tint-ink"][theme], colours[tint][theme])
        assert ratio >= 3.0, f"the icon on {tint} is {ratio:.2f} to 1"


def test_a_browser_without_light_dark_gets_exactly_the_light_theme(assets):
    tokens = stylesheet(assets, "tokens.css")
    colours = palette(tokens)
    plain = dict(
        re.findall(
            r"^  --([\w-]+): (#[0-9a-fA-F]{3,6});", tokens[: tokens.index("@supports")], re.M
        )
    )
    assert plain, "the fallback palette is missing"
    for name, (light, _dark) in colours.items():
        if name in plain:
            assert rgb(plain[name]) == light, (
                f"--{name} falls back to a colour the light theme does not use"
            )


@pytest.mark.parametrize("theme", [0, 1], ids=["light", "dark"])
def test_text_set_in_a_places_tint_stays_readable(assets, theme):
    colours = palette(stylesheet(assets, "tokens.css"))
    mixes = {
        int(percent)
        for name in ("workbench.css", "orchestration.css")
        for percent in re.findall(
            r"color-mix\(in srgb, var\(--(?:agent-)?tint\) (\d+)%, var\(--ink\)\)",
            stylesheet(assets, name),
        )
    }
    assert mixes, (
        "titles in a place's colour are expected to be pulled toward the ink for legibility"
    )
    for percent in mixes:
        weight = percent / 100
        for tint in TINTS:
            text = tuple(
                round(own * weight + ink * (1 - weight))
                for own, ink in zip(colours[tint][theme], colours["ink"][theme], strict=True)
            )
            for ground in ("canvas", "surface"):
                ratio = contrast(text, colours[ground][theme])  # type: ignore[arg-type]
                assert ratio >= 4.5, f"{tint} text at {percent}% on {ground} is {ratio:.2f} to 1"


@pytest.mark.parametrize("name", ["index.html", "workbench.html"])
def test_every_page_starts_from_the_shared_tokens(assets, name):
    links = [
        attrs["href"] or ""
        for attrs in Page(assets[0][name].body.decode()).named("link")
        if attrs.get("rel") == "stylesheet"
    ]
    assert links and links[0].split("?")[0] == "/static/tokens.css", links


def test_motion_is_never_forced_on_people_who_asked_for_less(assets):
    tokens = stylesheet(assets, "tokens.css")
    reduced = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{\s*\*,\s*\*::before,\s*\*::after \{(.*?)\}",
        tokens,
        re.S,
    )
    assert reduced, "one rule has to slow every animation and transition down for those who ask"
    for declaration in (
        "animation-duration: 0.001ms !important",
        "transition-duration: 0.001ms !important",
    ):
        assert declaration in reduced.group(1)
    for name, asset in assets[0].items():
        if not name.endswith(".css"):
            continue
        text = asset.body.decode()
        for match in re.finditer(r"@view-transition", text):
            before = text[: match.start()]
            assert "(prefers-reduced-motion: no-preference)" in before[before.rindex("@media") :], (
                f"{name}: cross-page transitions are only for people who welcome motion"
            )


def test_the_narrow_drawer_keeps_horizontal_drags_for_itself(assets):
    shell = stylesheet(assets, "product-shell.css")
    narrow = re.sub(r"/\*.*?\*/", "", shell[shell.index("@media (max-width: 820px)") :], flags=re.S)
    rule = re.search(r"([^{}]+)\{\s*touch-action: pan-y pinch-zoom;\s*\}", narrow)
    assert rule, (
        "without this the browser claims the horizontal drag and the swipe never reaches the drawer"
    )
    selectors = {selector.strip() for selector in rule.group(1).split(",")}
    # Opening needs the page's own surfaces to stay with the drawer; closing needs the drawer and the
    # veil over the page too (a swipe closed was once claimed by the browser because they were missing).
    assert {
        ".product-main",
        ".log",
        ".shell",
        ".product-drawer",
        ".product-drawer-overlay",
    } <= selectors


# ---- everything it needs is there ---------------------------------------------------------------
def test_the_page_is_made_of_exactly_the_expected_files(assets):
    assert sorted(assets[0]) == [
        "activity.js",
        "api.js",
        "app.js",
        "apple-touch-icon.png",
        "assistant.css",
        "assistant.js",
        "audio-metadata.js",
        "format.js",
        "icon-192.png",
        "icon-512.png",
        "icon-maskable-512.png",
        "icon.svg",
        "index.html",
        "manifest.webmanifest",
        "markdown-parser.js",
        "md.js",
        "media.js",
        "orchestration.css",
        "orchestration.js",
        "product-shell.css",
        "product-shell.js",
        "query.js",
        "sheet.js",
        "store.js",
        "style.css",
        "sw.js",
        "tokens.css",
        "workbench.css",
        "workbench.html",
        "workbench.js",
    ]


def test_every_file_the_page_asks_for_is_served(assets):
    found, _ = assets
    page = page_of(assets)
    wanted = [a["href"] for a in page.named("link") if a.get("href")]
    wanted += [a["src"] for a in page.named("script") if a.get("src")]
    assert len(wanted) >= 5
    for url in wanted:
        assert url.startswith("/"), f"{url} is not on this origin"
        assert url_to_name(url) in found, f"{url} is not served"


def test_every_script_the_page_imports_is_served(assets):
    found, _ = assets
    imports = 0
    for name, asset in found.items():
        if not name.endswith(".js") or name == "sw.js":
            continue
        for target in re.findall(r"""from\s+["']\./([\w.-]+)["']""", asset.body.decode()):
            imports += 1
            assert target in found, f"{name} imports ./{target}"
    assert imports >= 5


def test_the_service_worker_keeps_only_files_that_exist(assets):
    found, _ = assets
    source = found["sw.js"].body.decode()
    shell = re.search(r"const SHELL = \[(.*?)\];", source, re.S).group(1)
    urls = re.findall(r"""["`]([^"`]+)["`]""", shell)
    assert "/" in urls and len(urls) >= 8
    for url in urls:
        assert url_to_name(url.replace("${VERSION}", "x")) in found, url


def test_the_service_worker_keeps_every_script_the_page_needs_to_start(assets):
    found, _ = assets
    kept = {
        url_to_name(u.replace("${VERSION}", "x"))
        for u in re.findall(r"""["`](/[^"`]*)["`]""", found["sw.js"].body.decode())
    }
    needed = {name for name in found if name.endswith(".js") and name != "sw.js"}
    assert needed <= kept


def test_the_manifest_is_valid_and_its_icons_are_real_pictures_of_the_size_it_claims(assets):
    found, _ = assets
    manifest = json.loads(found["manifest.webmanifest"].body)
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    assert manifest["name"] == manifest["short_name"] == "Agent"
    purposes = {(i["sizes"], i.get("purpose")) for i in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= purposes
    for icon in manifest["icons"]:
        body = found[url_to_name(icon["src"])].body
        assert body[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", body[16:24])
        assert f"{width}x{height}" == icon["sizes"], icon["src"]


def test_the_home_screen_icon_for_ios_is_a_180_pixel_picture(assets):
    found, _ = assets
    link = next(a for a in page_of(assets).named("link") if a.get("rel") == "apple-touch-icon")
    body = found[url_to_name(link["href"])].body
    assert body[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", body[16:24]) == (180, 180)


def test_the_svg_icon_is_a_picture_and_nothing_that_could_run(assets):
    svg = assets[0]["icon.svg"].body.decode().lower()
    assert svg.lstrip().startswith(("<svg", "<?xml"))
    for bad in ("<script", "onload", "onclick", "javascript:", "<foreignobject", "href="):
        assert bad not in svg


def test_served_types_match_the_files(assets):
    types = {name: asset.content_type for name, asset in assets[0].items()}
    assert types["app.js"].startswith("text/javascript")
    assert types["style.css"].startswith("text/css")
    assert types["index.html"].startswith("text/html")
    assert types["manifest.webmanifest"] == "application/manifest+json"
    assert types["icon.svg"] == "image/svg+xml" and types["icon-192.png"] == "image/png"


# ---- the page obeys its own security policy ------------------------------------------------------------
def test_the_page_has_no_inline_script_style_or_event_handler_for_the_policy_to_forbid(assets):
    page = page_of(assets)
    scripts = page.named("script")
    assert [s.get("type") for s in scripts] == ["module"] and all(s.get("src") for s in scripts)
    assert not page.script_text.strip()
    assert not page.named("style") and not page.named("iframe") and not page.named("object")
    assert not page.named("embed") and not page.named("form") and not page.named("base")
    for tag, attrs in page.tags:
        for name, value in attrs.items():
            assert not name.startswith("on"), f"<{tag} {name}>"
            assert name != "style", f"<{tag} style=...> is blocked by the policy"
            assert "javascript:" not in (value or "").lower()


def test_the_policy_allows_nothing_remote_and_no_inline_code():
    directives = dict(part.split(" ", 1) for part in CSP.split("; "))
    assert directives["default-src"] == "'none'"
    for name in (
        "script-src",
        "style-src",
        "connect-src",
        "font-src",
        "worker-src",
        "manifest-src",
    ):
        assert directives[name] == "'self'", name
    assert directives["img-src"].split() == ["'self'", "blob:", "data:"]
    assert "unsafe" not in CSP and "http" not in CSP and "*" not in CSP


def test_the_files_name_no_address_but_this_server(assets):
    namespace = "http://www.w3.org/2000/svg"  # an XML name that is never fetched
    for name, asset in assets[0].items():
        if name.endswith((".js", ".css", ".html", ".webmanifest")):
            for url in re.findall(r"https?://[\w.\-/%:~?#=&@]+", asset.body.decode()):
                assert url == namespace, f"{name} mentions {url}"


COMMENTS = re.compile(r"(^|\s)//[^\n]*|/\*.*?\*/", re.S)
FORBIDDEN_CODE = [
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    'setTimeout("',
    'setInterval("',
    "srcdoc",
    "javascript:",
    'setAttribute("style"',
    "importScripts",
    "XMLHttpRequest",
    "WebSocket",
    "postMessage",
    'localStorage.setItem("token',
]


@pytest.mark.parametrize(
    "name",
    [
        "api.js",
        "app.js",
        "assistant.js",
        "format.js",
        "md.js",
        "markdown-parser.js",
        "media.js",
        "product-shell.js",
        "query.js",
        "sheet.js",
        "store.js",
        "sw.js",
        "workbench.js",
    ],
)
def test_the_scripts_never_turn_text_into_markup_or_code(assets, name):
    code = COMMENTS.sub(" ", assets[0][name].body.decode())
    if name == "markdown-parser.js":
        # Upstream mdurl explicitly lists JavaScript among hostless protocols. Match only that
        # exact two-entry data table; keep all executable sinks and other scheme literals banned.
        # DOM-level malicious/encoded link tests still require the app's safeUrl whitelist.
        code, tables = re.subn(r'\bjavascript: !0,\s*"javascript:": !0', "", code)
        assert tables == 1
    for bad in FORBIDDEN_CODE:
        assert bad not in code, f"{name} uses {bad}"


def test_bundled_adapters_are_complete_browser_modules(assets):
    for name in ("query.js", "markdown-parser.js"):
        code = assets[0][name].body.decode()
        assert "process.env" not in code  # Node tests alone would hide this browser crash
        assert not re.search(r"\b(?:from|import)\s*\(?\s*['\"](?:@|node:|https?:)", code)
        assert len(code) < 200_000


def test_the_page_keeps_nothing_secret_where_a_script_could_read_it(assets):
    # The session lives in an HttpOnly cookie. The only thing the page stores is the colour theme.
    code = "".join(
        COMMENTS.sub(" ", a.body.decode()) for n, a in assets[0].items() if n.endswith(".js")
    )
    keys = set(
        re.findall(
            r"""(?:localStorage|sessionStorage)\.(?:set|get|remove)Item\(\s*["']([^"']+)["']""",
            code,
        )
    )
    assert keys == {"pa-theme"}
    assert "document.cookie" not in code and "indexedDB" not in code


# ---- the agent's name cannot break out of the page or its manifest ----------------------------------------
def test_a_hostile_agent_name_is_text_in_the_page_and_never_markup():
    found, _ = load_assets(agent=HOSTILE)
    page = Page(found["index.html"].body.decode())
    assert page.title == "个人助理 · " + HOSTILE
    titles = [
        a["content"] for a in page.named("meta") if a.get("name") == "apple-mobile-web-app-title"
    ]
    assert titles == [HOSTILE]
    assert [s.get("src") for s in page.named("script")] == [
        next(s["src"] for s in page.named("script"))
    ]
    assert not page.script_text.strip()
    assert b"<script>alert" not in found["index.html"].body


def test_a_hostile_agent_name_stays_one_string_in_the_manifest():
    found, _ = load_assets(agent=HOSTILE)
    manifest = json.loads(found["manifest.webmanifest"].body)
    assert manifest["name"] == manifest["short_name"] == HOSTILE
    assert len(manifest["icons"]) == 3  # nothing else in the file moved


def test_the_name_is_never_read_as_a_placeholder_itself():
    found, version = load_assets(agent="{{version}}")
    assert Page(found["index.html"].body.decode()).title == "个人助理 · {{version}}"
    assert json.loads(found["manifest.webmanifest"].body)["name"] == "{{version}}"
    assert version != "{{version}}"


def test_no_placeholder_is_left_unfilled(assets):
    for name in ("index.html", "sw.js", "manifest.webmanifest"):
        assert b"{{" not in assets[0][name].body, name


# ---- the version: changes when anything the page shows changes ------------------------------------------------
def test_the_version_is_stable_for_the_same_files_and_name():
    assert load_assets(agent="Ada")[1] == load_assets(agent="Ada")[1]
    assert re.fullmatch(r"[0-9a-f]{12}", load_assets(agent="Ada")[1])


def test_a_different_name_is_a_different_version_so_phones_fetch_the_page_again():
    one, version_one = load_assets(agent="Ada")
    two, version_two = load_assets(agent="Grace")
    assert version_one != version_two
    assert one["index.html"].etag != two["index.html"].etag
    assert one["sw.js"].etag != two["sw.js"].etag  # the worker's cache name moves with it


def test_changing_any_file_changes_the_version(tmp_path):
    copy = tmp_path / "static"
    shutil.copytree(STATIC_DIR, copy)
    before = load_assets(copy, agent="Ada")[1]
    (copy / "style.css").write_text((copy / "style.css").read_text() + "\n/* edit */\n")
    assert load_assets(copy, agent="Ada")[1] != before


def test_the_version_is_what_the_page_and_its_worker_carry(assets):
    found, version = assets
    page = page_of(assets)
    urls = [a["href"] for a in page.named("link") if "style.css" in (a.get("href") or "")]
    urls += [a["src"] for a in page.named("script")]
    assert urls and all(u.endswith(f"?v={version}") for u in urls)
    assert f'const VERSION = "{version}"' in found["sw.js"].body.decode()


def test_every_file_has_its_own_validator_so_unchanged_files_are_not_resent(assets):
    etags = [a.etag for a in assets[0].values()]
    assert all(re.fullmatch(r'"[0-9a-f]{20}"', e) for e in etags)
    assert len(set(etags)) == len(etags)


def test_hidden_files_and_unknown_kinds_in_the_folder_are_not_served(tmp_path):
    copy = tmp_path / "static"
    shutil.copytree(STATIC_DIR, copy)
    (copy / ".secret.js").write_text("nope")
    (copy / "notes.txt").write_text("nope")
    (copy / "backup.js~").write_text("nope")
    found, _ = load_assets(copy)
    assert ".secret.js" not in found and "notes.txt" not in found and "backup.js~" not in found


# ---- the scripts run under Node's test runner -------------------------------------------------------------------
def node_major() -> int | None:
    node = shutil.which("node")
    if not node:
        return None
    out = subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout
    match = re.match(r"v(\d+)", out.strip())
    return int(match.group(1)) if match else None


needs_node = pytest.mark.skipif(
    (node_major() or 0) < 22, reason="the page's script tests need Node 22 or newer"
)


@needs_node
@pytest.mark.parametrize(
    "name",
    [
        "api.js",
        "app.js",
        "assistant.js",
        "format.js",
        "md.js",
        "media.js",
        "product-shell.js",
        "sheet.js",
        "store.js",
        "sw.js",
    ],
)
def test_every_script_parses(name):
    result = subprocess.run(
        ["node", "--check", str(STATIC_DIR / name)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@needs_node
def test_the_scripts_own_tests_pass():
    result = subprocess.run(
        ["node", "--test", "--test-reporter=tap", "./tests/js/*.test.mjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    passed = re.search(r"# pass (\d+)", result.stdout)
    assert passed and int(passed.group(1)) >= 70  # a glob that matched nothing would also "pass"
