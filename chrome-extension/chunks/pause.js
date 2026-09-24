function formatClock(ms) {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function stoppedAt(state) {
  return state.pauseReason === "stop-button" && typeof state.pausedAt === "number" ? state.pausedAt : null;
}
function pausedErrorMessage(state) {
  const at = stoppedAt(state);
  if (at !== null) {
    return `the user pressed Stop on the page at ${formatClock(at)} (${new Date(at).toISOString()}) — every tab was released and sharing is paused. Stop now and ask the user in chat before doing anything else; do not retry, re-attach or open tabs. Only they can resume (Resume sharing in the extension popup)`;
  }
  return "sharing is paused in the extension popup — resume it there before retrying";
}
function stopShortcutText(shortcut) {
  return shortcut ? `Stop Hermes from any tab: ${shortcut} (releases every tab and pauses sharing; change it at chrome://extensions/shortcuts).` : "Set a keyboard shortcut to stop Hermes from any tab at chrome://extensions/shortcuts.";
}
export {
  pausedErrorMessage as p,
  stopShortcutText as s
};
