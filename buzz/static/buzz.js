const buzzTableId = "torrent-table";
let _truncObserver = null;
let _buzzSocketStatusMonitor = null;

function markTruncatedCells() {
  document.querySelectorAll(".trunc-cell").forEach((cell) => {
    const idle = cell.querySelector(".trunc-idle");
    if (!idle) return;
    if (idle.scrollWidth > idle.clientWidth) {
      cell.classList.add("is-truncated");
    } else {
      cell.classList.remove("is-truncated");
    }
  });
}

function initTruncCells() {
  markTruncatedCells();
  const table = document.getElementById(buzzTableId);
  if (!table || typeof ResizeObserver === "undefined") return;
  if (_truncObserver) {
    _truncObserver.disconnect();
  }
  _truncObserver = new ResizeObserver(markTruncatedCells);
  _truncObserver.observe(table);
}

function initTableIfPresent() {
  if (document.getElementById(buzzTableId)) {
    initTruncCells();
  }
}

function setBuzzStatus(label, className) {
  const element = document.getElementById("status-ready-label");
  if (!element) return;
  element.textContent = label;
  element.className = className;
}

function setBuzzConsole(message, className) {
  const element = document.getElementById("meta-console-msg");
  if (!element) return;
  element.textContent = message;
  element.className = className;
}

function createBuzzSocketStatusMonitor() {
  function showOffline() {
    setBuzzStatus("[offline]", "service-status-red");
  }

  function bindSocketCallbacks() {
    const liveSocket = window.liveSocket;
    const socket = liveSocket && typeof liveSocket.getSocket === "function"
      ? liveSocket.getSocket()
      : null;
    if (!socket) {
      return false;
    }
    socket.onOpen(() => {});
    socket.onClose(() => {
      showOffline();
    });
    socket.onError(() => {
      showOffline();
    });
    return true;
  }

  function start() {
    showOffline();
    if (!bindSocketCallbacks()) {
      window.setTimeout(start, 100);
    }
  }

  function stop() {}

  return { start, stop };
}

function initBuzzSocketStatusMonitor() {
  if (_buzzSocketStatusMonitor) {
    _buzzSocketStatusMonitor.stop();
  }
  _buzzSocketStatusMonitor = createBuzzSocketStatusMonitor();
  _buzzSocketStatusMonitor.start();
}

document.addEventListener("DOMContentLoaded", initTableIfPresent);
document.addEventListener("DOMContentLoaded", initBuzzSocketStatusMonitor);
window.addEventListener("phx:navigate", initTableIfPresent);
window.addEventListener("phx:navigate", initBuzzSocketStatusMonitor);
