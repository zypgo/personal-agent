// Pictures and voice, on the owner's side: shrinking a photo before it is sent, and recording.

const KEEP_AS_IS = 900_000; // a picture already this small is sent as it is
const MAX_EDGE = 2048; // a model reads a 2048-pixel photo as well as a 12-megapixel one

/**
 * A picture ready to send. Large photos (a phone's camera makes 3 to 12 MB) are redrawn smaller as
 * JPEG, which is what keeps a message quick over a phone connection and under the server's size
 * limit. Anything that cannot be redrawn here is sent unchanged, and the server decides.
 */
export async function shrinkImage(file, { maxBytes = 8_000_000 } = {}) {
  if (!file.type.startsWith("image/") || file.type === "image/gif") return file;
  if (file.size <= KEEP_AS_IS && file.size <= maxBytes) return file;
  try {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    context.fillStyle = "#fff"; // a transparent PNG becomes white, not black
    context.fillRect(0, 0, width, height);
    context.drawImage(bitmap, 0, 0, width, height);
    bitmap.close?.();
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.86));
    if (!blob || (blob.size >= file.size && file.size <= maxBytes)) return file;
    const name = (file.name || "photo").replace(/\.[^.]+$/, "") + ".jpg";
    return new File([blob], name, { type: "image/jpeg" });
  } catch {
    return file;
  }
}

/** Which recording format this browser can make, best first. Safari makes mp4, the rest webm. */
export function pickRecordingType(recorder = globalThis.MediaRecorder) {
  if (!recorder?.isTypeSupported) return "";
  const wanted = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  return wanted.find((type) => recorder.isTypeSupported(type)) ?? "";
}

export function extensionFor(mime) {
  const base = mime.split(";")[0].trim().toLowerCase();
  if (base.includes("mp4") || base.includes("m4a") || base.includes("aac")) return "m4a";
  if (base.includes("ogg")) return "ogg";
  if (base.includes("wav")) return "wav";
  return "webm";
}

/** Whether recording can work here at all: it needs a microphone and a secure address. */
export function canRecord() {
  return Boolean(
    globalThis.isSecureContext && globalThis.MediaRecorder && navigator.mediaDevices?.getUserMedia,
  );
}

/**
 * Start recording. Returns `{ stop(), cancel(), done }`: `stop()` ends it and `done` resolves with
 * `{ blob, seconds, name }`. The recording stops by itself at `maxSeconds`. The microphone is
 * released as soon as it ends, however it ends.
 */
export async function record({ maxSeconds, onTick }) {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const type = pickRecordingType();
  const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  const chunks = [];
  const started = performance.now();
  let cancelled = false;
  let timer = null;
  const release = () => {
    clearInterval(timer);
    for (const track of stream.getTracks()) track.stop();
  };
  const done = new Promise((resolve, reject) => {
    recorder.ondataavailable = (event) => {
      if (event.data?.size) chunks.push(event.data);
    };
    recorder.onerror = (event) => {
      release();
      reject(event.error ?? new Error("The recording failed."));
    };
    recorder.onstop = () => {
      release();
      if (cancelled) {
        resolve(null);
        return;
      }
      const mime = recorder.mimeType || type || "audio/webm";
      const blob = new Blob(chunks, { type: mime });
      resolve({ blob, seconds: (performance.now() - started) / 1000, name: `voice.${extensionFor(mime)}` });
    };
  });
  recorder.start(1000);
  timer = setInterval(() => {
    const seconds = (performance.now() - started) / 1000;
    onTick?.(seconds);
    if (seconds >= maxSeconds && recorder.state === "recording") recorder.stop();
  }, 250);
  return {
    done,
    stop: () => recorder.state === "recording" && recorder.stop(),
    cancel: () => {
      cancelled = true;
      if (recorder.state === "recording") recorder.stop();
      else release();
    },
  };
}
