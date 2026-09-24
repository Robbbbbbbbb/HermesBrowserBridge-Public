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
  allowEvaluate: false,
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
  replayRetention: 200
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
const SETTINGS_KEY = "settings";
const CREDENTIALS_KEY = "credentials";
async function getSettings() {
  const stored = await chrome.storage.local.get(SETTINGS_KEY);
  return { ...DEFAULT_SETTINGS, ...stored[SETTINGS_KEY] ?? {} };
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
    maxUploadBytes: settings.maxUploadBytes
  };
}
function leaseSecondsOf(settings) {
  return settings.unlimitedLease ? 0 : clampLeaseSeconds(settings.leaseSeconds);
}
export {
  setCredentials as a,
  getCredentials as b,
  clearCredentials as c,
  clampReplayRetention as d,
  clampLeaseSeconds as e,
  getSettings as g,
  leaseSecondsOf as l,
  powerPolicyOf as p,
  redactionPolicyOf as r,
  setSettings as s
};
