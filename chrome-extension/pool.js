import { s as send } from "./chunks/messages.js";
import { s as sortWorkers, w as workerStateLabel, f as formatWorkerAge } from "./chunks/background-requests.js";
const el = (id) => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing element: ${id}`);
  return node;
};
const workersList = el("workersList");
const workersEmpty = el("workersEmpty");
const stopAllButton = el("stopAll");
const POLL_MS = 3e3;
let stopAllBusy = false;
function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}
async function render() {
  const response = await send({ target: "background", type: "silent.pool" });
  const workers = sortWorkers(response.ok ? response.data?.workers ?? [] : []);
  workersEmpty.hidden = workers.length > 0;
  stopAllButton.disabled = stopAllBusy || workers.length === 0;
  clearChildren(workersList);
  for (const worker of workers) {
    const row = document.createElement("li");
    row.className = "tab-row";
    const info = document.createElement("div");
    info.className = "tab-info";
    const originLine = document.createElement("div");
    originLine.className = "tab-origin";
    originLine.textContent = worker.origin;
    const metaLine = document.createElement("div");
    metaLine.className = "mode-source";
    metaLine.textContent = `${workerStateLabel(worker.state)} · ${formatWorkerAge(worker.age_ms)} old · ${worker.served} served`;
    info.append(originLine, metaLine);
    const stopButton = document.createElement("button");
    stopButton.type = "button";
    stopButton.className = "tab-release danger-outline";
    stopButton.textContent = "Stop";
    stopButton.addEventListener("click", () => {
      void (async () => {
        stopButton.disabled = true;
        try {
          await send({ target: "background", type: "silent.kill", origin: worker.origin });
        } finally {
          await render();
        }
      })();
    });
    row.append(info, stopButton);
    workersList.append(row);
  }
}
stopAllButton.addEventListener("click", () => {
  void (async () => {
    stopAllBusy = true;
    stopAllButton.disabled = true;
    try {
      await send({ target: "background", type: "silent.kill" });
    } finally {
      stopAllBusy = false;
      await render();
    }
  })();
});
void render();
setInterval(() => void render(), POLL_MS);
