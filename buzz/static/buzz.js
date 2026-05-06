let _buzzSocketStatusMonitor = null;

function setBuzzStatus(label, className) {
  const element = document.getElementById("status-ready-label");
  if (element) {
    element.textContent = label;
    element.className = className;
    element.title = label;
    element.setAttribute("aria-label", label);
  }
  document.querySelectorAll(".mobile-ready-indicator").forEach((indicator) => {
    indicator.className = `mobile-ready-indicator ${className}`;
    indicator.title = label;
    indicator.setAttribute("aria-label", label);
  });
}

function setBuzzConsole(message, className) {
  const element = document.getElementById("meta-console-msg");
  if (!element) return;
  element.textContent = message;
  element.className = className;
}

function createBuzzSocketStatusMonitor() {
  const probeIntervalMs = 1000;
  const probeTimeoutMs = 1500;
  let socket = null;
  let callbackRefs = [];
  let retryTimer = null;
  let probeTimer = null;
  let probeInFlight = false;
  let stopped = false;

  function clearTimer(timer) {
    if (timer) {
      window.clearTimeout(timer);
    }
    return null;
  }

  function isSocketConnected() {
    return socket && typeof socket.isConnected === "function"
      ? socket.isConnected()
      : false;
  }

  function showOffline() {
    setBuzzStatus("[offline]", "service-status-red");
    startOfflineProbe();
  }

  function showReachableStatus(payload) {
    const isReady = payload.ui_status === "ready" || payload.status === "ready";
    if (isReady) {
      setBuzzStatus("[ready]", "service-status-green");
    } else {
      setBuzzStatus("[starting]", "service-status-orange");
    }
  }

  function reconnectLiveSocket() {
    const liveSocket = window.liveSocket;
    if (!liveSocket || typeof liveSocket.connect !== "function") {
      return;
    }
    if (isSocketConnected()) {
      return;
    }
    liveSocket.connect();
  }

  function scheduleProbe(delay = probeIntervalMs) {
    if (stopped || isSocketConnected()) {
      return;
    }
    probeTimer = clearTimer(probeTimer);
    probeTimer = window.setTimeout(runProbe, delay);
  }

  async function runProbe() {
    if (stopped || probeInFlight || isSocketConnected()) {
      return;
    }

    probeInFlight = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), probeTimeoutMs);
    try {
      const response = await window.fetch("/readyz", {
        cache: "no-store",
        credentials: "same-origin",
        signal: controller.signal,
      });
      const payload = await response.json();
      showReachableStatus(payload);
      reconnectLiveSocket();
      scheduleProbe();
    } catch (_error) {
      setBuzzStatus("[offline]", "service-status-red");
      scheduleProbe();
    } finally {
      window.clearTimeout(timeout);
      probeInFlight = false;
    }
  }

  function startOfflineProbe() {
    if (stopped || probeTimer || probeInFlight || isSocketConnected()) {
      return;
    }
    scheduleProbe(0);
  }

  function stopOfflineProbe() {
    probeTimer = clearTimer(probeTimer);
  }

  function bindSocketCallbacks() {
    const liveSocket = window.liveSocket;
    socket = liveSocket && typeof liveSocket.getSocket === "function"
      ? liveSocket.getSocket()
      : null;
    if (!socket) {
      return false;
    }
    callbackRefs = [
      socket.onOpen(() => {
        stopOfflineProbe();
      }),
      socket.onClose(() => {
        showOffline();
      }),
      socket.onError(() => {
        showOffline();
      }),
    ];
    if (isSocketConnected()) {
      stopOfflineProbe();
    } else {
      startOfflineProbe();
    }
    return true;
  }

  function start() {
    if (!bindSocketCallbacks()) {
      retryTimer = window.setTimeout(start, 100);
    }
  }

  function stop() {
    stopped = true;
    retryTimer = clearTimer(retryTimer);
    probeTimer = clearTimer(probeTimer);
    if (socket && typeof socket.off === "function" && callbackRefs.length > 0) {
      socket.off(callbackRefs);
    }
    callbackRefs = [];
  }

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
