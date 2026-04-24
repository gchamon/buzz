let _buzzSocketStatusMonitor = null;

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

document.addEventListener("DOMContentLoaded", initBuzzSocketStatusMonitor);
window.addEventListener("phx:navigate", initBuzzSocketStatusMonitor);
