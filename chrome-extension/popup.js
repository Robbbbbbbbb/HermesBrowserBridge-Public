import { s as send } from "./chunks/messages.js";
import { f as formatRefusal } from "./chunks/refusals.js";
import { s as setSettings, g as getSettings } from "./chunks/storage.js";
import { s as stopShortcutText } from "./chunks/pause.js";
import { g as getReplayStore, a as attachabilityOf } from "./chunks/replay-store.js";
async function sendApproval(message) {
  try {
    const response = await chrome.runtime.sendMessage(message);
    return response ?? { ok: false, error: "no listener responded" };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}
const CAPABILITY_LABEL = {
  snapshot: "read the page's structure",
  screenshot: "take a screenshot",
  read: "read the page's text",
  act: "click, type or submit on the page",
  cdp: "run a low-level browser command",
  fetch: "make a network request as this site",
  cookies: "read this site's cookies",
  network: "read recent network activity",
  open_tab: "open a new tab to this site",
  upload: "send a file to this site",
  evaluate: "run custom code on this page",
  cookies_write: "change this site's cookies",
  http_auth: "check this site's sign-in prompt",
  dialog: "accept a pop-up dialog on this page",
  downloads: "look at this site's download history",
  console: "read this page's browser console output"
};
const DANGEROUS_CAPABILITIES = /* @__PURE__ */ new Set([
  "upload",
  "evaluate",
  "cookies_write",
  "http_auth",
  "dialog",
  "downloads",
  "console"
]);
const NO_STANDING_GRANT_CAPABILITIES = /* @__PURE__ */ new Set(["evaluate", "upload", "http_auth"]);
function capabilityLabel(capability) {
  return CAPABILITY_LABEL[capability] ?? capability;
}
let activeTimers = [];
function clearActiveTimers() {
  for (const id of activeTimers) window.clearInterval(id);
  activeTimers = [];
}
function formatRemaining(ms) {
  if (ms <= 0) return "expired";
  const totalSeconds = Math.ceil(ms / 1e3);
  if (totalSeconds < 60) return `${totalSeconds}s left`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m ${seconds.toString().padStart(2, "0")}s left`;
}
function renderCard(approval, onResolved) {
  const card = document.createElement("li");
  card.className = "approval-card";
  const head = document.createElement("div");
  head.className = "approval-head";
  const originEl = document.createElement("span");
  originEl.className = "approval-origin";
  originEl.textContent = approval.origin;
  const ttlEl = document.createElement("span");
  ttlEl.className = "approval-ttl";
  head.append(originEl, ttlEl);
  const ask = document.createElement("p");
  ask.className = "approval-ask";
  ask.textContent = `Wants to ${capabilityLabel(approval.capability)}`;
  const summary = document.createElement("p");
  summary.className = "approval-summary";
  summary.textContent = approval.summary || "(no further detail provided)";
  const actions = document.createElement("div");
  actions.className = "approval-actions";
  let resolving = false;
  const respond = async (choice, confirmText) => {
    if (resolving) return;
    if (confirmText && !window.confirm(confirmText)) return;
    resolving = true;
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    try {
      await sendApproval({ target: "offscreen", type: "approvals.respond", approvalId: approval.approvalId, choice });
    } finally {
      onResolved();
    }
  };
  const noStandingGrant = NO_STANDING_GRANT_CAPABILITIES.has(approval.capability);
  const isDangerous = DANGEROUS_CAPABILITIES.has(approval.capability);
  const onceButton = document.createElement("button");
  onceButton.className = "approval-once";
  onceButton.textContent = "Approve once";
  onceButton.title = "Allow this one request. Nothing changes for next time.";
  onceButton.addEventListener("click", () => void respond("once"));
  const denyButton = document.createElement("button");
  denyButton.className = "danger approval-deny";
  denyButton.textContent = "Deny";
  denyButton.addEventListener("click", () => void respond("deny"));
  actions.append(onceButton);
  let sessionButton = null;
  if (!noStandingGrant) {
    sessionButton = document.createElement("button");
    sessionButton.className = "approval-session";
    sessionButton.textContent = "Approve for this session";
    sessionButton.title = "Allow this and matching requests from the same Hermes session, without asking again.";
    sessionButton.addEventListener("click", () => void respond("session"));
    actions.append(sessionButton);
  }
  actions.append(denyButton);
  const alwaysRow = document.createElement("div");
  alwaysRow.className = "approval-always-row";
  const alwaysNote = document.createElement("p");
  alwaysNote.className = "approval-always-note";
  if (noStandingGrant) {
    alwaysNote.textContent = formatRefusal("once_only_capability", {
      capability: capabilityLabel(approval.capability)
    }).message;
    alwaysRow.append(alwaysNote);
  } else {
    const alwaysButton = document.createElement("button");
    alwaysButton.className = "approval-always";
    alwaysButton.textContent = `Always allow ${approval.origin} to ${capabilityLabel(approval.capability)}`;
    const confirmText = isDangerous ? `This lets ${approval.origin} ${capabilityLabel(approval.capability)} from now on, without asking again, until you change it back in this popup. It does NOT grant any OTHER capability -- only this one. Continue?` : `This sets ${approval.origin} to FULL access for this device -- every capability, every future request, with no further approvals -- until you change it back in this popup. Continue?`;
    alwaysButton.addEventListener("click", () => void respond("always", confirmText));
    alwaysNote.textContent = isDangerous ? "This is a standing grant for this capability only, not full access to the site." : "This is a standing grant, not a one-time approval.";
    alwaysRow.append(alwaysButton, alwaysNote);
  }
  card.append(head, ask, summary, actions, alwaysRow);
  const tick = () => {
    const remaining = approval.expiresAt - Date.now();
    ttlEl.textContent = formatRemaining(remaining);
    ttlEl.classList.toggle("approval-ttl-low", remaining > 0 && remaining < 15e3);
  };
  tick();
  activeTimers.push(window.setInterval(tick, 1e3));
  return card;
}
function renderApprovals(container, emptyEl, countEl, approvals, onResolved) {
  clearActiveTimers();
  container.innerHTML = "";
  emptyEl.hidden = approvals.length > 0;
  if (countEl) countEl.textContent = approvals.length > 0 ? String(approvals.length) : "";
  for (const approval of approvals) {
    container.append(renderCard(approval, onResolved));
  }
}
async function fetchPendingApprovals() {
  const response = await sendApproval({ target: "offscreen", type: "approvals.list" });
  return response.ok ? response.data?.approvals ?? [] : [];
}
function holdsFocus(container, active) {
  return active !== null && container.contains(active);
}
class RenderGuard {
  signatures = /* @__PURE__ */ new Map();
  busy = /* @__PURE__ */ new Set();
  /** Declare that the user is interacting with this list right now.
   *
   * The focus check below is not sufficient on its own, for two reasons found
   * in review:
   *
   *   1. The mode picker's own change handler sets `select.disabled = true`
   *      while the grant round-trips to the gateway. A disabled form control
   *      cannot hold focus, so `document.activeElement` moves off it — and a
   *      poll landing in that window would rebuild the list and destroy the
   *      very <select> the user had just used, leaving the pending re-enable
   *      to fire on a detached node.
   *   2. The focus check assumes an open <select> keeps `activeElement`
   *      pointed at itself. That is standard behaviour and almost certainly
   *      true, but nothing in this codebase proves it, and the whole fix would
   *      be theatre if it were false. Marking busy from real interaction
   *      events does not depend on that premise at all.
   *
   * Always paired with clearBusy in a finally, so a thrown handler cannot
   * freeze a list permanently.
   */
  markBusy(key) {
    this.busy.add(key);
  }
  clearBusy(key) {
    this.busy.delete(key);
  }
  shouldRender(key, container, signature, active) {
    if (this.signatures.get(key) === signature) return false;
    if (this.busy.has(key)) return false;
    if (holdsFocus(container, active)) return false;
    this.signatures.set(key, signature);
    return true;
  }
  /** Test seam: what signature was last actually rendered for `key`. */
  lastRendered(key) {
    return this.signatures.get(key);
  }
}
function effectiveMode(origin, status) {
  const grant = status.grants?.find((entry) => entry.origin === origin);
  if (grant) return { mode: grant.mode, explicit: true };
  return { mode: status.defaultMode ?? "", explicit: false };
}
function shouldShowCancel(state) {
  if (state === "connected") return false;
  return state === "connecting" || state === "pairing";
}
function deviceLabel(status) {
  const title = status.deviceId ? `device id: ${status.deviceId}` : "";
  if (status.deviceName) return { text: status.deviceName, title };
  if (status.deviceId) return { text: "(unnamed)", title };
  return { text: "—", title };
}
function statusChipState(status) {
  const connected = status.state === "connected";
  const failed = status.state === "error" || !connected && Boolean(status.lastError);
  const parts = [status.state === "connected" ? "Connected" : status.state];
  if (status.paused) parts.push("sharing paused");
  if (status.lastError) parts.push(status.lastError);
  return {
    compact: connected,
    bad: failed && !connected,
    title: parts.join(" — ")
  };
}
const LOCAL_FILE_HEADER_SIG = 67324752;
const CENTRAL_DIR_HEADER_SIG = 33639248;
const END_OF_CENTRAL_DIR_SIG = 101010256;
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 3988292384 ^ c >>> 1 : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();
function crc32(data) {
  let crc = 4294967295;
  for (let i = 0; i < data.length; i++) {
    crc = CRC_TABLE[(crc ^ data[i]) & 255] ^ crc >>> 8;
  }
  return (crc ^ 4294967295) >>> 0;
}
class ByteWriter {
  chunks = [];
  len = 0;
  push(bytes) {
    this.chunks.push(bytes);
    this.len += bytes.length;
  }
  u16(value) {
    const b = new Uint8Array(2);
    new DataView(b.buffer).setUint16(0, value, true);
    this.push(b);
  }
  u32(value) {
    const b = new Uint8Array(4);
    new DataView(b.buffer).setUint32(0, value >>> 0, true);
    this.push(b);
  }
  bytes() {
    const out = new Uint8Array(this.len);
    let offset = 0;
    for (const chunk of this.chunks) {
      out.set(chunk, offset);
      offset += chunk.length;
    }
    return out;
  }
}
const DOS_TIME = 0;
const DOS_DATE = 33;
function buildZip(entries) {
  const encoder = new TextEncoder();
  const out = new ByteWriter();
  const central = new ByteWriter();
  let offset = 0;
  let centralSize = 0;
  let centralCount = 0;
  for (const entry of entries) {
    const nameBytes = encoder.encode(entry.name);
    const crc = crc32(entry.data);
    const localOffset = offset;
    const local = new ByteWriter();
    local.u32(LOCAL_FILE_HEADER_SIG);
    local.u16(20);
    local.u16(0);
    local.u16(0);
    local.u16(DOS_TIME);
    local.u16(DOS_DATE);
    local.u32(crc);
    local.u32(entry.data.length);
    local.u32(entry.data.length);
    local.u16(nameBytes.length);
    local.u16(0);
    local.push(nameBytes);
    local.push(entry.data);
    const localBytes = local.bytes();
    out.push(localBytes);
    offset += localBytes.length;
    central.u32(CENTRAL_DIR_HEADER_SIG);
    central.u16(20);
    central.u16(20);
    central.u16(0);
    central.u16(0);
    central.u16(DOS_TIME);
    central.u16(DOS_DATE);
    central.u32(crc);
    central.u32(entry.data.length);
    central.u32(entry.data.length);
    central.u16(nameBytes.length);
    central.u16(0);
    central.u16(0);
    central.u16(0);
    central.u16(0);
    central.u32(0);
    central.u32(localOffset);
    central.push(nameBytes);
    centralCount++;
  }
  const centralBytes = central.bytes();
  centralSize = centralBytes.length;
  const centralOffset = offset;
  const end = new ByteWriter();
  end.u32(END_OF_CENTRAL_DIR_SIG);
  end.u16(0);
  end.u16(0);
  end.u16(centralCount);
  end.u16(centralCount);
  end.u32(centralSize);
  end.u32(centralOffset);
  end.u16(0);
  const final = new ByteWriter();
  final.push(out.bytes());
  final.push(centralBytes);
  final.push(end.bytes());
  return final.bytes();
}
const el$1 = (id) => document.getElementById(id);
function formatReplayCaption(action, index, total) {
  const when = new Date(action.time).toLocaleTimeString();
  const outcome = action.outcome === "error" ? " (failed)" : "";
  const target = action.target ? ` — ${action.target}` : "";
  return `${index + 1}/${total} · ${when} · ${action.kind}${target}${outcome}`;
}
function pad4(n) {
  return String(n).padStart(4, "0");
}
function buildReplayExportEntries(actions, frameBytes) {
  const manifest = actions.map((a, i) => ({
    frame: `frame_${pad4(i)}.jpg`,
    time: a.time,
    tabId: a.tabId,
    kind: a.kind,
    target: a.target ?? null,
    outcome: a.outcome
  }));
  const entries = actions.map((_, i) => ({ name: `frame_${pad4(i)}.jpg`, data: frameBytes[i] }));
  entries.push({ name: "actions.json", data: new TextEncoder().encode(JSON.stringify(manifest, null, 2)) });
  return entries;
}
function base64ToBytes(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
function initReplayUI() {
  const toggle = el$1("replayToggle");
  const view = el$1("replayView");
  const scrubber = el$1("replayScrubber");
  const frame = el$1("replayFrame");
  const caption = el$1("replayCaption");
  const empty = el$1("replayEmpty");
  const exportButton = el$1("replayExport");
  const clearButton = el$1("replayClear");
  if (!toggle || !view) return;
  let actions = [];
  function showFrame(index) {
    const action = actions[index];
    if (!action) return;
    if (frame) frame.src = `data:image/jpeg;base64,${action.image}`;
    if (caption) caption.textContent = formatReplayCaption(action, index, actions.length);
  }
  async function refresh2() {
    actions = await getReplayStore().listFrames();
    const hasFrames = actions.length > 0;
    if (empty) empty.hidden = hasFrames;
    if (scrubber) {
      scrubber.hidden = !hasFrames;
      scrubber.min = "0";
      scrubber.max = String(Math.max(0, actions.length - 1));
      scrubber.value = String(Math.max(0, actions.length - 1));
    }
    if (frame) frame.hidden = !hasFrames;
    if (caption) caption.hidden = !hasFrames;
    if (exportButton) exportButton.disabled = !hasFrames;
    if (clearButton) clearButton.disabled = !hasFrames;
    if (hasFrames) showFrame(actions.length - 1);
  }
  toggle.addEventListener("click", () => {
    view.hidden = !view.hidden;
    if (!view.hidden) void refresh2();
  });
  scrubber?.addEventListener("input", () => {
    showFrame(Number(scrubber.value));
  });
  exportButton?.addEventListener("click", () => {
    void (async () => {
      if (actions.length === 0) return;
      const frameBytes = actions.map((a) => base64ToBytes(a.image));
      const zipBytes = buildZip(buildReplayExportEntries(actions, frameBytes));
      const blob = new Blob([zipBytes.buffer], { type: "application/zip" });
      const url = URL.createObjectURL(blob);
      try {
        await chrome.downloads.download({
          url,
          filename: `hermes-replay-${Date.now()}.zip`,
          saveAs: true
        });
      } finally {
        setTimeout(() => URL.revokeObjectURL(url), 6e4);
      }
    })();
  });
  clearButton?.addEventListener("click", () => {
    void (async () => {
      await getReplayStore().clear();
      await refresh2();
    })();
  });
}
const el = (id) => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing element: ${id}`);
  return node;
};
const dot = el("dot");
const statusChip = el("statusChip");
const statusText = el("statusText");
const statusDetail = el("statusDetail");
const errorBox = el("errorBox");
const cancelConnectRow = el("cancelConnectRow");
const cancelConnectButton = el("cancelConnectButton");
const pairSection = el("pairSection");
const pairedSection = el("pairedSection");
const pauseButton = el("pauseButton");
const pauseButtonLabel = el("pauseButtonLabel");
const stopShortcutNote = el("stopShortcutNote");
const SHORTCUT_SYMBOL = {
  Alt: "⌥",
  Option: "⌥",
  Shift: "⇧",
  Ctrl: "⌃",
  Control: "⌃",
  MacCtrl: "⌃",
  Command: "⌘",
  Cmd: "⌘"
};
function isMacPlatform() {
  const uaPlatform = navigator.userAgentData?.platform;
  return /mac/i.test(uaPlatform ?? navigator.platform ?? navigator.userAgent);
}
function shortcutChipText(shortcut, mac = isMacPlatform()) {
  if (!shortcut) return "";
  if (!mac) return shortcut;
  return shortcut.split("+").map((part) => SHORTCUT_SYMBOL[part] ?? part).join("");
}
void chrome.commands.getAll().then((commands) => {
  const shortcut = commands.find((c) => c.name === "stop-hermes")?.shortcut ?? "";
  stopShortcutNote.textContent = shortcutChipText(shortcut);
  stopShortcutNote.hidden = !shortcut;
  pauseButton.title = stopShortcutText(shortcut);
}).catch(() => {
});
const optionsButton = el("optionsButton");
optionsButton.addEventListener("click", () => void chrome.runtime.openOptionsPage());
const onboardStepGateway = el("onboardStepGateway");
const onboardStepPair = el("onboardStepPair");
const onboardGatewayUrl = el("onboardGatewayUrl");
const onboardGatewayContinue = el("onboardGatewayContinue");
const onboardBackToGateway = el("onboardBackToGateway");
const onboardGatewayDisplay = el("onboardGatewayDisplay");
const copyPairCommand = el("copyPairCommand");
const pairCode = el("pairCode");
const deviceName = el("deviceName");
const pairButton = el("pairButton");
const reconnectButton = el("reconnectButton");
const shareActiveButton = el("shareActiveButton");
const shareActiveInfo = el("shareActiveInfo");
const shareError = el("shareError");
const shareTabsList = el("shareTabsList");
const shareTabsEmpty = el("shareTabsEmpty");
const shareTabsMore = el("shareTabsMore");
const attachedTabsList = el("attachedTabsList");
const attachedTabsEmpty = el("attachedTabsEmpty");
const httpAuthArmedList = el("httpAuthArmedList");
const httpAuthArmedEmpty = el("httpAuthArmedEmpty");
const httpAuthDisabledNote = el("httpAuthDisabledNote");
const httpAuthForm = el("httpAuthForm");
const httpAuthOrigin = el("httpAuthOrigin");
const httpAuthUsername = el("httpAuthUsername");
const httpAuthPassword = el("httpAuthPassword");
const httpAuthTtlMinutes = el("httpAuthTtlMinutes");
const httpAuthArmButton = el("httpAuthArmButton");
const httpAuthError = el("httpAuthError");
const approvalList = el("approvalList");
const approvalEmpty = el("approvalEmpty");
const approvalCount = document.getElementById("approvalCount");
const waitingApprovalHead = el("waitingApprovalHead");
const tabsApprovalBadge = document.getElementById("tabsApprovalBadge");
const tabsCountLabel = el("tabsCountLabel");
const gatewayUrlLine = el("gatewayUrlLine");
const hostReconnectButton = el("hostReconnectButton");
const footerDot = el("footerDot");
const footerGatewayHost = el("footerGatewayHost");
const footerLogs = el("footerLogs");
const footerShortcuts = el("footerShortcuts");
const footerDocs = el("footerDocs");
const MODES = ["off", "request", "full"];
const MODE_LABEL = { off: "Off", request: "Ask", full: "Full", "": "—" };
const guard = new RenderGuard();
function shouldRender(key, container, signature) {
  return guard.shouldRender(key, container, signature, document.activeElement);
}
const STATE_LABEL = {
  connected: "Live",
  connecting: "Connecting…",
  pairing: "Pairing…",
  disconnected: "Disconnected",
  error: "Not connected"
};
function pairedStatusDetail(status) {
  if (status.state === "connected") {
    return status.lastHeartbeatAt ? `heartbeat ${Math.round((Date.now() - status.lastHeartbeatAt) / 1e3)}s ago` : status.gatewayUrl.replace(/^wss?:\/\//, "");
  }
  return `paired, not connected — ${status.gatewayUrl.replace(/^wss?:\/\//, "")}`;
}
function dotColorClass(status) {
  if (status.paused) return "dot-warn";
  if (status.state === "connected") return "dot-ok";
  if (status.state === "connecting" || status.state === "pairing") return "dot-warn";
  return "dot-bad";
}
function gatewayHostPort(gatewayUrl) {
  try {
    const parsed = new URL(gatewayUrl);
    return parsed.port ? `${parsed.hostname}:${parsed.port}` : parsed.hostname;
  } catch {
    return gatewayUrl.replace(/^wss?:\/\//, "").split("/")[0] || "—";
  }
}
function render(status) {
  statusText.textContent = STATE_LABEL[status.state];
  statusDetail.textContent = status.paired ? pairedStatusDetail(status) : "not paired";
  const chip = statusChipState(status);
  dot.className = `dot ${dotColorClass(status)}`;
  const chipTitle = chip.title || statusDetail.textContent;
  statusChip.title = chipTitle;
  statusChip.setAttribute("aria-label", chipTitle);
  shareActiveButton.hidden = !status.paired;
  errorBox.hidden = !status.lastError;
  errorBox.textContent = status.lastError;
  cancelConnectRow.hidden = !shouldShowCancel(status.state);
  pairSection.hidden = status.paired;
  pairedSection.hidden = !status.paired;
  const device = deviceLabel(status);
  deviceName.textContent = device.text;
  deviceName.title = device.title;
  gatewayUrlLine.textContent = status.gatewayUrl || "—";
  footerDot.className = `dot ${status.state}`;
  footerGatewayHost.textContent = status.paired ? gatewayHostPort(status.gatewayUrl) : "not paired";
  pauseButtonLabel.textContent = status.paused ? "Resume Sharing" : "Pause Sharing";
  pauseButton.classList.toggle("active", status.paused);
}
function tabLabel(tab) {
  return tab.title?.trim() || tab.url || `tab ${tab.tabId}`;
}
function releaseButton(tabId) {
  const button = document.createElement("button");
  button.textContent = "Release";
  button.className = "release-button danger-outline";
  button.addEventListener("click", async () => {
    button.disabled = true;
    await send({ target: "background", type: "tabs.release", tabId });
    await refresh();
  });
  return button;
}
function modePill(origin, status) {
  const current = effectiveMode(origin, status);
  const pill = document.createElement("button");
  pill.type = "button";
  pill.className = "mode-pill";
  pill.textContent = MODE_LABEL[current.mode];
  pill.title = current.mode === "" ? "not reported yet" : current.explicit ? "set for this site — click to change" : "default access — click to change";
  pill.setAttribute("aria-label", `Access mode for ${origin}: ${MODE_LABEL[current.mode]}`);
  pill.addEventListener("click", async () => {
    guard.markBusy("attachedTabs");
    pill.disabled = true;
    try {
      const from = current.mode === "" ? MODES[0] : current.mode;
      const next = MODES[(MODES.indexOf(from) + 1) % MODES.length];
      await send({ target: "offscreen", type: "grant.set", origin, mode: next });
    } finally {
      pill.disabled = false;
      guard.clearBusy("attachedTabs");
      await refresh();
    }
  });
  return pill;
}
function renderAttachedTabs(tabs, status, activeTabId) {
  const signature = JSON.stringify(
    tabs.map((tab) => [
      tab.tabId,
      tabLabel(tab),
      tab.origin,
      tab.attachMode ?? "",
      tab.origin ? effectiveMode(tab.origin, status) : null,
      tab.tabId === activeTabId
    ])
  );
  if (!shouldRender("attachedTabs", attachedTabsList, signature)) {
    attachedTabsEmpty.hidden = tabs.length > 0;
    return;
  }
  attachedTabsList.innerHTML = "";
  attachedTabsEmpty.hidden = tabs.length > 0;
  for (const tab of tabs) {
    const row = document.createElement("li");
    row.className = "attached-card";
    const check = document.createElement("span");
    check.className = "attached-check";
    check.setAttribute("aria-hidden", "true");
    check.textContent = "✓";
    const info = document.createElement("div");
    info.className = "attached-info";
    const titleRow = document.createElement("div");
    titleRow.className = "attached-title-row";
    const title = document.createElement("span");
    title.className = "attached-title";
    title.textContent = tabLabel(tab);
    titleRow.append(title);
    if (tab.tabId === activeTabId) {
      const activeBadge = document.createElement("span");
      activeBadge.className = "active-badge";
      activeBadge.textContent = "Active";
      titleRow.append(activeBadge);
    }
    const origin = document.createElement("div");
    origin.className = "attached-origin";
    origin.textContent = tab.origin || "—";
    info.append(titleRow, origin);
    if (tab.attachMode === "limited") {
      const badge = document.createElement("div");
      badge.className = "tab-mode-badge";
      badge.textContent = "Limited mode (another extension is blocking full control)";
      if (tab.attachModeReason) badge.title = tab.attachModeReason;
      info.append(badge);
    }
    row.append(check, info);
    if (tab.origin) row.append(modePill(tab.origin, status));
    row.append(releaseButton(tab.tabId));
    attachedTabsList.append(row);
  }
}
function setHttpAuthError(message) {
  httpAuthError.hidden = !message;
  httpAuthError.textContent = message ?? "";
}
function formatExpiry(expiresAt) {
  const remainingMs = expiresAt - Date.now();
  if (remainingMs <= 0) return "expiring…";
  const minutes = Math.round(remainingMs / 6e4);
  return minutes < 1 ? "expires in under a minute" : `expires in ${minutes} min`;
}
function disarmButton(origin) {
  const button = document.createElement("button");
  button.textContent = "Disarm";
  button.className = "tab-release";
  button.addEventListener("click", async () => {
    button.disabled = true;
    await send({ target: "background", type: "auth.disarm", origin });
    await refreshHttpAuth();
  });
  return button;
}
function renderHttpAuthArmed(armed) {
  const signature = JSON.stringify(armed);
  if (!shouldRender("httpAuthArmed", httpAuthArmedList, signature)) {
    httpAuthArmedEmpty.hidden = armed.length > 0;
    return;
  }
  httpAuthArmedList.innerHTML = "";
  httpAuthArmedEmpty.hidden = armed.length > 0;
  for (const entry of armed) {
    const row = document.createElement("li");
    row.className = "tab-row";
    const info = document.createElement("div");
    info.className = "tab-info";
    const label = document.createElement("div");
    label.className = "tab-origin";
    label.textContent = entry.origin;
    const expiry = document.createElement("div");
    expiry.className = "mode-source";
    expiry.textContent = formatExpiry(entry.expiresAt);
    info.append(label, expiry);
    row.append(info, disarmButton(entry.origin));
    httpAuthArmedList.append(row);
  }
}
async function refreshHttpAuth() {
  const settings = await getSettings();
  const allowed = settings.allowHttpAuth;
  httpAuthDisabledNote.hidden = allowed;
  httpAuthForm.hidden = !allowed;
  httpAuthArmButton.disabled = !allowed;
  if (!allowed) {
    renderHttpAuthArmed([]);
    return;
  }
  const response = await send({
    target: "background",
    type: "auth.listArmed"
  });
  renderHttpAuthArmed(response.data?.armed ?? []);
}
httpAuthArmButton.addEventListener("click", async () => {
  setHttpAuthError(null);
  const origin = httpAuthOrigin.value.trim();
  const username = httpAuthUsername.value;
  const password = httpAuthPassword.value;
  const ttlMinutes = Number(httpAuthTtlMinutes.value) || 15;
  httpAuthArmButton.disabled = true;
  try {
    const response = await send({
      target: "background",
      type: "auth.arm",
      origin,
      username,
      password,
      ttlMs: ttlMinutes * 6e4
    });
    if (!response.ok) {
      setHttpAuthError(response.error || "Could not stage that credential.");
      return;
    }
    httpAuthUsername.value = "";
    httpAuthPassword.value = "";
    await refreshHttpAuth();
  } finally {
    httpAuthArmButton.disabled = false;
  }
});
const SHARE_OTHER_TABS_CAP = 30;
let shareBusy = false;
function safeOrigin(url) {
  if (!url) return "";
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}
function chromeTabLabel(tab) {
  return tab.title?.trim() || tab.url || `tab ${tab.id ?? "?"}`;
}
async function resolveActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true, windowType: "normal" });
  return tab;
}
function faviconEl(tab) {
  if (!tab.favIconUrl) return null;
  const img = document.createElement("img");
  img.className = "tab-favicon";
  img.src = tab.favIconUrl;
  img.alt = "";
  img.addEventListener("error", () => img.remove());
  return img;
}
function setShareError(message) {
  shareError.hidden = !message;
  shareError.textContent = message ?? "";
}
async function shareTabById(tabId, button) {
  if (shareBusy) return;
  shareBusy = true;
  button.disabled = true;
  setShareError(null);
  try {
    const response = await send({ target: "background", type: "tabs.shareTab", tabId });
    if (!response.ok) setShareError(response.error || "Hermes couldn't share that tab.");
  } finally {
    shareBusy = false;
    await refresh();
  }
}
function shareRow(tab) {
  const row = document.createElement("li");
  row.className = "tab-row";
  const known = attachabilityOf(tab.url);
  const statusDot = document.createElement("span");
  statusDot.className = `share-dot${known.attachable ? "" : " blocked"}`;
  statusDot.setAttribute("aria-hidden", "true");
  const info = document.createElement("div");
  info.className = "tab-info share-tab-info";
  const title = document.createElement("div");
  title.className = "tab-title";
  title.textContent = chromeTabLabel(tab);
  const origin = document.createElement("div");
  origin.className = "tab-origin";
  origin.textContent = safeOrigin(tab.url) || "—";
  info.append(title, origin);
  const button = document.createElement("button");
  button.className = "tab-release share-row-button primary";
  button.textContent = "Share";
  if (!known.attachable) {
    button.disabled = true;
    button.title = known.reason ?? "";
    const why = document.createElement("div");
    why.className = "tab-origin share-tab-blocked";
    why.textContent = `Blocked: ${known.reason}`;
    why.title = `Blocked: ${known.reason}`;
    info.append(why);
  } else {
    button.addEventListener("click", () => {
      if (typeof tab.id === "number") void shareTabById(tab.id, button);
    });
  }
  const favicon = faviconEl(tab);
  row.append(statusDot, ...favicon ? [favicon] : [], info, button);
  return row;
}
async function renderShareOtherTabs(status, activeTabId) {
  const attachedIds = new Set(status.attachedTabIds);
  const allTabs = await chrome.tabs.query({});
  const candidates = allTabs.filter(
    (tab) => typeof tab.id === "number" && tab.id !== activeTabId && !attachedIds.has(tab.id) && attachabilityOf(tab.url).attachable
  );
  const shown = candidates.slice(0, SHARE_OTHER_TABS_CAP);
  const signature = JSON.stringify(shown.map((tab) => [tab.id, tab.title, tab.url]));
  const rebuild = shouldRender("shareTabs", shareTabsList, signature);
  shareTabsEmpty.hidden = candidates.length > 0;
  if (rebuild) {
    shareTabsList.innerHTML = "";
    for (const tab of shown) shareTabsList.append(shareRow(tab));
  }
  const remaining = candidates.length - shown.length;
  shareTabsMore.hidden = remaining <= 0;
  if (remaining > 0) {
    shareTabsMore.textContent = `+${remaining} more open tab${remaining === 1 ? "" : "s"} not shown — switch to one to share it from here.`;
  }
  tabsCountLabel.textContent = `Tabs (${candidates.length})`;
}
async function renderShareSection(status) {
  const activeTab = await resolveActiveTab();
  if (!activeTab || typeof activeTab.id !== "number") {
    shareActiveButton.disabled = true;
    shareActiveButton.title = "No tab found to share right now.";
    shareActiveInfo.textContent = "No tab found to share right now.";
    await renderShareOtherTabs(status, void 0);
    return;
  }
  const alreadyShared = status.attachedTabIds.includes(activeTab.id);
  const known = attachabilityOf(activeTab.url);
  shareActiveButton.disabled = alreadyShared || shareBusy || !known.attachable;
  shareActiveButton.title = alreadyShared ? "Already sharing this tab" : !known.attachable ? `Can't be shared — ${known.reason ?? ""}` : "";
  shareActiveInfo.textContent = `${chromeTabLabel(activeTab)} — ${safeOrigin(activeTab.url) || "—"}`;
  await renderShareOtherTabs(status, activeTab.id);
}
shareActiveButton.addEventListener("click", async () => {
  if (shareBusy) return;
  shareBusy = true;
  shareActiveButton.disabled = true;
  setShareError(null);
  try {
    const response = await send({ target: "background", type: "tabs.shareActive" });
    if (!response.ok) setShareError(response.error || "Hermes couldn't share this tab.");
  } finally {
    shareBusy = false;
    await refresh();
  }
});
async function refreshAttached(status) {
  const activeTab = await resolveActiveTab();
  const activeTabId = typeof activeTab?.id === "number" ? activeTab.id : void 0;
  if (!status.paired || status.attachedTabIds.length === 0) {
    renderAttachedTabs([], status, activeTabId);
    return;
  }
  const attachedIds = new Set(status.attachedTabIds);
  const response = await send({ target: "background", type: "tabs.list" });
  const tabs = (response.data?.tabs ?? []).filter((tab) => attachedIds.has(tab.tabId));
  renderAttachedTabs(tabs, status, activeTabId);
}
async function refreshApprovals() {
  const approvals = await fetchPendingApprovals();
  renderApprovals(approvalList, approvalEmpty, approvalCount, approvals, () => {
    void refreshApprovals();
  });
  waitingApprovalHead.hidden = approvals.length === 0;
  if (tabsApprovalBadge) {
    tabsApprovalBadge.textContent = approvals.length > 0 ? String(approvals.length) : "";
    tabsApprovalBadge.hidden = approvals.length === 0;
  }
}
async function renderOnboarding() {
  const settings = await getSettings();
  if (document.activeElement !== onboardGatewayUrl) {
    onboardGatewayUrl.value = settings.gatewayUrl;
  }
  onboardGatewayDisplay.textContent = settings.gatewayUrl.replace(/^wss?:\/\//, "") || "(not set)";
  const onGatewayStep = !settings.gatewayUrlConfirmed;
  onboardStepGateway.hidden = !onGatewayStep;
  onboardStepPair.hidden = onGatewayStep;
}
async function refresh() {
  const response = await send({ target: "offscreen", type: "status.get" });
  if (response.ok && response.data) {
    render(response.data);
    if (!response.data.paired) await renderOnboarding();
    if (response.data.paired) await renderShareSection(response.data);
    await refreshAttached(response.data);
    if (response.data.paired) await refreshApprovals();
    if (response.data.paired) await refreshHttpAuth();
  } else {
    statusText.textContent = "Starting…";
    statusDetail.textContent = "";
  }
}
onboardGatewayContinue.addEventListener("click", async () => {
  const url = onboardGatewayUrl.value.trim();
  if (!url) {
    errorBox.hidden = false;
    errorBox.textContent = "Enter the gateway URL before continuing.";
    return;
  }
  errorBox.hidden = true;
  onboardGatewayContinue.disabled = true;
  try {
    await setSettings({ gatewayUrl: url, gatewayUrlConfirmed: true });
    await send({ target: "offscreen", type: "settings.changed" });
    await refresh();
  } finally {
    onboardGatewayContinue.disabled = false;
  }
});
onboardBackToGateway.addEventListener("click", async () => {
  await setSettings({ gatewayUrlConfirmed: false });
  await refresh();
});
copyPairCommand.addEventListener("click", async () => {
  const command = "hermes browser-bridge pair";
  try {
    await navigator.clipboard.writeText(command);
    const original = copyPairCommand.textContent;
    copyPairCommand.textContent = "Copied";
    setTimeout(() => {
      copyPairCommand.textContent = original;
    }, 1200);
  } catch {
  }
});
pairButton.addEventListener("click", async () => {
  const code = pairCode.value.trim();
  if (!/^\d{6}$/.test(code)) {
    errorBox.hidden = false;
    errorBox.textContent = "Enter the six-digit code printed by `hermes browser-bridge pair`.";
    return;
  }
  pairButton.disabled = true;
  try {
    const response = await send({ target: "offscreen", type: "connect", pairCode: code });
    pairCode.value = "";
    if (response.data) render(response.data);
    else await refresh();
  } finally {
    pairButton.disabled = false;
  }
});
cancelConnectButton.addEventListener("click", async () => {
  cancelConnectButton.disabled = true;
  try {
    await send({ target: "offscreen", type: "cancelConnect" });
    await refresh();
  } finally {
    cancelConnectButton.disabled = false;
  }
});
pauseButton.addEventListener("click", async () => {
  pauseButton.disabled = true;
  try {
    const settings = await getSettings();
    const paused = !settings.paused;
    await setSettings({ paused, pauseReason: paused ? "popup" : null, pausedAt: paused ? Date.now() : null });
    await send({ target: "offscreen", type: "pause.changed" });
    await refresh();
  } finally {
    pauseButton.disabled = false;
  }
});
async function doReconnect() {
  reconnectButton.disabled = true;
  hostReconnectButton.disabled = true;
  try {
    await send({ target: "offscreen", type: "connect" });
    await refresh();
  } finally {
    reconnectButton.disabled = false;
    hostReconnectButton.disabled = false;
  }
}
reconnectButton.addEventListener("click", () => void doReconnect());
hostReconnectButton.addEventListener("click", () => void doReconnect());
el("disconnectButton").addEventListener("click", async () => {
  await send({ target: "offscreen", type: "disconnect", reason: "popup disconnect" });
  await refresh();
});
el("killButton").addEventListener("click", async () => {
  await send({ target: "offscreen", type: "killSwitch" });
  await refresh();
});
el("unpairButton").addEventListener("click", async () => {
  await send({ target: "offscreen", type: "unpair" });
  await refresh();
});
el("openOptions").addEventListener("click", (event) => {
  event.preventDefault();
  void chrome.runtime.openOptionsPage();
});
const cookiePasteSection = el("cookiePasteSection");
const cookiePasteOrigin = el("cookiePasteOrigin");
const cookiePasteName = el("cookiePasteName");
const cookiePasteValue = el("cookiePasteValue");
const cookiePasteSecure = el("cookiePasteSecure");
const cookiePasteHttpOnly = el("cookiePasteHttpOnly");
const cookiePasteButton = el("cookiePasteButton");
const cookiePasteResult = el("cookiePasteResult");
cookiePasteSection.addEventListener("toggle", () => {
  if (!cookiePasteSection.open || cookiePasteOrigin.value) return;
  void resolveActiveTab().then((tab) => {
    const origin = safeOrigin(tab?.url);
    if (origin) cookiePasteOrigin.value = origin;
  });
});
cookiePasteButton.addEventListener("click", async () => {
  const url = cookiePasteOrigin.value.trim();
  const name = cookiePasteName.value.trim();
  const value = cookiePasteValue.value;
  cookiePasteResult.hidden = false;
  if (!url || !name || !value) {
    cookiePasteResult.textContent = "Origin, name, and value are all required.";
    return;
  }
  cookiePasteButton.disabled = true;
  try {
    const result = await chrome.cookies.set({
      url,
      name,
      value,
      secure: cookiePasteSecure.checked,
      httpOnly: cookiePasteHttpOnly.checked
    });
    if (result) {
      cookiePasteResult.textContent = `Set — ${result.domain}${result.path} (secure=${result.secure}, httpOnly=${result.httpOnly}).`;
      cookiePasteValue.value = "";
    } else {
      cookiePasteResult.textContent = "chrome.cookies.set rejected these details (e.g. domain/secure/SameSite mismatch for this origin).";
    }
  } catch (error) {
    cookiePasteResult.textContent = `Failed: ${error instanceof Error ? error.message : String(error)}`;
  } finally {
    cookiePasteButton.disabled = false;
  }
});
const TAB_STRIP_KEY = "hermes_popup_tab";
const TAB_STRIP_IDS = ["tabs", "credentials", "hostnet"];
const tabStripButtons = {
  tabs: el("tabBtnTabs"),
  credentials: el("tabBtnCredentials"),
  hostnet: el("tabBtnHostNet")
};
const tabStripPanels = {
  tabs: el("panelTabs"),
  credentials: el("panelCredentials"),
  hostnet: el("panelHostNet")
};
async function loadLastTab() {
  try {
    const session = chrome.storage.session;
    if (session) {
      const stored = await session.get(TAB_STRIP_KEY);
      const value = stored[TAB_STRIP_KEY];
      if (TAB_STRIP_IDS.includes(value)) return value;
    }
  } catch {
  }
  try {
    const value = window.localStorage.getItem(TAB_STRIP_KEY);
    if (value && TAB_STRIP_IDS.includes(value)) return value;
  } catch {
  }
  return "tabs";
}
function saveLastTab(tab) {
  try {
    const session = chrome.storage.session;
    void session?.set({ [TAB_STRIP_KEY]: tab });
  } catch {
  }
  try {
    window.localStorage.setItem(TAB_STRIP_KEY, tab);
  } catch {
  }
}
function selectTab(tab, focusButton = false) {
  for (const id of TAB_STRIP_IDS) {
    const active = id === tab;
    tabStripButtons[id].setAttribute("aria-selected", String(active));
    tabStripButtons[id].tabIndex = active ? 0 : -1;
    tabStripPanels[id].hidden = !active;
  }
  if (focusButton) tabStripButtons[tab].focus();
  saveLastTab(tab);
}
for (const id of TAB_STRIP_IDS) {
  tabStripButtons[id].addEventListener("click", () => selectTab(id));
  tabStripButtons[id].addEventListener("keydown", (event) => {
    const index = TAB_STRIP_IDS.indexOf(id);
    let nextIndex = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % TAB_STRIP_IDS.length;
    else if (event.key === "ArrowLeft") nextIndex = (index - 1 + TAB_STRIP_IDS.length) % TAB_STRIP_IDS.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = TAB_STRIP_IDS.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    selectTab(TAB_STRIP_IDS[nextIndex], true);
  });
}
void loadLastTab().then((tab) => selectTab(tab));
footerShortcuts.addEventListener("click", (event) => {
  event.preventDefault();
  void chrome.tabs.create({ url: "chrome://extensions/shortcuts" });
});
footerLogs.addEventListener("click", (event) => {
  event.preventDefault();
  selectTab("hostnet");
  const replayView = document.getElementById("replayView");
  if (replayView?.hidden) el("replayToggle").click();
  el("replayView").scrollIntoView({ block: "nearest" });
});
footerDocs.addEventListener("click", (event) => {
  event.preventDefault();
  void chrome.runtime.openOptionsPage();
});
void refresh();
setInterval(() => void refresh(), 2e3);
initReplayUI();
