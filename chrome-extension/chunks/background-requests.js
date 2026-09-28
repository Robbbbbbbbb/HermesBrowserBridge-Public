const BACKGROUND_MODE_LABEL = {
  off: "Off",
  ask: "Ask first",
  always: "Always allow"
};
function currentBackgroundMode(origin, overrides) {
  return overrides[origin] ?? "default";
}
function describeDefault(ordinaryMode) {
  switch (ordinaryMode) {
    case "off":
      return "Default — off (this site has no access at all)";
    case "request":
      return "Default — asks once per session";
    case "full":
      return "Default (set in the plugin settings — silent_fetch.full_implies_silent)";
    default:
      return "Default (not reported yet)";
  }
}
function sortWorkers(workers) {
  return [...workers].sort((a, b) => a.origin.localeCompare(b.origin));
}
const WORKER_STATE_LABEL = {
  idle: "Idle",
  busy: "Busy",
  launching: "Launching…",
  recycling: "Recycling…"
};
function workerStateLabel(state) {
  return WORKER_STATE_LABEL[state] ?? state;
}
function formatWorkerAge(ageMs) {
  const totalSeconds = Math.max(0, Math.floor(ageMs / 1e3));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor(totalSeconds % 3600 / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${seconds}s`;
}
export {
  BACKGROUND_MODE_LABEL as B,
  currentBackgroundMode as c,
  describeDefault as d,
  formatWorkerAge as f,
  sortWorkers as s,
  workerStateLabel as w
};
