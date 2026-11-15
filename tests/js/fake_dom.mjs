// Just enough of a DOM for the page's renderers to build nodes into, and to look at afterwards.
// Anything that would turn text into markup (innerHTML and friends) throws: a renderer that used
// it would fail every test, which is the point.

const FORBIDDEN = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"];

export class FakeText {
  constructor(data) {
    this.nodeType = 3;
    this.data = String(data);
  }
  get textContent() {
    return this.data;
  }
}

export class FakeElement {
  constructor(tag, type = 1) {
    this.nodeType = type;
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.children = [];
    this.listeners = new Map();
  }
  setAttribute(name, value) {
    this.attrs.set(name, String(value));
  }
  getAttribute(name) {
    return this.attrs.has(name) ? this.attrs.get(name) : null;
  }
  hasAttribute(name) {
    return this.attrs.has(name);
  }
  append(...kids) {
    for (const kid of kids) {
      if (kid === null || kid === undefined || kid === false) continue;
      if (typeof kid === "string") this.children.push(new FakeText(kid));
      else if (kid.nodeType === 11) {
        this.children.push(...kid.children);
        kid.children = [];
      } else this.children.push(kid);
    }
  }
  appendChild(kid) {
    this.append(kid);
    return kid;
  }
  addEventListener(name, fn) {
    this.listeners.set(name, fn);
  }
  get textContent() {
    return this.children.map((c) => c.textContent).join("");
  }
  set textContent(value) {
    this.children = [new FakeText(value)];
  }
  get className() {
    return this.getAttribute("class") ?? "";
  }
  set className(value) {
    this.setAttribute("class", value);
  }
}

for (const name of FORBIDDEN) {
  Object.defineProperty(FakeElement.prototype, name, {
    get() {
      throw new Error(`${name} must not be read`);
    },
    set() {
      throw new Error(`${name} must not be used: text would become markup`);
    },
  });
}

export function makeDocument() {
  return {
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (text) => new FakeText(text),
    createDocumentFragment: () => new FakeElement("#fragment", 11),
  };
}

const VOID = new Set(["BR", "HR", "IMG", "INPUT"]);
const escapeText = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escapeAttr = (s) => escapeText(s).replace(/"/g, "&quot;");

/** What the nodes would look like as markup, with every piece of text escaped. */
export function toHtml(node) {
  if (node.nodeType === 3) return escapeText(node.data);
  const inner = node.children.map(toHtml).join("");
  if (node.nodeType === 11) return inner;
  const tag = node.tagName.toLowerCase();
  const attrs = [...node.attrs].map(([k, v]) => ` ${k}="${escapeAttr(v)}"`).join("");
  return VOID.has(node.tagName) ? `<${tag}${attrs}>` : `<${tag}${attrs}>${inner}</${tag}>`;
}

/** Every element below `node`, depth first. */
export function elements(node) {
  const out = [];
  for (const child of node.children ?? []) {
    if (child.nodeType === 1) {
      out.push(child, ...elements(child));
    }
  }
  return out;
}

export function find(node, tag) {
  return elements(node).filter((e) => e.tagName === tag.toUpperCase());
}
