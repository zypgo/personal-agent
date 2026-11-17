/** markdown-it parses syntax; the application's DOM renderer remains the security boundary.
 * No HTML renderer is called, no token-supplied tag or attribute is passed to the DOM, and image
 * tokens become text/link references. Limits apply both before parsing and while converting ASTs.
 */
import MarkdownIt, { type Token } from "markdown-it";

export const MAX_CHARS = 60_000;
const MAX_DEPTH = 6;
const MAX_INLINE = 20_000;
const MAX_TABLE_ROWS = 200;
const MAX_TABLE_COLUMNS = 20;

export type Inline =
  | { t: "text" | "code"; v: string }
  | { t: "br" }
  | { t: "strong" | "em" | "del"; kids: Inline[] }
  | { t: "link"; href: string; kids: Inline[] }
  | { t: "image"; href: string; alt: string };

type Paragraph = { type: "p"; source: string; inline: Inline[] };
type ListItem = { blocks: Block[]; task: "open" | "done" | null; number: number | null };
export type Block =
  | Paragraph
  | { type: "h"; level: number; inline: Inline[] }
  | { type: "hr" }
  | { type: "quote"; blocks: Block[] }
  | { type: "code"; lang: string; text: string }
  | { type: "list"; ordered: boolean; start: number; items: ListItem[] }
  | { type: "table"; align: ("left" | "center" | "right" | null)[]; head: Inline[][]; rows: Inline[][][] }
  | { type: "cut"; hidden: number };

const markdown = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
  typographer: false,
  // List and item tokens each add a parser level; DOM recursion is limited separately below.
  maxNesting: MAX_DEPTH * 3,
});
markdown.linkify.set({ fuzzyLink: false, fuzzyEmail: false, fuzzyIP: false });
// Recognize even a disallowed destination so its label can still be displayed as ordinary text.
// The URL never goes to HTML: safeUrl() is checked again by the DOM renderer for links/images.
markdown.validateLink = () => true;

export function safeUrl(raw: unknown): string | null {
  const value = String(raw ?? "").trim();
  if (!value || value.length > 2048 || /[\u0000-\u0020\u007f]/.test(value)) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

/** End of a balanced token group; never recurse to find the matching close. */
function closeAt(tokens: Token[], start: number, end = tokens.length): number {
  let level = 0;
  for (let index = start; index < end; index++) {
    level += tokens[index]?.nesting ?? 0;
    if (level === 0) return index;
  }
  return end;
}

function plainTokens(tokens: Token[], depth = 0): string {
  return tokens.map(token => {
    if (token.type === "softbreak" || token.type === "hardbreak") return "\n";
    if (token.children && depth < MAX_DEPTH) return plainTokens(token.children, depth + 1);
    return token.content;
  }).join("");
}

function inlineTokens(tokens: Token[], inLink = false, depth = 0): Inline[] {
  if (depth >= MAX_DEPTH) return [{ t: "text", v: plainTokens(tokens) }];
  const out: Inline[] = [];
  const push = (item: Inline) => {
    const previous = out.at(-1);
    if (item.t === "text" && previous?.t === "text") previous.v += item.v;
    else if (item.t !== "text" || item.v) out.push(item);
  };
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index]!;
    if (token.type === "text" || token.type === "html_inline") push({ t: "text", v: token.content });
    else if (token.type === "code_inline") push({ t: "code", v: token.content });
    else if (token.type === "softbreak" || token.type === "hardbreak") push({ t: "br" });
    else if (token.type === "image") {
      const alt = plainTokens(token.children ?? []) || token.content;
      if (inLink) push({ t: "text", v: alt ? `image: ${alt}` : "image" });
      else push({ t: "image", href: String(token.attrGet("src") ?? ""), alt });
    } else if (token.nesting === 1) {
      const end = closeAt(tokens, index);
      const kids = inlineTokens(tokens.slice(index + 1, end), inLink || token.type === "link_open", depth + 1);
      if (token.type === "link_open" && !inLink) push({ t: "link", href: String(token.attrGet("href") ?? ""), kids });
      else if (token.type === "strong_open") push({ t: "strong", kids });
      else if (token.type === "em_open") push({ t: "em", kids });
      else if (token.type === "s_open") push({ t: "del", kids });
      else for (const child of kids) push(child);
      index = end;
    } else if (token.nesting === 0 && token.content) push({ t: "text", v: token.content });
  }
  return out;
}

function inlineOf(token?: Token): Inline[] {
  if (!token) return [];
  if (token.content.length > MAX_INLINE) return [{ t: "text", v: token.content }];
  return inlineTokens(token.children ?? []);
}

export function parseInline(source: string, inLink = false): Inline[] {
  if (source.length > MAX_INLINE) return [{ t: "text", v: source }];
  const token = markdown.parseInline(source, {})[0];
  return inlineTokens(token?.children ?? [], inLink);
}

function tableOf(tokens: Token[], start: number, end: number): Extract<Block, { type: "table" }> {
  const table: Extract<Block, { type: "table" }> = { type: "table", align: [], head: [], rows: [] };
  let header = false;
  for (let index = start; index < end; index++) {
    const token = tokens[index]!;
    if (token.type === "thead_open") header = true;
    else if (token.type === "thead_close") header = false;
    else if (token.type === "tr_open") {
      const rowEnd = closeAt(tokens, index, end);
      if (!header && table.rows.length >= MAX_TABLE_ROWS) { index = rowEnd; continue; }
      const cells: Inline[][] = [];
      for (let cell = index + 1; cell < rowEnd && cells.length < MAX_TABLE_COLUMNS; cell++) {
        const opener = tokens[cell]!;
        if (opener.type !== "th_open" && opener.type !== "td_open") continue;
        cells.push(inlineOf(tokens[cell + 1]));
        if (header) {
          const alignment = /^text-align:(left|center|right)$/.exec(String(opener.attrGet("style") ?? ""))?.[1];
          table.align.push(alignment as "left" | "center" | "right" | undefined ?? null);
        }
        cell = closeAt(tokens, cell, rowEnd);
      }
      if (header) table.head = cells;
      else {
        while (cells.length < table.head.length) cells.push([]);
        table.rows.push(cells.slice(0, table.head.length));
      }
      index = rowEnd;
    }
  }
  return table;
}

function blocksOf(tokens: Token[], start: number, end: number, depth: number): Block[] {
  if (depth >= MAX_DEPTH) {
    const source = plainTokens(tokens.slice(start, end));
    return source ? [{ type: "p", source, inline: [{ t: "text", v: source }] }] : [];
  }
  const out: Block[] = [];
  for (let index = start; index < end; index++) {
    const token = tokens[index]!;
    if (token.type === "paragraph_open") {
      const inline = tokens[index + 1];
      out.push({ type: "p", source: inline?.content ?? "", inline: inlineOf(inline) });
      index = closeAt(tokens, index, end);
    } else if (token.type === "heading_open") {
      out.push({ type: "h", level: Number(token.tag.slice(1)), inline: inlineOf(tokens[index + 1]) });
      index = closeAt(tokens, index, end);
    } else if (token.type === "hr") out.push({ type: "hr" });
    else if (token.type === "fence" || token.type === "code_block") {
      out.push({ type: "code", lang: token.info.trim().split(/\s/, 1)[0]!.replace(/[^\w+#.-]/g, "").slice(0, 24), text: token.content.replace(/\n$/, "") });
    } else if (token.type === "blockquote_open") {
      const close = closeAt(tokens, index, end);
      out.push({ type: "quote", blocks: blocksOf(tokens, index + 1, close, depth + 1) });
      index = close;
    } else if (token.type === "bullet_list_open" || token.type === "ordered_list_open") {
      const close = closeAt(tokens, index, end);
      const ordered = token.type === "ordered_list_open";
      const first = ordered ? Number(token.attrGet("start") ?? 1) : 1;
      const items: ListItem[] = [];
      for (let item = index + 1; item < close; item++) {
        if (tokens[item]?.type !== "list_item_open") continue;
        const itemEnd = closeAt(tokens, item, close);
        const blocks = blocksOf(tokens, item + 1, itemEnd, depth + 1);
        const lead = blocks[0];
        let task: ListItem["task"] = null;
        if (lead?.type === "p") {
          const mark = /^\[( |x|X)\][ \t]+/.exec(lead.source);
          if (mark) {
            task = mark[1] === " " ? "open" : "done";
            lead.source = lead.source.slice(mark[0].length);
            lead.inline = parseInline(lead.source);
          }
        }
        items.push({ blocks, task, number: ordered ? first + items.length : null });
        item = itemEnd;
      }
      out.push({ type: "list", ordered, start: first, items });
      index = close;
    } else if (token.type === "table_open") {
      const close = closeAt(tokens, index, end);
      out.push(tableOf(tokens, index + 1, close));
      index = close;
    } else if (token.nesting === 0 && token.content) {
      out.push({ type: "p", source: token.content, inline: [{ t: "text", v: token.content }] });
    }
  }
  return out;
}

export function parseMarkdown(source: unknown): Block[] {
  const normalized = String(source ?? "").replace(/\r\n?/g, "\n");
  const text = normalized.slice(0, MAX_CHARS);
  let blocks: Block[];
  try {
    const tokens = markdown.parse(text, {});
    blocks = blocksOf(tokens, 0, tokens.length, 0);
  } catch {
    // A parser failure must not prevent access to the conversation; plain text is always safe.
    blocks = [{ type: "p", source: text, inline: [{ t: "text", v: text }] }];
  }
  if (normalized.length > MAX_CHARS) blocks.push({ type: "cut", hidden: normalized.length - MAX_CHARS });
  return blocks;
}
