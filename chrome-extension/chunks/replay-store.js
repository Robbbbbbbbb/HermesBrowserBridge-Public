const CHROME_PDF_VIEWER_ID = "mhjfbmdgcfjbbpaeojofohoefgiehjai";
const ATTACHABLE = { attachable: true };
function attachabilityOf(url) {
  if (!url) return ATTACHABLE;
  let scheme = "";
  try {
    scheme = new URL(url).protocol.toLowerCase().replace(/:$/, "");
  } catch {
    return ATTACHABLE;
  }
  if (scheme === "chrome-extension") {
    if (url.includes(CHROME_PDF_VIEWER_ID)) {
      return {
        attachable: false,
        reason: "this is a PDF, and Chrome renders PDFs inside its own built-in viewer, which the debugger cannot attach to"
      };
    }
    return { attachable: false, reason: "this is another extension's page, which Chrome will not let this one attach to" };
  }
  if (scheme === "chrome" || scheme === "chrome-untrusted" || scheme === "devtools" || scheme === "about") {
    return { attachable: false, reason: "Chrome blocks the debugger on its own internal pages" };
  }
  if (scheme === "view-source") {
    return { attachable: false, reason: "Chrome blocks the debugger on view-source: pages" };
  }
  if (/^https:\/\/(chromewebstore\.google\.com|chrome\.google\.com\/webstore)/.test(url)) {
    return { attachable: false, reason: "Chrome blocks extensions from acting on the Web Store" };
  }
  return ATTACHABLE;
}
const FOREIGN_EXTENSION_REFUSAL = /chrome-extension:\/\/ URL of different extension/i;
function isForeignExtensionRefusal(chromeMessage) {
  return FOREIGN_EXTENSION_REFUSAL.test(chromeMessage);
}
function foreignExtensionIds(frameUrls, ownId) {
  const ids = [];
  for (const url of frameUrls) {
    const match = /^chrome-extension:\/\/([a-p]{32})\//.exec(url);
    if (!match) continue;
    const id = match[1];
    if (id === ownId || ids.includes(id)) continue;
    ids.push(id);
  }
  return ids;
}
function summariseFrames(frameUrls) {
  const counts = /* @__PURE__ */ new Map();
  for (const url of frameUrls) {
    let label;
    try {
      const parsed = new URL(url);
      label = parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.origin : url.split(/[?#]/)[0];
    } catch {
      label = url === "" ? "(no url reported)" : url.split(/[?#]/)[0];
    }
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts].map(([label, n]) => n > 1 ? `${label} ×${n}` : label).join(", ") || "none";
}
function summariseFrameRefs(frames) {
  const labels = frames.map((frame) => {
    const origin = summariseFrames([frame.url]);
    const traits = [];
    if (frame.frameType === "sub_frame") traits.push("inside the page");
    else if (frame.frameType === "fenced_frame") traits.push("fenced frame");
    else if (frame.parentFrameId === -1 && frame.frameId !== 0) traits.push("separate page in this tab");
    if (frame.lifecycle === "prerender") traits.push("preloaded by Chrome");
    else if (frame.lifecycle === "cached") traits.push("held in back/forward cache");
    else if (frame.lifecycle === "pending_deletion") traits.push("being torn down");
    return traits.length ? `${origin} [${traits.join(", ")}]` : origin;
  });
  return labels.join(", ") || "none";
}
function unreportedParentCount(frames) {
  const reported = new Set(frames.map((frame) => frame.frameId));
  const missing = /* @__PURE__ */ new Set();
  for (const frame of frames) {
    if (frame.parentFrameId >= 0 && !reported.has(frame.parentFrameId)) missing.add(frame.parentFrameId);
  }
  return missing.size;
}
function describeFrameDiagnostics(diagnostics) {
  if (diagnostics.length === 0) return "";
  const noReply = diagnostics.filter((d) => !d.replied).length;
  const excess = diagnostics.filter((d) => d.replied).reduce((max, d) => Math.max(max, (d.windowLength ?? 0) - d.webNavigationChildCount, (d.elementCount ?? 0) - d.webNavigationChildCount), 0);
  const parts = [];
  if (noReply > 0) {
    parts.push(
      `the scan didn't run in ${noReply} of ${diagnostics.length} document${diagnostics.length === 1 ? "" : "s"} in this tab (no reply from its content script)`
    );
  }
  if (excess > 0) {
    parts.push(`page has ${excess} child frame${excess === 1 ? "" : "s"} that Chrome's frame list doesn't show`);
  }
  return parts.length > 0 ? ` (${parts.join("; ")})` : "";
}
function mergeIds(a, b) {
  const merged = [...a];
  for (const id of b) if (!merged.includes(id)) merged.push(id);
  return merged;
}
function foreignFrameExplanation(rawIds, frameUrls, unreportedParents, frameSummary, contentScan, diagnostics) {
  const ids = mergeIds(rawIds, contentScan?.extensionIds ?? []);
  if (ids.length === 0 && unreportedParents > 0) {
    return `a frame in this tab sits inside another frame that Chrome would not report to this extension (documents in this tab: ${frameSummary ?? summariseFrames(frameUrls)}). That hidden frame is what Chrome is refusing on, and another extension's injected frame is the usual thing hidden this way. To find which: turn off your other extensions at chrome://extensions and share again, then turn them back on one at a time until it fails`;
  }
  if (ids.length === 0) {
    const anyScanDidNotRun = (diagnostics ?? []).some((d) => !d.replied);
    const scanClause = contentScan && !anyScanDidNotRun ? " A page scan that also checks inside closed shadow roots found none either." : "";
    const diagnosticsClause = describeFrameDiagnostics(diagnostics ?? []);
    return `Chrome refused with "Cannot access a chrome-extension:// URL of different extension", but no other extension's frame was found in this tab (documents in this tab: ${frameSummary ?? summariseFrames(frameUrls)}).${scanClause}${diagnosticsClause} The cause is not confirmed. Password managers are the usual cause of this refusal, and their overlay can hide from every check here — present only while their own popup/dropdown is open, which this measurement may have missed. Try sharing the same address from a brand-new tab that has never shown anything else; if that also fails, turn off your other extensions at chrome://extensions and try again`;
  }
  const base = "another extension has put its own frame into this page, and Chrome won't let one extension's debugger attach to a page containing another extension's frame";
  const named = ids.map(
    (id) => id === CHROME_PDF_VIEWER_ID ? `Chrome's built-in PDF viewer (${id}), from a PDF embedded in the page` : `extension ${id} — chrome://extensions/?id=${id}`
  );
  const fix = ids.some((id) => id !== CHROME_PDF_VIEWER_ID) ? ` Open that link, set the extension's Site access to "On click" (or close that extension's popup/dropdown), reload the page, and share again.` : "";
  return `${base}. The frame belongs to ${named.join("; ")}.${fix}`;
}
function preloadedPageHint(frames) {
  const preloaded = frames.filter((frame) => frame.lifecycle === "prerender");
  if (preloaded.length === 0) return "";
  const origins = [...new Set(preloaded.map((frame) => summariseFrames([frame.url])))].join(", ");
  return ` Chrome has preloaded ${origins} in the background of this tab (visible to this extension only as a second document, never as something you navigated to), and another extension's frame inside that preloaded page can block the debugger this same way. This extension's Options page has a one-click fix for exactly this — "Turn off Chrome's page preloading so other extensions can't block sharing" — or you can do the same thing by hand in Settings → Performance → Preload pages. Alternatively, open this address in a fresh tab that has never shown anything else, then share again.`;
}
function describeAttachFailure(url, chromeMessage, frameUrls, ownId = "", unreportedParents = 0, frameSummary, contentScan, retryInfo, diagnostics) {
  const where = url ? ` (${url})` : "";
  const known = attachabilityOf(url);
  if (known.reason) return `Can't share this tab${where}: ${known.reason}.`;
  if (isForeignExtensionRefusal(chromeMessage) && frameUrls !== void 0) {
    const retriedNote = retryInfo && retryInfo.attempts > 1 ? `Retried ${retryInfo.attempts} times (this refusal is often transient) and still failed. ` : "";
    return `Can't share this tab${where}: ${retriedNote}${foreignFrameExplanation(foreignExtensionIds(frameUrls, ownId), frameUrls, unreportedParents, frameSummary, contentScan, diagnostics)}.`;
  }
  return `Can't share this tab${where}: ${chromeMessage}`;
}
function redactionMarker(kind) {
  return `[redacted:${kind}]`;
}
const ALL_ENABLED = {
  password: true,
  card: true,
  ssn: true,
  email: true,
  phone: true
};
function luhnCheck(digits) {
  if (!/^\d+$/.test(digits) || digits.length === 0) return false;
  let sum = 0;
  let double = false;
  for (let i = digits.length - 1; i >= 0; i--) {
    let n = digits.charCodeAt(i) - 48;
    if (double) {
      n *= 2;
      if (n > 9) n -= 9;
    }
    sum += n;
    double = !double;
  }
  return sum % 10 === 0;
}
const CARD_CANDIDATE = /\b\d(?:[\d][ -]?){11,22}\d\b/g;
const SSN_PATTERN = /\b\d{3}[- ]\d{2}[- ]\d{4}\b/g;
const EMAIL_PATTERN = /\b[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,8}[A-Za-z]{2,24}\b/g;
const PHONE_PATTERN = /\b(?:\+?1[-.\s])?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b/g;
function redactText(input, policy = ALL_ENABLED) {
  let count = 0;
  let out = input;
  if (policy.card) {
    out = out.replace(CARD_CANDIDATE, (match) => {
      const digits = match.replace(/[ -]/g, "");
      if (digits.length < 13 || digits.length > 19) return match;
      if (!luhnCheck(digits)) return match;
      count++;
      return redactionMarker("card");
    });
  }
  if (policy.ssn) {
    out = out.replace(SSN_PATTERN, () => {
      count++;
      return redactionMarker("ssn");
    });
  }
  if (policy.email) {
    out = out.replace(EMAIL_PATTERN, () => {
      count++;
      return redactionMarker("email");
    });
  }
  if (policy.phone) {
    out = out.replace(PHONE_PATTERN, () => {
      count++;
      return redactionMarker("phone");
    });
  }
  return { text: out, count };
}
function secretRedactionMarker() {
  return "[redacted:token]";
}
const JWT_PATTERN = /\beyJ[A-Za-z0-9_-]{4,2000}\.[A-Za-z0-9_-]{4,2000}\.[A-Za-z0-9_-]{4,2000}\b/g;
const BEARER_PATTERN = /\bBearer\s+[A-Za-z0-9\-_.~+/]{8,2000}=*/gi;
const API_KEY_PATTERN = /\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret[_-]?key|client[_-]?secret)\s*[:=]\s*['"]?[A-Za-z0-9\-_.]{12,512}['"]?/gi;
const URL_SECRET_PARAM_PATTERN = /([?&](?:access_token|token|api[_-]?key|apikey|secret|password)=)[^&#\s]{1,2000}/gi;
const AWS_ACCESS_KEY_PATTERN = /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g;
const ALREADY_TOKEN_REDACTED = "[redacted:token]";
function redactSecrets(input) {
  let count = 0;
  let out = input;
  out = out.replace(JWT_PATTERN, () => {
    count++;
    return secretRedactionMarker();
  });
  out = out.replace(BEARER_PATTERN, (match) => {
    if (match.includes(ALREADY_TOKEN_REDACTED)) return match;
    count++;
    return `Bearer ${secretRedactionMarker()}`;
  });
  out = out.replace(API_KEY_PATTERN, (match) => {
    if (match.includes(ALREADY_TOKEN_REDACTED)) return match;
    count++;
    const sep = match.match(/[:=]/)?.[0] ?? "=";
    const name = match.slice(0, match.indexOf(sep));
    return `${name}${sep}${secretRedactionMarker()}`;
  });
  out = out.replace(URL_SECRET_PARAM_PATTERN, (match, prefix) => {
    if (match.includes(ALREADY_TOKEN_REDACTED)) return match;
    count++;
    return `${prefix}${secretRedactionMarker()}`;
  });
  out = out.replace(AWS_ACCESS_KEY_PATTERN, () => {
    count++;
    return secretRedactionMarker();
  });
  return { text: out, count };
}
const REDACT_THEN_TRUNCATE_PREFIX_MULTIPLIER = 4;
const TRUNCATION_SUFFIX = "…[truncated]";
const TOKEN_TRIM_MAX_WALKBACK = 300;
const TRAILING_TOKEN_RUN = /[A-Za-z0-9+/=_.-]+$/;
function trimTrailingPartialToken(text) {
  const match = TRAILING_TOKEN_RUN.exec(text);
  if (!match) return text;
  const runLength = Math.min(match[0].length, TOKEN_TRIM_MAX_WALKBACK);
  return text.slice(0, text.length - runLength);
}
function redactThenTruncate(input, max, policy = ALL_ENABLED) {
  const prefixLen = max * REDACT_THEN_TRUNCATE_PREFIX_MULTIPLIER;
  const discardedTail = input.length > prefixLen;
  const prefix = discardedTail ? input.slice(0, prefixLen) : input;
  const pass1 = redactText(prefix, policy);
  const pass2 = redactSecrets(pass1.text);
  let text = pass2.text;
  let truncated = discardedTail;
  if (text.length > max) {
    truncated = true;
    text = trimTrailingPartialToken(text.slice(0, max));
  }
  if (truncated) text += TRUNCATION_SUFFIX;
  return { text, count: pass1.count + pass2.count, truncated };
}
const REPLAY_BYTE_CAP = 50 * 1024 * 1024;
function pickEvictions(existing, incomingBytes, retention, byteCap = REPLAY_BYTE_CAP) {
  const evicted = [];
  let count = existing.length;
  let totalBytes = existing.reduce((sum, r) => sum + r.bytes, 0) + incomingBytes;
  let i = 0;
  while (i < existing.length && (count > Math.max(0, retention - 1) || totalBytes > byteCap)) {
    evicted.push(existing[i].id);
    totalBytes -= existing[i].bytes;
    count--;
    i++;
  }
  return evicted;
}
class ReplayStore {
  backend;
  constructor(backend) {
    this.backend = backend;
  }
  /** Inserts one frame+action record, evicting the oldest existing records
   * first if needed to respect `retention` and the 50MB byte cap. Never
   * throws on a backend failure that isn't the caller's to handle —
   * background/replay-capture.ts's own caller already wraps this in a
   * best-effort try/catch (recording must never fail or slow down the
   * `page.act` it's attached to), so this method itself stays a thin,
   * unguarded pass-through: swallowing errors HERE would hide a real bug
   * from that outer catch's own logging. */
  async addFrame(record, retention) {
    const existing = await this.backend.listSizes();
    const evictIds = pickEvictions(existing, record.bytes, retention);
    if (evictIds.length > 0) await this.backend.deleteMany(evictIds);
    await this.backend.insert(record);
  }
  async listFrames() {
    return this.backend.getAll();
  }
  async clear() {
    await this.backend.clear();
  }
}
const DB_NAME = "hermes-replay";
const DB_VERSION = 1;
const STORE_NAME = "actions";
function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: "id", autoIncrement: true });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("indexedDB.open failed"));
  });
}
function reqToPromise(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("IDBRequest failed"));
  });
}
function txDone(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error ?? new Error("IDBTransaction failed"));
    tx.onabort = () => reject(tx.error ?? new Error("IDBTransaction aborted"));
  });
}
class IndexedDbBackend {
  async insert(record) {
    const db = await openDb();
    try {
      const tx = db.transaction(STORE_NAME, "readwrite");
      const store = tx.objectStore(STORE_NAME);
      const idReq = store.add(record);
      const [id] = await Promise.all([reqToPromise(idReq), txDone(tx)]);
      return id;
    } finally {
      db.close();
    }
  }
  async listSizes() {
    const db = await openDb();
    try {
      const tx = db.transaction(STORE_NAME, "readonly");
      const store = tx.objectStore(STORE_NAME);
      const out = [];
      await new Promise((resolve, reject) => {
        const cursorReq = store.openCursor();
        cursorReq.onsuccess = () => {
          const cursor = cursorReq.result;
          if (!cursor) return resolve();
          const value = cursor.value;
          out.push({ id: value.id, bytes: value.bytes });
          cursor.continue();
        };
        cursorReq.onerror = () => reject(cursorReq.error ?? new Error("cursor failed"));
      });
      return out;
    } finally {
      db.close();
    }
  }
  async deleteMany(ids) {
    if (ids.length === 0) return;
    const db = await openDb();
    try {
      const tx = db.transaction(STORE_NAME, "readwrite");
      const store = tx.objectStore(STORE_NAME);
      for (const id of ids) store.delete(id);
      await txDone(tx);
    } finally {
      db.close();
    }
  }
  async getAll() {
    const db = await openDb();
    try {
      const tx = db.transaction(STORE_NAME, "readonly");
      const store = tx.objectStore(STORE_NAME);
      const all = await reqToPromise(store.getAll());
      return all;
    } finally {
      db.close();
    }
  }
  async clear() {
    const db = await openDb();
    try {
      const tx = db.transaction(STORE_NAME, "readwrite");
      tx.objectStore(STORE_NAME).clear();
      await txDone(tx);
    } finally {
      db.close();
    }
  }
}
let sharedStore;
function getReplayStore() {
  if (!sharedStore) sharedStore = new ReplayStore(new IndexedDbBackend());
  return sharedStore;
}
function redactCaption(raw, policy) {
  return redactText(raw, policy).text;
}
export {
  REDACT_THEN_TRUNCATE_PREFIX_MULTIPLIER as R,
  attachabilityOf as a,
  redactCaption as b,
  secretRedactionMarker as c,
  describeAttachFailure as d,
  redactThenTruncate as e,
  foreignExtensionIds as f,
  getReplayStore as g,
  redactSecrets as h,
  isForeignExtensionRefusal as i,
  preloadedPageHint as p,
  redactText as r,
  summariseFrameRefs as s,
  trimTrailingPartialToken as t,
  unreportedParentCount as u
};
