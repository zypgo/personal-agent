// markdown-it supplies bounded syntax tokens. This renderer only constructs allowlisted DOM
// elements with textContent: it never consumes HTML and never loads a model-supplied image.
import { MAX_CHARS, parseInline, parseMarkdown, safeUrl } from "./markdown-parser.js";
export { MAX_CHARS, parseInline, parseMarkdown, safeUrl };

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
