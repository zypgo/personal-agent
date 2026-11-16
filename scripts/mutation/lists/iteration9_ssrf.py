"""Mutations for iteration 9: the redirect decision and the capped read, after `_follow` was split.

Run: uv run python scripts/mutation/run.py scripts/mutation/lists/iteration9_ssrf.py
Format and meaning: scripts/mutation/run.py.
"""

# ruff: noqa

SS = "src/personal_agent/security/ssrf.py"
TS = ["tests/test_ssrf.py"]
TW = ["tests/test_ssrf.py", "tests/test_web_tools.py"]

M = [
    # ---- what a redirect carries ---------------------------------------------------------
    (
        "ss redirect: every header crosses sites",
        SS,
        "            headers = {k: v for k, v in headers.items() if k.lower() in KEPT_ACROSS_SITES}",
        "            headers = dict(headers)",
        TS,
    ),
    (
        "ss redirect: only the known credential headers stay behind",
        SS,
        "if k.lower() in KEPT_ACROSS_SITES}",
        'if k.lower() not in ("authorization", "cookie")}',
        TS,
    ),
    (
        "ss redirect: a site is told apart by its host alone",
        SS,
        "        if origin_of(target) != origin_of(self.url):",
        "        if host_of(target) != host_of(self.url):",
        TS,
    ),
    (
        "ss redirect: data goes to another site on 307 and 308",
        SS,
        "            if content is not None and status in (307, 308):",
        "            if False:",
        TS,
    ),
    (
        "ss redirect: data is refused on every redirect",
        SS,
        "            if content is not None and status in (307, 308):",
        "            if content is not None:",
        TS,
    ),
    (
        "ss redirect: 308 may carry data",
        SS,
        "if content is not None and status in (307, 308):",
        "if content is not None and status == 307:",
        TS,
    ),
    # ---- what a redirect turns the request into -------------------------------------------
    (
        "ss redirect: a post stays a post",
        SS,
        '        if status in (301, 302, 303) and verb not in ("GET", "HEAD"):',
        "        if False:",
        TS,
    ),
    (
        "ss redirect: 301 and 302 keep the verb",
        SS,
        "        if status in (301, 302, 303) and verb",
        "        if status == 303 and verb",
        TS,
    ),
    (
        "ss redirect: a head becomes a get",
        SS,
        'verb not in ("GET", "HEAD"):',
        'verb != "GET":',
        TS,
    ),
    (
        "ss redirect: the body is sent again after it became a get",
        SS,
        '            verb, content = "GET", None',
        '            verb, content = "GET", content',
        TS,
    ),
    (
        "ss redirect: the headers that describe the body stay",
        SS,
        '                if k.lower() not in ("content-type", "content-length")\n',
        "",
        TS,
    ),
    (
        "ss redirect: only the type is dropped, not the length",
        SS,
        'if k.lower() not in ("content-type", "content-length")',
        'if k.lower() not in ("content-type",)',
        TS,
    ),
    # ---- the address it asks for ---------------------------------------------------------
    (
        "ss redirect: the target is not vetted",
        SS,
        "        target = check_url(str(self.url.join(location)))",
        "        target = self.url.join(location)",
        TS,
    ),
    (
        "ss redirect: a relative address is read as absolute",
        SS,
        "self.url.join(location)",
        "httpx.URL(location)",
        TS,
    ),
    (
        "ss redirect: followed addresses are not listed",
        SS,
        "                    visited.append(str(following.url))\n",
        "",
        TS,
    ),
    (
        "ss redirect: the hop callback sees the address it left, not the one it reaches",
        SS,
        "                on_hop(request.url, previous)",
        "                on_hop(previous or request.url, previous)",
        TS,
    ),
    (
        "ss redirect: one hop too many",
        SS,
        "for _ in range(cfg.max_redirects + 1):",
        "for _ in range(cfg.max_redirects + 2):",
        TS,
    ),
    # ---- the download ---------------------------------------------------------------------
    (
        "ss read: no cap",
        SS,
        "        body += chunk[:room]",
        "        body += chunk",
        TS,
    ),
    (
        "ss read: an exact fit counts as cut",
        SS,
        "        if len(chunk) > room:",
        "        if len(chunk) >= room:",
        TS,
    ),
    (
        "ss read: a cut body is not said to be cut",
        SS,
        "            return bytes(body), True",
        "            return bytes(body), False",
        TS,
    ),
    (
        "ss read: asks for compressed answers",
        SS,
        '"accept-encoding": "identity"',
        '"accept-encoding": "gzip"',
        TS,
    ),
    # ---- what a request says about itself ---------------------------------------------------
    (
        "ss header: a caller's header of another capitalization is sent beside ours",
        SS,
        "    headers.update(written)\n    return headers",
        "    headers = httpx.Headers({**dict(headers.items()), **written})\n    return headers",
        TW,
    ),
    (
        "ss header: ours replace the caller's",
        SS,
        "    headers.update(written)\n    return headers",
        "    written = httpx.Headers(written)\n    written.update(headers)\n    return written",
        TW,
    ),
    (
        "ss header: ours are dropped",
        SS,
        "    headers.update(written)\n    return headers",
        "    return httpx.Headers(written)",
        TW,
    ),
    (
        "ss header: the identity encoding is not asked for",
        SS,
        '            "accept-encoding": "identity",\n',
        "",
        TS,
    ),
    (
        "ss header: ours are not sent again after a redirect",
        SS,
        "                headers=_headers_of(cfg, request.headers),",
        "                headers=_headers_of(cfg, request.headers) if previous is None else request.headers,",
        TS,
    ),
]
