const APPROVAL_POLICY_VALUES = /* @__PURE__ */ new Set([
  "always_allow",
  "ask_per_session",
  "always_ask"
]);
function isApprovalPolicy(value) {
  return typeof value === "string" && APPROVAL_POLICY_VALUES.has(value);
}
const DEFAULT_SETTINGS = {
  gatewayUrl: "ws://localhost:8765/bridge",
  deviceName: "",
  autoConnect: true,
  redactPasswords: true,
  redactCreditCards: true,
  redactSsn: true,
  redactEmails: false,
  redactPhones: false,
  showPresenceCursor: true,
  showActivityGlow: true,
  highlightSharedTabs: true,
  pointerAnimation: "normal",
  gatewayUrlConfirmed: false,
  paused: false,
  pauseReason: null,
  pausedAt: null,
  allowFileUpload: false,
  allowFileUploadFromAgent: false,
  allowDialogDismiss: true,
  allowDialogAccept: false,
  allowEvaluate: true,
  allowConsoleRead: false,
  allowCookieWrite: false,
  allowHttpAuth: false,
  allowDownloadsRead: true,
  uploadRoots: "",
  maxUploadBytes: 10485760,
  defaultAccessMode: "full",
  holdForeignFramesWhileAttached: true,
  leaseSeconds: 60,
  unlimitedLease: false,
  commitMode: "auto",
  recordReplay: false,
  replayRetention: 200,
  evaluateApproval: "always_allow",
  uploadApproval: "ask_per_session",
  httpAuthApproval: "ask_per_session"
};
const MIN_LEASE_SECONDS = 10;
const MAX_LEASE_SECONDS = 1200;
const DEFAULT_LEASE_SECONDS = 60;
function clampLeaseSeconds(value) {
  if (!Number.isFinite(value)) return DEFAULT_LEASE_SECONDS;
  return Math.min(MAX_LEASE_SECONDS, Math.max(MIN_LEASE_SECONDS, Math.round(value)));
}
const MIN_REPLAY_RETENTION = 1;
const MAX_REPLAY_RETENTION = 1e3;
const DEFAULT_REPLAY_RETENTION = 200;
function clampReplayRetention(value) {
  if (!Number.isFinite(value)) return DEFAULT_REPLAY_RETENTION;
  return Math.min(MAX_REPLAY_RETENTION, Math.max(MIN_REPLAY_RETENTION, Math.round(value)));
}
function migrateApprovalPolicies(merged, stored) {
  let changed = false;
  const settings = { ...merged };
  if (stored && "evaluateApproval" in stored) {
    if (!isApprovalPolicy(stored.evaluateApproval)) {
      settings.evaluateApproval = "always_ask";
      changed = true;
    }
  } else if (stored && "allowEvaluate" in stored) {
    settings.evaluateApproval = stored.allowEvaluate === true ? "always_allow" : "always_ask";
    changed = true;
  } else {
    settings.evaluateApproval = DEFAULT_SETTINGS.evaluateApproval;
  }
  for (const key of ["uploadApproval", "httpAuthApproval"]) {
    if (stored && key in stored) {
      if (!isApprovalPolicy(stored[key])) {
        settings[key] = "always_ask";
        changed = true;
      }
    } else {
      settings[key] = DEFAULT_SETTINGS[key];
    }
  }
  return { settings, changed };
}
const SETTINGS_KEY = "settings";
const CREDENTIALS_KEY = "credentials";
async function getSettings() {
  const stored = await chrome.storage.local.get(SETTINGS_KEY);
  const storedSettings = stored[SETTINGS_KEY];
  const merged = { ...DEFAULT_SETTINGS, ...storedSettings ?? {} };
  const { settings, changed } = migrateApprovalPolicies(merged, storedSettings);
  if (changed) {
    await chrome.storage.local.set({ [SETTINGS_KEY]: settings });
  }
  return settings;
}
async function setSettings(patch) {
  const next = { ...await getSettings(), ...patch };
  await chrome.storage.local.set({ [SETTINGS_KEY]: next });
  return next;
}
async function getCredentials() {
  const stored = await chrome.storage.local.get(CREDENTIALS_KEY);
  const creds = stored[CREDENTIALS_KEY];
  return creds?.deviceId && creds?.deviceToken ? creds : null;
}
async function setCredentials(creds) {
  await chrome.storage.local.set({ [CREDENTIALS_KEY]: creds });
}
async function clearCredentials() {
  await chrome.storage.local.remove(CREDENTIALS_KEY);
}
const ORIGIN_SILENT_MODES_KEY = "originSilentModes";
async function getOriginSilentModes() {
  const stored = await chrome.storage.local.get(ORIGIN_SILENT_MODES_KEY);
  return { ...stored[ORIGIN_SILENT_MODES_KEY] ?? {} };
}
async function setOriginSilentMode(origin, mode) {
  const next = { ...await getOriginSilentModes(), [origin]: mode };
  await chrome.storage.local.set({ [ORIGIN_SILENT_MODES_KEY]: next });
  return next;
}
async function clearOriginSilentMode(origin) {
  const next = { ...await getOriginSilentModes() };
  delete next[origin];
  await chrome.storage.local.set({ [ORIGIN_SILENT_MODES_KEY]: next });
  return next;
}
function originSilentModeArrayOf(modes) {
  return Object.entries(modes).map(([origin, mode]) => ({ origin, mode }));
}
function redactionPolicyOf(settings) {
  return {
    password: settings.redactPasswords,
    card: settings.redactCreditCards,
    ssn: settings.redactSsn,
    email: settings.redactEmails,
    phone: settings.redactPhones
  };
}
function powerPolicyOf(settings) {
  return {
    allowFileUpload: settings.allowFileUpload,
    allowFileUploadFromAgent: settings.allowFileUploadFromAgent,
    allowDialogDismiss: settings.allowDialogDismiss,
    allowDialogAccept: settings.allowDialogAccept,
    allowEvaluate: settings.allowEvaluate,
    allowConsoleRead: settings.allowConsoleRead,
    allowCookieWrite: settings.allowCookieWrite,
    allowHttpAuth: settings.allowHttpAuth,
    allowDownloadsRead: settings.allowDownloadsRead,
    uploadRoots: settings.uploadRoots,
    maxUploadBytes: settings.maxUploadBytes,
    evaluateApproval: settings.evaluateApproval,
    uploadApproval: settings.uploadApproval,
    httpAuthApproval: settings.httpAuthApproval
  };
}
function leaseSecondsOf(settings) {
  return settings.unlimitedLease ? 0 : clampLeaseSeconds(settings.leaseSeconds);
}
export {
  getOriginSilentModes as a,
  setCredentials as b,
  clearCredentials as c,
  getCredentials as d,
  clampReplayRetention as e,
  clampLeaseSeconds as f,
  getSettings as g,
  clearOriginSilentMode as h,
  setOriginSilentMode as i,
  leaseSecondsOf as l,
  originSilentModeArrayOf as o,
  powerPolicyOf as p,
  redactionPolicyOf as r,
  setSettings as s
};
