// Small pure helpers: times, labels, ids. Nothing here touches the page, so Node can test it.

const pad2 = (n) => String(n).padStart(2, "0");

/** "14:05" or "2:05 PM", whichever the owner's locale writes. `seconds` is a Unix time. */
export function clockTime(seconds, locale) {
  return new Intl.DateTimeFormat(locale, { hour: "numeric", minute: "2-digit" }).format(
    new Date(seconds * 1000),
  );
}

/** The local calendar day of a Unix time, as "2026-11-15": the key days are grouped by. */
export function dayKey(seconds) {
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/** "Today", "Yesterday", or a short date. Days are calendar days (a night's sleep is not 24 h). */
export function dayLabel(seconds, nowSeconds, locale) {
  const key = dayKey(seconds);
  const now = new Date(nowSeconds * 1000);
  if (key === dayKey(nowSeconds)) return "Today";
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (key === dayKey(yesterday.getTime() / 1000)) return "Yesterday";
  const then = new Date(seconds * 1000);
  const options = { weekday: "short", day: "numeric", month: "short" };
  if (then.getFullYear() !== now.getFullYear()) options.year = "numeric";
  return new Intl.DateTimeFormat(locale, options).format(then);
}

/** m:ss for a length of time. */
export function durationLabel(seconds) {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${pad2(whole % 60)}`;
}

/** "1.4 MB". */
export function bytesLabel(bytes) {
  if (bytes < 1000) return `${bytes} B`;
  if (bytes < 1_000_000) return `${Math.round(bytes / 1000)} kB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

/** How long until something that expires at `expiresAt` does: "9 min", "2 h", or "expired". */
export function expiresIn(expiresAt, nowSeconds) {
  const left = Math.floor(expiresAt - nowSeconds);
  if (left <= 0) return "expired";
  if (left < 90) return `${left} s`;
  if (left < 5400) return `${Math.round(left / 60)} min`;
  if (left < 86400 * 2) return `${Math.round(left / 3600)} h`;
  return `${Math.round(left / 86400)} d`;
}

/** A pairing code out of whatever the owner pasted: the whole link, or just the code. */
export function parsePairing(text) {
  const value = String(text ?? "").trim();
  if (!value) return null;
  const inLink = /[#&?]pair=([A-Za-z0-9_-]{16,128})(?![A-Za-z0-9_-])/.exec(value);
  if (inLink) return inLink[1];
  return /^[A-Za-z0-9_-]{16,128}$/.test(value) ? value : null;
}

/** An id the server will accept (`[A-Za-z0-9_-]{8,64}`), so that sending twice is safe. */
export function newClientId(random = globalThis.crypto) {
  const bytes = new Uint8Array(12);
  random.getRandomValues(bytes);
  return Array.from(bytes, (b) => pad2(b.toString(16))).join("");
}

export const COMMANDS = [
  ["/new", "start a fresh conversation"],
  ["/status", "what is running, what it cost today"],
  ["/approvals", "actions waiting for your yes or no"],
  ["/approve", "allow a waiting action"],
  ["/deny", "refuse it"],
  ["/help", "every command"],
];

/** The commands that start with what has been typed, while it is still just a command word. */
export function commandHints(text, commands = COMMANDS) {
  if (!text.startsWith("/") || /\s/.test(text)) return [];
  const typed = text.toLowerCase();
  return commands.filter(([name]) => name.startsWith(typed) && name !== typed);
}

/** Cut a long text for a label, on a word if there is one near the end. */
export function clip(text, limit) {
  if (text.length <= limit) return text;
  const cut = text.slice(0, limit);
  const space = cut.lastIndexOf(" ");
  return (space > limit * 0.6 ? cut.slice(0, space) : cut).trimEnd() + "…";
}
