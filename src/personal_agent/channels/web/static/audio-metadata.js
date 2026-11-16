// Reading a chosen file's duration works on HTTP too; it never requests a microphone or plays it.

/** Resolve to seconds, or undefined when this browser cannot read the file's metadata in time. */
export function audioDuration(file, {
  timeoutMs = 3000,
  Audio: AudioClass = globalThis.Audio,
  URL: urls = globalThis.URL,
  setTimeout: schedule = globalThis.setTimeout,
  clearTimeout: cancel = globalThis.clearTimeout,
} = {}) {
  return new Promise((resolve) => {
    let audio;
    let objectUrl;
    let timer;
    let settled = false;
    const safely = (action) => { try { action(); } catch { /* Cleanup must still reach the URL. */ } };
    const finish = (seconds) => {
      if (settled) return;
      settled = true;
      if (timer !== undefined) safely(() => cancel(timer));
      if (audio) {
        safely(() => audio.removeEventListener("loadedmetadata", metadata));
        safely(() => audio.removeEventListener("durationchange", metadata));
        safely(() => audio.removeEventListener("error", failed));
        safely(() => audio.removeEventListener("abort", failed));
        // Drop the decoder's reference before revoking the URL, including on timeout/error.
        safely(() => { audio.removeAttribute("src"); audio.load(); });
      }
      if (objectUrl !== undefined) safely(() => urls.revokeObjectURL(objectUrl));
      resolve(seconds);
    };
    const metadata = () => {
      const seconds = audio.duration;
      if (Number.isFinite(seconds) && seconds > 0) finish(seconds);
    };
    const failed = () => finish(undefined);
    try {
      audio = new AudioClass();
      audio.preload = "metadata";
      audio.autoplay = false;
      audio.addEventListener("loadedmetadata", metadata);
      audio.addEventListener("durationchange", metadata);
      audio.addEventListener("error", failed);
      audio.addEventListener("abort", failed);
      objectUrl = urls.createObjectURL(file);
      timer = schedule(failed, Number.isFinite(timeoutMs) && timeoutMs >= 0 ? timeoutMs : 3000);
      audio.src = objectUrl;
      if (!settled) audio.load();
    } catch {
      failed();
    }
  });
}
