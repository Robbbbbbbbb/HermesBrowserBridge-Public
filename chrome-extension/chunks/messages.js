async function send(message) {
  try {
    const response = await chrome.runtime.sendMessage(message);
    return response ?? { ok: false, error: "no listener responded" };
  } catch (error) {
    return { ok: false, error: String(error) };
  }
}
export {
  send as s
};
