import assert from "node:assert/strict";
import { test } from "node:test";

import { MAX_CHARS, parseInline, parseMarkdown, renderMarkdown, safeUrl } from "../../src/personal_agent/channels/web/static/md.js";
import { elements, find, makeDocument, toHtml } from "./fake_dom.mjs";

const doc = makeDocument();
const html = (source) => toHtml(renderMarkdown(doc, source));
const fragment = (source) => renderMarkdown(doc, source);

// ---- what it understands -------------------------------------------------------------------
test("a paragraph keeps its words and breaks its lines", () => {
  assert.equal(html("one\ntwo\n\nthree"), "<p>one<br>two</p><p>three</p>");
});

test("emphasis, strong, strike and code", () => {
  assert.equal(
    html("**bold** and *em* and ~~gone~~ and `code`"),
    '<p><strong>bold</strong> and <em>em</em> and <del>gone</del> and <code class="inline">code</code></p>',
  );
});

test("nested emphasis resolves the way people write it", () => {
  assert.equal(html("*a **b** c*"), "<p><em>a <strong>b</strong> c</em></p>");
  assert.equal(html("***both***"), "<p><em><strong>both</strong></em></p>");
  assert.equal(html("**Note:** text"), "<p><strong>Note:</strong> text</p>");
});

test("things that only look like emphasis stay as they are", () => {
  assert.equal(html("snake_case_word"), "<p>snake_case_word</p>");
  assert.equal(html("2 * 3 * 4"), "<p>2 * 3 * 4</p>");
  assert.equal(html("a ~ b ~~ c"), "<p>a ~ b ~~ c</p>");
  assert.equal(html("5 * 3 = 15 and 2*2"), "<p>5 * 3 = 15 and 2*2</p>");
});

test("a backslash makes a character literal", () => {
  assert.equal(html("\\*not em\\* and \\`not code\\`"), "<p>*not em* and `not code`</p>");
});

test("code spans keep everything, including markup characters", () => {
  assert.equal(html("`**x** <b>` and ``a ` b``"), '<p><code class="inline">**x** &lt;b&gt;</code> and <code class="inline">a ` b</code></p>');
});

test("headings start at h2 because the page has its own h1", () => {
  assert.equal(html("# One\n## Two\n###### Six"), "<h2>One</h2><h3>Two</h3><h6>Six</h6>");
  assert.equal(html("#hashtag"), "<p>#hashtag</p>");
});

test("lists: bullets, numbers from where they start, nesting and tasks", () => {
  assert.equal(html("- a\n- b"), "<ul><li><p>a</p></li><li><p>b</p></li></ul>");
  assert.equal(html("3. c\n4. d"), '<ol start="3"><li><p>c</p></li><li><p>d</p></li></ol>');
  assert.equal(
    html("- a\n  - b\n  - c\n- d"),
    "<ul><li><p>a</p><ul><li><p>b</p></li><li><p>c</p></li></ul></li><li><p>d</p></li></ul>",
  );
  const tasks = html("- [ ] open\n- [x] done");
  assert.match(tasks, /data-task="open"/);
  assert.match(tasks, /data-task="done"/);
  assert.ok(!tasks.includes("[x]") && !tasks.includes("[ ]"));
});

test("a list with blank lines between items is still one list", () => {
  const out = html("1. first\n\n2. second\n\n3. third");
  assert.equal(find(fragment("1. first\n\n2. second\n\n3. third"), "ol").length, 1);
  assert.equal((out.match(/<li/g) ?? []).length, 3);
});

test("a list directly after a line of text is still a list", () => {
  assert.equal(html("Steps:\n1. one\n2. two"), "<p>Steps:</p><ol><li><p>one</p></li><li><p>two</p></li></ol>");
});

test("indented text belongs to the item above", () => {
  const out = html("1. install\n   then run it\n2. done");
  assert.match(out, /install<br>then run it/);
});

test("a rule is a rule, not a list", () => {
  assert.equal(html("a\n\n---\n\nb"), "<p>a</p><hr><p>b</p>");
  assert.equal(html("* * *"), "<hr>");
});

test("quotes can hold other things", () => {
  assert.equal(html("> one\n> two\n>\n> - x"), "<blockquote><p>one<br>two</p><ul><li><p>x</p></li></ul></blockquote>");
});

test("code blocks keep every character and say which language", () => {
  const out = html("```python\nprint('<b>hi</b>')\n  indented\n```");
  assert.match(out, /<span class="lang">python<\/span>/);
  assert.match(out, /<pre><code>print\('&lt;b&gt;hi&lt;\/b&gt;'\)\n  indented<\/code><\/pre>/);
  assert.ok(find(fragment("```\nx\n```"), "button")[0].getAttribute("data-copy") !== null);
});

test("an unclosed block runs to the end; a longer fence closes only with a longer fence", () => {
  assert.match(html("```\nopen forever\nstill code"), /open forever\nstill code/);
  assert.match(html("````\n```\ninner\n```\n````"), /<code>```\ninner\n```<\/code>/);
  assert.match(html("~~~sh\nls\n~~~"), /<code>ls<\/code>/);
});

test("inline triple backticks are not a code block", () => {
  assert.equal(html("```not a block``` here"), '<p><code class="inline">not a block</code> here</p>');
});

test("tables with alignment, escaped pipes, and short rows", () => {
  const out = html("| a | b | c |\n|:--|:-:|--:|\n| 1 | x \\| y | 3 |\n| 4 |");
  assert.match(out, /<th class="al-left">a<\/th><th class="al-center">b<\/th><th class="al-right">c<\/th>/);
  assert.match(out, /<td class="al-center">x \| y<\/td>/);
  assert.match(out, /<td class="al-left">4<\/td><td class="al-center"><\/td><td class="al-right"><\/td>/);
  assert.match(out, /<div class="table-wrap">/);
});

test("a line with pipes that is not followed by a delimiter row is a paragraph", () => {
  assert.equal(html("a | b\nc | d"), "<p>a | b<br>c | d</p>");
});

// ---- links ---------------------------------------------------------------------------------
test("only web and mail addresses are links", () => {
  assert.equal(safeUrl("https://example.com/a?b=c#d"), "https://example.com/a?b=c#d");
  assert.equal(safeUrl("HTTP://Example.com"), "http://example.com/");
  assert.equal(safeUrl("mailto:me@example.com"), "mailto:me@example.com");
  for (const bad of [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    " javascript:alert(1)",
    "java\nscript:alert(1)",
    "java\tscript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "blob:https://example.com/x",
    "//evil.example/path",
    "/relative/path",
    "#fragment",
    "tel:+123",
    "https://exa mple.com",
    "https://example.com/a b",
    "https://example.com/a\u0000b",
    "https://example.com/a\u007fb",
    "",
    null,
    undefined,
    "https://example.com/" + "a".repeat(3000),
  ]) {
    assert.equal(safeUrl(bad), null, String(bad));
  }
});

test("a link says where it really goes when the words say something else", () => {
  const out = html("[the docs](https://www.example.com/docs)");
  assert.equal(
    out,
    '<p><a href="https://www.example.com/docs" target="_blank" rel="noopener noreferrer nofollow">the docs</a><span class="host">example.com</span></p>',
  );
});

test("a link that is its own words needs no note", () => {
  const out = html("see https://example.com/page.");
  assert.equal(out, '<p>see <a href="https://example.com/page" target="_blank" rel="noopener noreferrer nofollow">https://example.com/page</a>.</p>');
});

test("a bare address does not swallow the punctuation after it, nor a closing bracket that is not its own", () => {
  assert.match(html("(see https://example.com/a)"), /href="https:\/\/example.com\/a"/);
  assert.match(html("https://en.wikipedia.org/wiki/Foo_(bar) is it"), /href="https:\/\/en.wikipedia.org\/wiki\/Foo_\(bar\)"/);
  assert.match(html("go to https://example.com, then"), /href="https:\/\/example.com\/"/);
  assert.match(html("**https://example.com**"), /<strong><a href="https:\/\/example.com\/"/);
});

test("angle-bracket addresses are links; other angle-bracket text is only text", () => {
  assert.match(html("<https://example.com/x>"), /<a href="https:\/\/example.com\/x"/);
  assert.equal(html("<div>not html</div>"), "<p>&lt;div&gt;not html&lt;/div&gt;</p>");
});

test("a link with an unsafe address keeps its words and loses the link", () => {
  assert.equal(html("[click](javascript:alert(1))"), "<p>click</p>");
  assert.equal(html("[click](data:text/html;base64,AAAA)"), "<p>click</p>");
  assert.equal(find(fragment("[x](javascript:alert(1))"), "a").length, 0);
});

test("nested link syntax follows CommonMark and never creates nested links", () => {
  const root = fragment("[**bold** and [inner](https://b.example)](https://a.example)");
  const out = toHtml(root);
  const links = find(root, "a");
  // CommonMark recognizes the inner link; linkify recognizes the now-bare outer URL. They are
  // separate siblings, never nested anchors, and both destinations pass the DOM URL whitelist.
  assert.deepEqual(links.map(link => link.getAttribute("href")), ["https://b.example/", "https://a.example/"]);
  for (const link of links) assert.equal(find(link, "a").length, 0);
  assert.match(out, /<strong>bold<\/strong>/);
  assert.match(root.textContent, /\]\(https:\/\/a.example\)/);
});

test("a linked image is an inert label inside one validated link", () => {
  const root = fragment("[![diagram](https://tracker.example/image.png)](https://docs.example/)");
  assert.equal(find(root, "img").length, 0);
  assert.equal(find(root, "a").length, 1);
  assert.equal(find(root, "a")[0].getAttribute("href"), "https://docs.example/");
  assert.match(root.textContent, /image: diagram/);
});

test("reference links and entities are parsed without enabling HTML", () => {
  const out = html("[说明][docs] &amp; <b>literal</b>\n\n[docs]: https://docs.example/guide");
  assert.match(out, /href="https:\/\/docs.example\/guide"/);
  assert.match(out, /说明<\/a>/);
  assert.match(out, /&amp; &lt;b&gt;literal&lt;\/b&gt;/);
});

test("pictures are never loaded, only named", () => {
  const out = html("![a cat](https://tracker.example/cat.png?secret=1234)");
  assert.equal(find(fragment("![x](https://e.example/x.png)"), "img").length, 0);
  assert.match(out, /图片：a cat/);
  assert.doesNotMatch(out, /<img/i);
});

// ---- what it must never do -----------------------------------------------------------------
test("markup in an answer is shown as text, never built", () => {
  const nasty = [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    '<a href="javascript:alert(1)">x</a>',
    "<iframe src=//evil.example></iframe>",
    "<style>body{display:none}</style>",
    '</p><p onclick="x()">',
    "<svg onload=alert(1)>",
  ];
  for (const text of nasty) {
    const root = fragment(text + "\n\n" + "`" + text + "`\n\n```\n" + text + "\n```\n\n> " + text + "\n\n- " + text);
    const tags = new Set(elements(root).map((e) => e.tagName));
    for (const bad of ["SCRIPT", "IMG", "IFRAME", "STYLE", "SVG", "A"]) {
      assert.ok(!tags.has(bad), `${bad} appeared for ${text}`);
    }
    assert.match(toHtml(root), /&lt;/);
  }
});

const ALLOWED_TAGS = new Set([
  "P", "H2", "H3", "H4", "H5", "H6", "HR", "BLOCKQUOTE", "DIV", "SPAN", "BUTTON", "PRE", "CODE", "UL", "OL", "LI",
  "TABLE", "THEAD", "TBODY", "TR", "TH", "TD", "A", "STRONG", "EM", "DEL", "BR",
]);
const ALLOWED_ATTRS = new Set(["class", "href", "target", "rel", "start", "type", "data-copy", "data-task", "aria-label"]);

function assertInert(root) {
  for (const node of elements(root)) {
    assert.ok(ALLOWED_TAGS.has(node.tagName), `unexpected <${node.tagName}>`);
    for (const [name, value] of node.attrs) {
      assert.ok(ALLOWED_ATTRS.has(name), `unexpected attribute ${name}`);
      if (name === "href") assert.match(value, /^(https?:|mailto:)/i);
    }
    if (node.tagName === "A") {
      assert.equal(node.getAttribute("target"), "_blank");
      assert.match(node.getAttribute("rel"), /noopener/);
    }
  }
}

function lcg(seed) {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 2 ** 32;
  };
}

test("whatever the input, only known elements and attributes come out (2,000 random documents)", () => {
  const random = lcg(20261115);
  const pieces = [
    "*", "**", "_", "__", "~~", "`", "``", "```", "~~~", "[", "]", "(", ")", "![", "<", ">", "|", "-", "---", ":", "#", "##",
    "1.", "- ", "> ", "\n", "\n\n", " ", "  ", "\\", "a", "b word", "https://e.example/x", "javascript:alert(1)", "mailto:a@b.c",
    "<script>", "</", "&amp;", "\t", "[x](", "](https://ok.example)", "- [ ] ", "| a | b |", "|--|--|",
  ];
  for (let n = 0; n < 2000; n++) {
    let source = "";
    for (let k = 0, count = 1 + Math.floor(random() * 60); k < count; k++) source += pieces[Math.floor(random() * pieces.length)];
    assertInert(fragment(source));
  }
});

test("hostile sizes and shapes finish quickly and do not overflow the stack", () => {
  const started = Date.now();
  const shapes = [
    "> ".repeat(2000) + "deep",
    "- ".repeat(2000) + "deep",
    Array.from({ length: 300 }, (_, i) => " ".repeat(i * 2) + "- item").join("\n"),
    "*a ".repeat(5000),
    "a* ".repeat(5000),
    "[".repeat(5000),
    "[a](".repeat(2000),
    "`".repeat(5000),
    "|a".repeat(3000) + "\n" + "|-".repeat(3000),
    "x".repeat(MAX_CHARS * 3),
    "<".repeat(20000),
    "\\".repeat(20000),
  ];
  for (const shape of shapes) assertInert(fragment(shape));
  assert.ok(Date.now() - started < 5000, "took " + (Date.now() - started) + " ms");
});

test("a long run of unmatched markers is not worked through, only shown", () => {
  const started = Date.now();
  const out = parseInline("a* ".repeat(19999));
  assert.ok(Date.now() - started < 400, "took " + (Date.now() - started) + " ms");
  assert.equal(out.length, 1);
  assert.equal(out[0].t, "text");
});

test("an enormous answer is cut and says so", () => {
  const out = html("word ".repeat(MAX_CHARS));
  assert.match(out, /个字符未显示/);
  assert.ok(out.length < MAX_CHARS * 2);
});

test("a table is capped, not unbounded", () => {
  const rows = Array.from({ length: 1000 }, (_, i) => `| ${i} | x |`).join("\n");
  const root = fragment("| n | v |\n|---|---|\n" + rows);
  assert.equal(find(root, "tr").length, 201); // the header and 200 rows
});

test("nothing ever uses innerHTML", () => {
  // The fake DOM throws on it; rendering the whole zoo above already proved it. This is the
  // direct statement of that, for anyone who changes md.js.
  assert.throws(() => {
    makeDocument().createElement("div").innerHTML = "<b>";
  }, /must not be used/);
  assert.doesNotThrow(() => fragment("# h\n\n- a\n\n| a |\n|---|\n| b |\n\n```x\ny\n```"));
});

// ---- the shape it parses to ------------------------------------------------------------------
test("the parse is plain data", () => {
  const [heading, list] = parseMarkdown("# T\n\n- x");
  assert.equal(heading.type, "h");
  assert.equal(heading.level, 1);
  assert.equal(list.type, "list");
  assert.deepEqual(parseInline("a **b**"), [
    { t: "text", v: "a " },
    { t: "strong", kids: [{ t: "text", v: "b" }] },
  ]);
});

test("empty and odd inputs are fine", () => {
  assert.equal(html(""), "");
  assert.equal(html(null), "");
  assert.equal(html(undefined), "");
  assert.equal(html("   \n\n  "), "");
  assert.equal(html("\r\nwindows\r\nline endings\r\n"), "<p>windows<br>line endings</p>");
  assert.equal(html(42), "<p>42</p>");
});
