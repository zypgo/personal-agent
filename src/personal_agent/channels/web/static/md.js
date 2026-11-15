// A small Markdown reader for the agent's answers.
//
// What matters most is what it will not do. The text can come from the model, which may be
// repeating a web page that someone else wrote, so it is treated as untrusted:
//   * it builds DOM nodes and sets their text with textContent; no HTML string is ever produced or
//     parsed, so markup in an answer is only ever shown as text;
//   * links go to http, https or mailto and nowhere else, and say which host they lead to;
//   * pictures are never fetched (the page's policy forbids it, and an address can carry data out);
//   * input is cut at a length and nesting at a depth, so a hostile answer cannot freeze the page.
//
// `parseMarkdown` is pure (no DOM) and is what the tests look at; `renderMarkdown` turns the result
// into nodes of whatever `document` it is given.

export const MAX_CHARS = 60_000;
const MAX_DEPTH = 6;
const MAX_INLINE = 20_000;
const MAX_TABLE_ROWS = 200;
const MAX_TABLE_COLUMNS = 20;

// ---------------------------------------------------------------------------------------------
// Blocks
// ---------------------------------------------------------------------------------------------
const FENCE = /^( {0,3})(`{3,}|~{3,})[ \t]*([^\s`]*)[^`]*$/;
const HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const QUOTE = /^ {0,3}>[ ]?(.*)$/;
const ITEM = /^( *)([-*+]|\d{1,9}[.)])([ \t]+)(.*)$/;
const TABLE_DELIMITER = /^ *\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|? *$/;

const isBlank = (line) => /^\s*$/.test(line);
const indentOf = (line) => /^ */.exec(line)[0].length;
const strip = (line, count) => line.slice(Math.min(count, indentOf(line)));

function startsTable(lines, i) {
  if (i + 1 >= lines.length || !lines[i].includes("|")) return false;
  const delimiter = lines[i + 1];
  if (!delimiter.includes("|") || !TABLE_DELIMITER.test(delimiter)) return false;
  return splitRow(lines[i]).length === splitRow(delimiter).length;
}

function startsBlock(lines, i) {
  const line = lines[i];
  return (
    FENCE.test(line) ||
    HEADING.test(line) ||
    RULE.test(line) ||
    QUOTE.test(line) ||
    ITEM.test(line) ||
    startsTable(lines, i)
  );
}

function splitRow(line) {
  let row = line.trim();
  if (row.startsWith("|")) row = row.slice(1);
  if (row.endsWith("|") && !row.endsWith("\\|")) row = row.slice(0, -1);
  const cells = [];
  let cell = "";
  for (let i = 0; i < row.length; i++) {
    if (row[i] === "\\" && row[i + 1] === "|") {
      cell += "|";
      i++;
    } else if (row[i] === "|") {
      cells.push(cell.trim());
      cell = "";
    } else {
      cell += row[i];
    }
  }
  cells.push(cell.trim());
  return cells;
}

function parseTable(lines, start) {
  const head = splitRow(lines[start]).slice(0, MAX_TABLE_COLUMNS);
  const align = splitRow(lines[start + 1])
    .slice(0, head.length)
    .map((cell) => {
      const left = cell.startsWith(":");
      const right = cell.endsWith(":");
      return left && right ? "center" : right ? "right" : left ? "left" : null;
    });
  const rows = [];
  let i = start + 2;
  while (i < lines.length && !isBlank(lines[i]) && lines[i].includes("|") && rows.length < MAX_TABLE_ROWS) {
    if (FENCE.test(lines[i]) || HEADING.test(lines[i])) break;
    const cells = splitRow(lines[i]).slice(0, head.length);
    while (cells.length < head.length) cells.push("");
    rows.push(cells.map((cell) => parseInline(cell)));
    i++;
  }
  return {
    block: {
      type: "table",
      align,
      head: head.map((cell) => parseInline(cell)),
      rows,
    },
    next: i,
  };
}

function parseList(lines, start, depth) {
  const first = ITEM.exec(lines[start]);
  const base = first[1].length;
  const ordered = /\d/.test(first[2][0]);
  const items = [];
  let i = start;
  while (i < lines.length) {
    let k = i;
    while (k < lines.length && isBlank(lines[k])) k++; // blank lines between items keep one list
    const m = k < lines.length ? ITEM.exec(lines[k]) : null;
    if (!m || m[1].length > base + 1 || /\d/.test(m[2][0]) !== ordered || RULE.test(lines[k])) break;
    i = k;
    const contentIndent = m[1].length + m[2].length + m[3].length;
    const body = [m[4]];
    i++;
    while (i < lines.length) {
      const next = lines[i];
      if (isBlank(next)) {
        let k = i;
        while (k < lines.length && isBlank(lines[k])) k++;
        if (k < lines.length && indentOf(lines[k]) >= base + 2) {
          while (i < k) body.push(lines[i++]);
          continue;
        }
        break;
      }
      const indent = indentOf(next);
      if (indent >= base + 2) {
        body.push(strip(next, contentIndent));
        i++;
        continue;
      }
      if (ITEM.test(next) || FENCE.test(next) || HEADING.test(next) || RULE.test(next) || QUOTE.test(next)) break;
      body.push(next.trim()); // the rest of a paragraph that was not indented
      i++;
    }
    const blocks = parseBlocks(body, depth + 1);
    let task = null;
    const lead = blocks[0];
    if (lead && lead.type === "p") {
      const mark = /^\[( |x|X)\][ \t]+/.exec(lead.source);
      if (mark) {
        task = mark[1] === " " ? "open" : "done";
        lead.source = lead.source.slice(mark[0].length);
        lead.inline = parseInline(lead.source);
      }
    }
    items.push({ blocks, task, number: ordered ? parseInt(m[2], 10) : null });
  }
  return {
    block: { type: "list", ordered, start: ordered ? items[0].number : 1, items },
    next: i,
  };
}

function parseBlocks(lines, depth) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) {
      i++;
      continue;
    }
    if (depth >= MAX_DEPTH) {
      const source = lines.slice(i).join("\n").trim();
      out.push({ type: "p", source, inline: [{ t: "text", v: source }] });
      break;
    }
    let m = FENCE.exec(line);
    if (m) {
      const marker = m[2][0];
      const size = m[2].length;
      const closing = new RegExp(`^ {0,3}${marker === "`" ? "`" : "~"}{${size},}[ \\t]*$`);
      const body = [];
      i++;
      while (i < lines.length && !closing.test(lines[i])) body.push(strip(lines[i++], m[1].length));
      i++; // the closing fence, or past the end of an unclosed one
      out.push({ type: "code", lang: m[3].replace(/[^\w+#.-]/g, "").slice(0, 24), text: body.join("\n") });
      continue;
    }
    m = HEADING.exec(line);
    if (m) {
      out.push({ type: "h", level: m[1].length, inline: parseInline(m[2] ?? "") });
      i++;
      continue;
    }
    if (RULE.test(line)) {
      out.push({ type: "hr" });
      i++;
      continue;
    }
    if (QUOTE.test(line)) {
      const inner = [];
      while (i < lines.length && (m = QUOTE.exec(lines[i]))) {
        inner.push(m[1]);
        i++;
      }
      out.push({ type: "quote", blocks: parseBlocks(inner, depth + 1) });
      continue;
    }
    if (startsTable(lines, i)) {
      const table = parseTable(lines, i);
      out.push(table.block);
      i = table.next;
      continue;
    }
    if (ITEM.test(line)) {
      const list = parseList(lines, i, depth);
      out.push(list.block);
      i = list.next;
      continue;
    }
    const para = [line.trim()];
    i++;
    while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines, i)) para.push(lines[i++].trim());
    const source = para.join("\n");
    out.push({ type: "p", source, inline: parseInline(source) });
  }
  return out;
}

export function parseMarkdown(source) {
  let text = String(source ?? "").replace(/\r\n?/g, "\n");
  let hidden = 0;
  if (text.length > MAX_CHARS) {
    hidden = text.length - MAX_CHARS;
    text = text.slice(0, MAX_CHARS);
  }
  const blocks = parseBlocks(text.split("\n"), 0);
  if (hidden) blocks.push({ type: "cut", hidden });
  return blocks;
}

// ---------------------------------------------------------------------------------------------
// Inline
// ---------------------------------------------------------------------------------------------
const ESCAPABLE = /[!-/:-@[-`{-~]/;
const PUNCTUATION = /[!-/:-@[-`{-~\p{P}\p{S}]/u;
const WORD = /[\p{L}\p{N}]/u;
const BARE_URL = /https?:\/\/[^\s<]+/iy;
const AUTOLINK = /<((?:https?:\/\/|mailto:)[^\s<>]+)>/iy;

export function safeUrl(raw) {
  const value = String(raw ?? "").trim();
  if (!value || value.length > 2048 || /[\u0000-\u0020\u007f]/.test(value)) return null;
  let url;
  try {
    url = new URL(value);
  } catch {
    return null; // a relative address means nothing on this page
  }
  return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : null;
}

function skipCode(src, i) {
  // src[i] is a backtick: the index after the code span that starts here, or i + run if none
  let run = 0;
  while (src[i + run] === "`") run++;
  let j = i + run;
  while (j < src.length) {
    if (src[j] === "`") {
      let n = 0;
      while (src[j + n] === "`") n++;
      if (n === run) return { end: j + n, content: src.slice(i + run, j), run };
      j += n;
    } else {
      j++;
    }
  }
  return { end: i + run, content: null, run };
}

function parseLinkAt(src, i) {
  // src[i] is "[": the label, address and end of an inline link, or null
  let depth = 0;
  let j = i;
  for (; j < src.length; j++) {
    const c = src[j];
    if (c === "\\") {
      j++;
    } else if (c === "`") {
      const code = skipCode(src, j);
      if (code.content !== null) j = code.end - 1;
      else j = code.end - 1;
    } else if (c === "[") {
      depth++;
    } else if (c === "]") {
      depth--;
      if (depth === 0) break;
    }
  }
  if (depth !== 0 || j >= src.length || src[j + 1] !== "(") return null;
  const label = src.slice(i + 1, j);
  let k = j + 2;
  while (src[k] === " " || src[k] === "\n") k++;
  let href = "";
  if (src[k] === "<") {
    const close = src.indexOf(">", k);
    if (close < 0) return null;
    href = src.slice(k + 1, close);
    k = close + 1;
  } else {
    let parens = 0;
    const from = k;
    for (; k < src.length; k++) {
      const c = src[k];
      if (c === "\\") k++;
      else if (c === "(") parens++;
      else if (c === ")") {
        if (parens === 0) break;
        parens--;
      } else if (c <= " ") break;
    }
    href = src.slice(from, k);
  }
  while (src[k] === " " || src[k] === "\n") k++;
  if (src[k] === '"' || src[k] === "'" || src[k] === "(") {
    const close = src.indexOf(src[k] === "(" ? ")" : src[k], k + 1);
    if (close < 0) return null;
    k = close + 1;
    while (src[k] === " " || src[k] === "\n") k++;
  }
  if (src[k] !== ")") return null;
  return { label, href: href.replace(/\\([!-/:-@[-`{-~])/g, "$1"), end: k + 1 };
}

function trimUrl(url) {
  let end = url.length;
  for (;;) {
    const last = url[end - 1];
    if (end > 0 && /[.,;:!?'"*_~]/.test(last)) end--;
    else if (end > 0 && last === ")" && count(url.slice(0, end), ")") > count(url.slice(0, end), "(")) end--;
    else break;
  }
  return url.slice(0, end);
}

const count = (text, char) => text.split(char).length - 1;

function tokenize(src) {
  const nodes = [];
  let text = "";
  const flush = () => {
    if (text) nodes.push({ t: "text", v: text });
    text = "";
  };
  let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (c === "\\") {
      const next = src[i + 1];
      if (next === "\n") {
        flush();
        nodes.push({ t: "br" });
        i += 2;
      } else if (next !== undefined && ESCAPABLE.test(next)) {
        text += next;
        i += 2;
      } else {
        text += c;
        i++;
      }
      continue;
    }
    if (c === "`") {
      const code = skipCode(src, i);
      if (code.content === null) {
        text += "`".repeat(code.run);
      } else {
        flush();
        let value = code.content.replace(/\n/g, " ");
        if (value.length > 2 && value.startsWith(" ") && value.endsWith(" ") && value.trim()) value = value.slice(1, -1);
        nodes.push({ t: "code", v: value });
      }
      i = code.end;
      continue;
    }
    if (c === "\n") {
      text = text.replace(/[ \t]+$/, "");
      flush();
      nodes.push({ t: "br" });
      i++;
      continue;
    }
    if (c === "!" && src[i + 1] === "[") {
      const link = parseLinkAt(src, i + 1);
      if (link) {
        flush();
        nodes.push({ t: "image", alt: link.label.replace(/[*_`~[\]]/g, ""), href: link.href });
        i = link.end;
        continue;
      }
    }
    if (c === "[") {
      const link = parseLinkAt(src, i);
      if (link) {
        flush();
        nodes.push({ t: "link", href: link.href, kids: parseInline(link.label, true) });
        i = link.end;
        continue;
      }
    }
    if (c === "<") {
      AUTOLINK.lastIndex = i;
      const m = AUTOLINK.exec(src);
      if (m) {
        flush();
        nodes.push({ t: "link", href: m[1], kids: [{ t: "text", v: m[1] }] });
        i += m[0].length;
        continue;
      }
    }
    if ((c === "h" || c === "H") && (i === 0 || !WORD.test(src[i - 1]))) {
      BARE_URL.lastIndex = i;
      const m = BARE_URL.exec(src);
      if (m) {
        const url = trimUrl(m[0]);
        flush();
        nodes.push({ t: "link", href: url, kids: [{ t: "text", v: url }] });
        i += url.length;
        continue;
      }
    }
    if (c === "*" || c === "_" || (c === "~" && src[i + 1] === "~")) {
      let n = 0;
      while (src[i + n] === c) n++;
      if (c === "~" && n !== 2) {
        text += c.repeat(n);
        i += n;
        continue;
      }
      const before = i === 0 ? " " : src[i - 1];
      const after = i + n >= src.length ? " " : src[i + n];
      const beforeSpace = /\s/.test(before);
      const afterSpace = /\s/.test(after);
      const beforePunct = PUNCTUATION.test(before);
      const afterPunct = PUNCTUATION.test(after);
      const left = !afterSpace && (!afterPunct || beforeSpace || beforePunct);
      const right = !beforeSpace && (!beforePunct || afterSpace || afterPunct);
      const open = c === "_" ? left && (!right || beforePunct) : left;
      const close = c === "_" ? right && (!left || afterPunct) : right;
      flush();
      nodes.push({ t: "delim", ch: c, n, orig: n, open, close });
      i += n;
      continue;
    }
    text += c;
    i++;
  }
  flush();
  return nodes;
}

function resolveEmphasis(nodes) {
  let at = 0;
  while (at < nodes.length) {
    const closer = nodes[at];
    if (closer.t !== "delim" || !closer.close || closer.n === 0) {
      at++;
      continue;
    }
    let from = -1;
    for (let j = at - 1; j >= 0; j--) {
      const opener = nodes[j];
      if (opener.t !== "delim" || opener.ch !== closer.ch || !opener.open || opener.n === 0) continue;
      const both = (opener.close || closer.open) && (opener.orig + closer.orig) % 3 === 0;
      if (both && !(opener.orig % 3 === 0 && closer.orig % 3 === 0)) continue;
      from = j;
      break;
    }
    if (from < 0) {
      closer.close = false;
      at++;
      continue;
    }
    const opener = nodes[from];
    const use = closer.ch === "~" ? 2 : opener.n >= 2 && closer.n >= 2 ? 2 : 1;
    const kind = closer.ch === "~" ? "del" : use === 2 ? "strong" : "em";
    const kids = nodes.slice(from + 1, at);
    opener.n -= use;
    closer.n -= use;
    nodes.splice(from + 1, at - from - 1, { t: kind, kids });
    at = from + 2; // just after the new node, which is where the closer now is
    if (opener.n === 0) {
      nodes.splice(from, 1);
      at--;
    }
    if (nodes[at]?.n === 0) nodes.splice(at, 1);
  }
}

function settle(nodes) {
  const out = [];
  for (const node of nodes) {
    let next = node;
    if (node.t === "delim") next = { t: "text", v: node.ch.repeat(node.n) };
    else if (node.kids) next = { ...node, kids: settle(node.kids) };
    const last = out[out.length - 1];
    if (next.t === "text" && last && last.t === "text") last.v += next.v;
    else if (!(next.t === "text" && next.v === "")) out.push(next);
  }
  return out;
}

export function parseInline(src, inLink = false) {
  if (src.length > MAX_INLINE) return [{ t: "text", v: src }];
  const nodes = tokenize(src);
  resolveEmphasis(nodes);
  const done = settle(nodes);
  return inLink ? flattenLinks(done) : done;
}

function flattenLinks(nodes) {
  // a link inside a link label is only its text
  return nodes.flatMap((node) => {
    if (node.t === "link") return flattenLinks(node.kids);
    if (node.kids) return [{ ...node, kids: flattenLinks(node.kids) }];
    return [node];
  });
}

// ---------------------------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------------------------
function el(doc, tag, className, ...kids) {
  const node = doc.createElement(tag);
  if (className) node.setAttribute("class", className);
  for (const kid of kids) node.append(kid);
  return node;
}

const text = (doc, value) => doc.createTextNode(value);

function plain(nodes) {
  return nodes
    .map((n) => (n.kids ? plain(n.kids) : n.t === "text" || n.t === "code" ? n.v : n.t === "br" ? " " : ""))
    .join("");
}

function inlineNodes(doc, nodes) {
  const out = [];
  for (const node of nodes) {
    switch (node.t) {
      case "text":
        out.push(text(doc, node.v));
        break;
      case "br":
        out.push(el(doc, "br"));
        break;
      case "code": {
        const code = el(doc, "code", "inline");
        code.textContent = node.v;
        out.push(code);
        break;
      }
      case "strong":
      case "em":
      case "del":
        out.push(el(doc, node.t, "", ...inlineNodes(doc, node.kids)));
        break;
      case "link": {
        const href = safeUrl(node.href);
        const kids = inlineNodes(doc, node.kids);
        if (href === null) {
          out.push(...kids);
          break;
        }
        const link = el(doc, "a", "", ...kids);
        link.setAttribute("href", href);
        link.setAttribute("target", "_blank");
        link.setAttribute("rel", "noopener noreferrer nofollow");
        out.push(link);
        const url = new URL(href);
        if (url.protocol !== "mailto:" && plain(node.kids).trim() !== node.href.trim()) {
          // where it really goes, in case the words say something else
          const host = el(doc, "span", "host");
          host.textContent = url.hostname.replace(/^www\./, "");
          out.push(host);
        }
        break;
      }
      case "image": {
        // never loaded: only said what it was, and where
        const href = safeUrl(node.href);
        const label = el(doc, "span", "image-ref");
        label.textContent = node.alt ? `image: ${node.alt}` : "image";
        if (href) {
          const link = el(doc, "a", "", label);
          link.setAttribute("href", href);
          link.setAttribute("target", "_blank");
          link.setAttribute("rel", "noopener noreferrer nofollow");
          out.push(link);
        } else {
          out.push(label);
        }
        break;
      }
      default:
        break;
    }
  }
  return out;
}

function blockNodes(doc, blocks) {
  const out = [];
  for (const block of blocks) {
    switch (block.type) {
      case "p":
        out.push(el(doc, "p", "", ...inlineNodes(doc, block.inline)));
        break;
      case "h":
        out.push(el(doc, `h${Math.min(block.level + 1, 6)}`, "", ...inlineNodes(doc, block.inline)));
        break;
      case "hr":
        out.push(el(doc, "hr"));
        break;
      case "quote":
        out.push(el(doc, "blockquote", "", ...blockNodes(doc, block.blocks)));
        break;
      case "code": {
        const bar = el(doc, "div", "code-bar");
        const lang = el(doc, "span", "lang");
        lang.textContent = block.lang;
        const copy = el(doc, "button", "copy");
        copy.setAttribute("type", "button");
        copy.setAttribute("data-copy", "");
        copy.setAttribute("aria-label", "Copy code");
        copy.textContent = "Copy";
        bar.append(lang, copy);
        const code = el(doc, "code", "");
        code.textContent = block.text;
        out.push(el(doc, "div", "code", bar, el(doc, "pre", "", code)));
        break;
      }
      case "list": {
        const list = el(doc, block.ordered ? "ol" : "ul", "");
        if (block.ordered && block.start !== 1) list.setAttribute("start", String(block.start));
        for (const item of block.items) {
          const li = el(doc, "li", item.task ? `task ${item.task}` : "", ...blockNodes(doc, item.blocks));
          if (item.task) li.setAttribute("data-task", item.task);
          list.append(li);
        }
        out.push(list);
        break;
      }
      case "table": {
        const head = el(doc, "tr", "");
        block.head.forEach((cell, i) => {
          const th = el(doc, "th", block.align[i] ? `al-${block.align[i]}` : "", ...inlineNodes(doc, cell));
          head.append(th);
        });
        const body = el(doc, "tbody", "");
        for (const row of block.rows) {
          const tr = el(doc, "tr", "");
          row.forEach((cell, i) => {
            tr.append(el(doc, "td", block.align[i] ? `al-${block.align[i]}` : "", ...inlineNodes(doc, cell)));
          });
          body.append(tr);
        }
        const table = el(doc, "table", "", el(doc, "thead", "", head), body);
        out.push(el(doc, "div", "table-wrap", table));
        break;
      }
      case "cut": {
        const note = el(doc, "p", "cut");
        note.textContent = `… ${block.hidden.toLocaleString("en")} more characters not shown`;
        out.push(note);
        break;
      }
      default:
        break;
    }
  }
  return out;
}

/** A fragment of `doc` showing `source`. Nothing in it can run, load or navigate on its own. */
export function renderMarkdown(doc, source) {
  const fragment = doc.createDocumentFragment();
  for (const node of blockNodes(doc, parseMarkdown(source))) fragment.append(node);
  return fragment;
}
