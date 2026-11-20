// Sheets: how the page asks for a moment of attention. On a phone a sheet rises from the bottom
// edge and can be pulled back down with a finger; on a wide screen it is a centred dialog. Either
// way it leaves the way it came, and Escape, the backdrop and the close button all take that same
// way out. Nothing here knows what a sheet contains.

const LEAVE_MS = 420;
const FLICK = 0.55; // px per ms: a quick flick down dismisses even a short pull
const FAR = 110; // px: a slow pull has to go this far

const reduced = () => Boolean(globalThis.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches);
const wide = () => Boolean(globalThis.matchMedia?.("(min-width: 700px)")?.matches);

/** Close with the exit animation (or at once when motion is reduced). Safe to call twice. */
export function dismissSheet(dialog) {
  if (!dialog.open || dialog.dataset.closing === "true") return;
  if (reduced()) { dialog.close(); return; }
  dialog.dataset.closing = "true";
  dialog.className = `${dialog.className} closing`.trim();
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    dialog.removeEventListener("animationend", onEnd);
    delete dialog.dataset.closing;
    dialog.className = dialog.className.replace(/(?:^|\s)closing(?=\s|$)/g, "").trim();
    if (dialog.open) dialog.close();
  };
  // Animations of what is inside the sheet bubble up here; only the sheet's own ending counts.
  const onEnd = event => { if (event.target === dialog) finish(); };
  dialog.addEventListener("animationend", onEnd);
  setTimeout(finish, LEAVE_MS + 120);
}

/**
 * Show `dialog` modally and wire its ways out. `handles` are the parts a finger may grab to pull
 * the sheet down (the grabber and the title bar), never the scrolling content.
 */
export function presentSheet(dialog, { document: doc = globalThis.document, handles = [] } = {}) {
  dialog.addEventListener("cancel", event => { event.preventDefault(); dismissSheet(dialog); });
  dialog.addEventListener("click", event => { if (event.target === dialog) dismissSheet(dialog); });
  for (const handle of handles) pullDown(dialog, handle);
  doc.body.append(dialog);
  dialog.showModal();
}

function pullDown(dialog, handle) {
  let drag = null;
  const move = amount => {
    dialog.style.transition = "none";
    dialog.style.transform = amount ? `translateY(${amount}px)` : "";
    dialog.style.setProperty("--sheet-alpha", String(Math.max(0, 1 - amount / Math.max(dialog.offsetHeight, 1)).toFixed(3)));
  };
  handle.addEventListener("pointerdown", event => {
    if (event.pointerType === "mouse" || event.isPrimary === false || drag || wide() || dialog.dataset.closing === "true") return;
    // A header can contain the close button. Capturing its pointer at the header would
    // retarget the subsequent click there too, preventing the button from being activated.
    if (event.target?.closest?.("button,a,input,select,textarea,[role='button'],[contenteditable],[data-no-swipe]")) return;
    drag = { id: event.pointerId, y: event.clientY, at: event.timeStamp, last: event.clientY, lastAt: event.timeStamp, speed: 0, amount: 0 };
    handle.setPointerCapture?.(event.pointerId);
  });
  handle.addEventListener("pointermove", event => {
    if (!drag || event.pointerId !== drag.id) return;
    const raw = event.clientY - drag.y;
    // Pulling up resists, like a rubber band; pulling down follows the finger exactly.
    drag.amount = raw >= 0 ? raw : -Math.sqrt(-raw) * 1.5;
    const dt = event.timeStamp - drag.lastAt;
    if (dt > 0) drag.speed = (event.clientY - drag.last) / dt;
    drag.last = event.clientY; drag.lastAt = event.timeStamp;
    move(drag.amount);
  });
  const release = event => {
    if (!drag || event.pointerId !== drag.id) return;
    const { amount, speed } = drag;
    drag = null;
    if (event.type !== "pointercancel" && (amount > FAR || (amount > 24 && speed > FLICK))) { dismissSheet(dialog); return; }
    // Not far enough: settle back where it was.
    dialog.style.transition = `transform ${LEAVE_MS - 60}ms cubic-bezier(0.32, 0.72, 0, 1)`;
    dialog.style.transform = "";
    dialog.style.removeProperty("--sheet-alpha");
    setTimeout(() => { dialog.style.transition = ""; }, LEAVE_MS);
  };
  handle.addEventListener("pointerup", release);
  handle.addEventListener("pointercancel", release);
}
