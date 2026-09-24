import { s as send } from "./chunks/messages.js";
import { g as getSettings, s as setSettings, d as clampReplayRetention, e as clampLeaseSeconds } from "./chunks/storage.js";
const el$2 = (id) => document.getElementById(id);
const POINTER_ANIMATION_MODES = ["normal", "fast", "off"];
function normalizePointerAnimation(value) {
  return POINTER_ANIMATION_MODES.includes(value) ? value : "normal";
}
function applyPresenceSettings(settings) {
  const cursor = el$2("showPresenceCursor");
  const glow = el$2("showActivityGlow");
  const highlight = el$2("highlightSharedTabs");
  const holdForeignFrames = el$2("holdForeignFramesWhileAttached");
  const pointerAnimation = el$2("pointerAnimation");
  if (cursor) cursor.checked = settings.showPresenceCursor;
  if (glow) glow.checked = settings.showActivityGlow;
  if (highlight) highlight.checked = settings.highlightSharedTabs;
  if (holdForeignFrames) holdForeignFrames.checked = settings.holdForeignFramesWhileAttached;
  if (pointerAnimation) pointerAnimation.value = settings.pointerAnimation;
}
function collectPresencePatch() {
  const patch = {};
  const cursor = el$2("showPresenceCursor");
  const glow = el$2("showActivityGlow");
  const highlight = el$2("highlightSharedTabs");
  const holdForeignFrames = el$2("holdForeignFramesWhileAttached");
  const pointerAnimation = el$2("pointerAnimation");
  if (cursor) patch.showPresenceCursor = cursor.checked;
  if (glow) patch.showActivityGlow = glow.checked;
  if (highlight) patch.highlightSharedTabs = highlight.checked;
  if (holdForeignFrames) patch.holdForeignFramesWhileAttached = holdForeignFrames.checked;
  if (pointerAnimation) patch.pointerAnimation = normalizePointerAnimation(pointerAnimation.value);
  return patch;
}
const el$1 = (id) => document.getElementById(id);
function getNetworkPrediction() {
  return new Promise((resolve) => {
    chrome.privacy.network.networkPredictionEnabled.get({}, (details) => {
      resolve({ value: Boolean(details.value), levelOfControl: details.levelOfControl });
    });
  });
}
function setNetworkPredictionDisabled() {
  return new Promise((resolve) => {
    chrome.privacy.network.networkPredictionEnabled.set({ value: false }, () => resolve());
  });
}
function clearNetworkPredictionOverride() {
  return new Promise((resolve) => {
    chrome.privacy.network.networkPredictionEnabled.clear({}, () => resolve());
  });
}
function describeLevelOfControl(level) {
  switch (level) {
    case "controlled_by_other_extensions":
      return "Another extension is controlling this Chrome setting, so this checkbox has no effect until that extension releases it.";
    case "not_controllable":
      return "This Chrome setting is locked by your organization's policy and can't be changed here.";
    case "controlled_by_this_extension":
      return "Hermes Browser Bridge is currently holding this Chrome setting off.";
    case "controllable_by_this_extension":
    default:
      return "";
  }
}
function applyNetworkPredictionState(state) {
  const checkbox = el$1("disableChromePreloading");
  const status = el$1("disableChromePreloadingStatus");
  if (checkbox) {
    checkbox.checked = state.value === false;
    checkbox.disabled = state.levelOfControl === "not_controllable" || state.levelOfControl === "controlled_by_other_extensions";
  }
  if (status) {
    const text = describeLevelOfControl(state.levelOfControl);
    status.textContent = text;
    status.hidden = text.length === 0;
  }
}
async function refreshNetworkPredictionState() {
  const state = await getNetworkPrediction();
  applyNetworkPredictionState(state);
  return state;
}
async function onToggle(checkbox) {
  if (checkbox.checked) {
    await setNetworkPredictionDisabled();
  } else {
    await clearNetworkPredictionOverride();
  }
  await refreshNetworkPredictionState();
}
function initPreloadToggle() {
  void refreshNetworkPredictionState();
  const checkbox = el$1("disableChromePreloading");
  checkbox?.addEventListener("change", () => void onToggle(checkbox));
}
const el = (id) => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing element: ${id}`);
  return node;
};
const gatewayUrl = el("gatewayUrl");
const deviceName = el("deviceName");
const autoConnect = el("autoConnect");
const redactPasswords = el("redactPasswords");
const redactCreditCards = el("redactCreditCards");
const redactSsn = el("redactSsn");
const redactEmails = el("redactEmails");
const redactPhones = el("redactPhones");
const allowFileUpload = el("allowFileUpload");
const allowFileUploadFromAgent = el("allowFileUploadFromAgent");
const allowDialogDismiss = el("allowDialogDismiss");
const allowDialogAccept = el("allowDialogAccept");
const allowEvaluate = el("allowEvaluate");
const allowConsoleRead = el("allowConsoleRead");
const allowCookieWrite = el("allowCookieWrite");
const allowHttpAuth = el("allowHttpAuth");
const allowDownloadsRead = el("allowDownloadsRead");
const uploadRoots = el("uploadRoots");
const maxUploadBytes = el("maxUploadBytes");
const defaultAccessMode = el("defaultAccessMode");
const leaseSeconds = el("leaseSeconds");
const unlimitedLease = el("unlimitedLease");
const commitMode = el("commitMode");
const recordReplay = el("recordReplay");
const replayRetention = el("replayRetention");
const saved = el("saved");
unlimitedLease.addEventListener("change", () => {
  leaseSeconds.disabled = unlimitedLease.checked;
});
function apply(settings) {
  gatewayUrl.value = settings.gatewayUrl;
  deviceName.value = settings.deviceName;
  autoConnect.checked = settings.autoConnect;
  redactPasswords.checked = settings.redactPasswords;
  redactCreditCards.checked = settings.redactCreditCards;
  redactSsn.checked = settings.redactSsn;
  redactEmails.checked = settings.redactEmails;
  redactPhones.checked = settings.redactPhones;
  allowFileUpload.checked = settings.allowFileUpload;
  allowFileUploadFromAgent.checked = settings.allowFileUploadFromAgent;
  allowDialogDismiss.checked = settings.allowDialogDismiss;
  allowDialogAccept.checked = settings.allowDialogAccept;
  allowEvaluate.checked = settings.allowEvaluate;
  allowConsoleRead.checked = settings.allowConsoleRead;
  allowCookieWrite.checked = settings.allowCookieWrite;
  allowHttpAuth.checked = settings.allowHttpAuth;
  allowDownloadsRead.checked = settings.allowDownloadsRead;
  uploadRoots.value = settings.uploadRoots;
  maxUploadBytes.value = String(settings.maxUploadBytes);
  defaultAccessMode.value = settings.defaultAccessMode;
  leaseSeconds.value = String(settings.leaseSeconds);
  unlimitedLease.checked = settings.unlimitedLease;
  leaseSeconds.disabled = settings.unlimitedLease;
  commitMode.value = settings.commitMode;
  recordReplay.checked = settings.recordReplay;
  replayRetention.value = String(settings.replayRetention);
  applyPresenceSettings(settings);
}
function redactionChanged(previous) {
  return redactPasswords.checked !== previous.redactPasswords || redactCreditCards.checked !== previous.redactCreditCards || redactSsn.checked !== previous.redactSsn || redactEmails.checked !== previous.redactEmails || redactPhones.checked !== previous.redactPhones;
}
function powersChanged(previous) {
  return allowFileUpload.checked !== previous.allowFileUpload || allowFileUploadFromAgent.checked !== previous.allowFileUploadFromAgent || allowDialogDismiss.checked !== previous.allowDialogDismiss || allowDialogAccept.checked !== previous.allowDialogAccept || allowEvaluate.checked !== previous.allowEvaluate || allowConsoleRead.checked !== previous.allowConsoleRead || allowCookieWrite.checked !== previous.allowCookieWrite || allowHttpAuth.checked !== previous.allowHttpAuth || allowDownloadsRead.checked !== previous.allowDownloadsRead || uploadRoots.value !== previous.uploadRoots || Number(maxUploadBytes.value) !== previous.maxUploadBytes;
}
function defaultAccessModeChanged(previous) {
  return defaultAccessMode.value !== previous.defaultAccessMode;
}
function leaseChanged(previous) {
  return clampLeaseSeconds(Number(leaseSeconds.value)) !== previous.leaseSeconds || unlimitedLease.checked !== previous.unlimitedLease;
}
function commitModeChanged(previous) {
  return commitMode.value !== previous.commitMode;
}
el("save").addEventListener("click", async () => {
  const previous = await getSettings();
  const nextGatewayUrl = gatewayUrl.value.trim();
  const redactionWasChanged = redactionChanged(previous);
  const powersWereChanged = powersChanged(previous);
  const defaultAccessModeWasChanged = defaultAccessModeChanged(previous);
  const leaseWasChanged = leaseChanged(previous);
  const commitModeWasChanged = commitModeChanged(previous);
  await setSettings({
    gatewayUrl: nextGatewayUrl,
    deviceName: deviceName.value.trim(),
    autoConnect: autoConnect.checked,
    redactPasswords: redactPasswords.checked,
    redactCreditCards: redactCreditCards.checked,
    redactSsn: redactSsn.checked,
    redactEmails: redactEmails.checked,
    redactPhones: redactPhones.checked,
    allowFileUpload: allowFileUpload.checked,
    allowFileUploadFromAgent: allowFileUploadFromAgent.checked,
    allowDialogDismiss: allowDialogDismiss.checked,
    allowDialogAccept: allowDialogAccept.checked,
    allowEvaluate: allowEvaluate.checked,
    allowConsoleRead: allowConsoleRead.checked,
    allowCookieWrite: allowCookieWrite.checked,
    allowHttpAuth: allowHttpAuth.checked,
    allowDownloadsRead: allowDownloadsRead.checked,
    uploadRoots: uploadRoots.value,
    maxUploadBytes: Number(maxUploadBytes.value) || 0,
    defaultAccessMode: defaultAccessMode.value,
    leaseSeconds: clampLeaseSeconds(Number(leaseSeconds.value)),
    unlimitedLease: unlimitedLease.checked,
    commitMode: commitMode.value,
    recordReplay: recordReplay.checked,
    replayRetention: clampReplayRetention(Number(replayRetention.value)),
    ...collectPresencePatch(),
    // A user typing a real URL here (as opposed to the first-run popup flow)
    // has, by definition, set it — this keeps the popup from ever showing
    // onboarding's "step 1" again for a device that's already been through
    // Options once, even if it somehow never went through the popup flow.
    gatewayUrlConfirmed: true
  });
  await send({ target: "offscreen", type: "settings.changed" });
  if (redactionWasChanged) {
    await send({ target: "offscreen", type: "redaction.changed" });
  }
  if (powersWereChanged) {
    await send({ target: "offscreen", type: "powers.changed" });
  }
  if (defaultAccessModeWasChanged) {
    await send({ target: "offscreen", type: "defaultMode.changed" });
  }
  if (leaseWasChanged) {
    await send({ target: "offscreen", type: "lease.changed" });
  }
  if (commitModeWasChanged) {
    await send({ target: "offscreen", type: "commitMode.changed" });
  }
  saved.textContent = nextGatewayUrl !== previous.gatewayUrl ? "Saved — reconnecting to the new gateway…" : "Saved";
  saved.hidden = false;
  setTimeout(() => {
    saved.hidden = true;
  }, 2500);
});
void getSettings().then(apply);
initPreloadToggle();
