// One navigation for the assistant and its tools. Only a layout preference is persisted;
// account, space, credentials and unfinished work remain in their existing owners.
export const PRODUCT_PAGES = Object.freeze([
  { id: "chat", label: "个人助理", path: "/", icon: "chat" },
  { id: "tasks", label: "任务", path: "/workspace?tab=tasks", icon: "tasks" },
  { id: "knowledge", label: "资料", path: "/workspace?tab=knowledge", icon: "knowledge" },
  { id: "agents", label: "助手与连接", path: "/workspace?tab=agents", icon: "agents" },
  { id: "spaces", label: "项目与成员", path: "/workspace?tab=spaces", icon: "spaces" },
  { id: "devices", label: "设备与账号", path: "/workspace?tab=devices", icon: "devices" },
]);

const PATHS = {
  menu: "M4 5h16v14H4zM9 5v14",
  close: "M6 6l12 12M18 6L6 18",
  chat: "M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 3V6a2 2 0 0 1 2-2zM7 9h10M7 13h6",
  tasks: "M9 5h11M9 12h11M9 19h11M3 5l1 1 2-2M3 12l1 1 2-2M3 19l1 1 2-2",
  knowledge: "M5 3h10l4 4v14H5zM14 3v5h5M8 12h8M8 16h6",
  agents: "M9 15l6-6M8 10l-2 2a4 4 0 0 0 6 6l2-2M10 8l2-2a4 4 0 0 1 6 6l-2 2",
  spaces: "M3 7V5h7l2 2h9v13H3z",
  devices: "M3 4h14v11H3zM8 15v5M5 20h6M18 9h4v12h-7v-4",
  settings: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2",
};

// A small, original mark made from four petals; decorative in every context.
export function createAgentMark(doc, className = "") {
  const mark = doc.createElement("span");
  mark.className = `product-mark ${className}`.trim();
  mark.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 4; index++) mark.append(doc.createElement("span"));
  return mark;
}

export function createProductShell({ document: doc = globalThis.document, content, current = "chat", resolve = path => path, onNavigate, spaceControl, onSettings }) {
  const view = doc.defaultView || globalThis;
  const node = (tag, text = "", className = "") => {
    const element = doc.createElement(tag);
    if (text) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const icon = name => {
    const svg = doc.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("class", "product-icon");
    const path = doc.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", PATHS[name]); svg.append(path); return svg;
  };
  const control = (label, symbol, className) => {
    const button = node("button", "", className);
    button.type = "button"; button.setAttribute("aria-label", label); button.title = label;
    button.append(icon(symbol)); return button;
  };
  const media = view.matchMedia?.("(max-width: 820px)");
  let narrow = Boolean(media?.matches), desktopOpen = true, alive = true;
  try {
    desktopOpen = view.localStorage?.getItem("pa-navigation") !== "collapsed";
    const theme = view.localStorage?.getItem("pa-theme");
    if (["auto", "light", "dark"].includes(theme)) doc.documentElement.dataset.theme = theme;
  } catch { /* Layout still works when browser storage is unavailable. */ }
  let open = !narrow && desktopOpen;
  const element = node("div", "", "product-layout");
  const drawer = node("aside", "", "product-drawer");
  drawer.id = "product-drawer"; drawer.setAttribute("aria-label", "个人助理导航");
  const toggle = control("打开菜单", "menu", "product-menu-toggle");
  toggle.setAttribute("aria-controls", drawer.id);
  const close = control("收起菜单", "menu", "product-drawer-close");
  const brand = node("a", "", "product-brand");
  brand.append(createAgentMark(doc), node("span", "Personal Agent"));
  brand.setAttribute("href", resolve("/"));
  const head = node("div", "", "product-drawer-head"); head.append(brand, close);
  drawer.append(head);
  if (spaceControl) {
    const context = node("div", "", "product-space");
    const label = node("label", "当前空间", "product-space-label");
    if (!spaceControl.id) spaceControl.id = "product-space-select";
    label.setAttribute("for", spaceControl.id);
    spaceControl.classList.add("product-space-select");
    context.append(label, spaceControl); drawer.append(context);
  }
  const nav = node("nav", "", "product-nav"); nav.setAttribute("aria-label", "主导航");
  const links = new Map();
  for (const page of PRODUCT_PAGES) {
    if (page.id === "tasks") nav.append(node("p", "我的空间", "product-nav-label"));
    const link = node("a", "", "product-nav-link");
    link.dataset.page = page.id;
    link.setAttribute("href", resolve(page.path));
    const symbol = node("span", "", "product-nav-symbol");
    symbol.append(icon(page.icon));
    link.append(symbol, node("span", page.label));
    link.addEventListener("click", event => {
      if (event.defaultPrevented || event.button > 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      if (onNavigate && page.id !== "chat") {
        event.preventDefault();
        if (narrow) setOpen(false);
        onNavigate(page.id);
      }
    });
    links.set(page.id, link); nav.append(link);
  }
  drawer.append(nav);
  const foot = node("div", "", "product-drawer-foot");
  if (onSettings) {
    const settings = node("button", "", "product-settings"); settings.type = "button";
    settings.append(icon("settings"), node("span", "偏好与设置"));
    settings.addEventListener("click", () => { if (narrow) setOpen(false); onSettings(); }); foot.append(settings);
  }
  const identity = node("div", "", "product-identity");
  const avatar = node("span", "P", "product-avatar"); avatar.setAttribute("aria-hidden", "true");
  const identityCopy = node("div", "", "product-identity-copy");
  const name = node("strong", "你的个人助理"), contextName = node("span", "从一句话开始");
  identityCopy.append(name, contextName); identity.append(avatar, identityCopy); foot.append(identity); drawer.append(foot);
  const overlay = node("div", "", "product-drawer-overlay"); overlay.setAttribute("aria-hidden", "true");
  if (content.parentNode) content.before(element);
  content.classList.add("product-main"); element.append(drawer, overlay, content);

  function paint() {
    element.dataset.drawerOpen = String(open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "收起菜单" : "打开菜单"); toggle.title = open ? "收起菜单" : "打开菜单";
    drawer.inert = !open;
    drawer.setAttribute("aria-hidden", String(!open));
    content.inert = narrow && open;
    doc.body.classList.toggle("product-menu-open", narrow && open);
    overlay.hidden = !narrow || !open;
    if (narrow && open) { drawer.setAttribute("role", "dialog"); drawer.setAttribute("aria-modal", "true"); }
    else { drawer.removeAttribute("role"); drawer.removeAttribute("aria-modal"); }
  }
  function setOpen(value, restore = true) {
    if (!alive) return;
    const wasOpen = open; open = Boolean(value);
    if (!narrow) {
      desktopOpen = open;
      try { view.localStorage?.setItem("pa-navigation", open ? "expanded" : "collapsed"); } catch { /* Optional preference. */ }
    }
    paint();
    if (narrow && open && !wasOpen) close.focus();
    else if (wasOpen && !open && restore) toggle.focus();
  }
  function setActive(key) {
    current = PRODUCT_PAGES.some(page => page.id === key) ? key : "chat";
    for (const page of PRODUCT_PAGES) {
      const link = links.get(page.id), active = page.id === current;
      link.classList.toggle("selected", active);
      link.setAttribute("href", resolve(page.path));
      if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    }
    brand.setAttribute("href", resolve("/"));
  }
  function setContext(context = {}) {
    name.textContent = context.name || "你的个人助理";
    avatar.textContent = Array.from(context.name || "P")[0];
    contextName.textContent = [context.space, context.role].filter(Boolean).join(" · ") || "从一句话开始";
    setActive(current);
  }
  const onKey = event => {
    if (!open) return;
    if (event.key === "Escape" && (narrow || drawer.contains(doc.activeElement))) {
      event.preventDefault(); setOpen(false); return;
    }
    if (event.key !== "Tab" || !narrow) return;
    const focusable = [...drawer.querySelectorAll("a[href],button,select,input,[tabindex]")].filter(item => !item.disabled && !item.hidden && !item.closest("[hidden]") && item.getAttribute("tabindex") !== "-1");
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (!first) return;
    if (event.shiftKey && (doc.activeElement === first || !drawer.contains(doc.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (doc.activeElement === last || !drawer.contains(doc.activeElement))) { event.preventDefault(); first.focus(); }
  };
  const onResize = () => {
    const focusedInside = drawer.contains(doc.activeElement);
    narrow = Boolean(media?.matches); open = !narrow && desktopOpen; paint();
    if (!open && focusedInside) toggle.focus();
  };
  toggle.addEventListener("click", () => setOpen(!open));
  close.addEventListener("click", () => setOpen(false));
  overlay.addEventListener("click", () => setOpen(false));
  doc.addEventListener("keydown", onKey); media?.addEventListener("change", onResize);
  setActive(current); paint();
  return { element, toggle, setActive, setContext, destroy() {
    alive = false; content.inert = false; doc.body.classList.remove("product-menu-open");
    doc.removeEventListener("keydown", onKey); media?.removeEventListener("change", onResize);
  } };
}
