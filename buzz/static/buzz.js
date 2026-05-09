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

function setBuzzConsole(message, className, fadeTimeout = 5000) {
  const element = document.getElementById("meta-console-msg");
  if (!element) return;
  if (element._fadeTimer) {
    clearTimeout(element._fadeTimer);
    element._fadeTimer = null;
  }
  element.textContent = message;
  element.className = className;
  if (fadeTimeout > 0) {
    element._fadeTimer = setTimeout(() => {
      element.textContent = "";
      element.className = "";
      element._fadeTimer = null;
    }, fadeTimeout);
  }
}

function createBuzzSocketStatusMonitor() {
  const probeIntervalMs = 1000;
  const probeTimeoutMs = 1500;
  const wakeCooldownMs = 750;
  const reconnectCooldownMs = 5000;
  let socket = null;
  let callbackRefs = [];
  let wakeListeners = [];
  let retryTimer = null;
  let probeTimer = null;
  let wakeCooldownTimer = null;
  let probeInFlight = false;
  let reconnectInFlight = false;
  let lastReconnectAt = 0;
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
    if (isReady && isSocketConnected()) {
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
    if (reconnectInFlight) {
      return;
    }
    const now = Date.now();
    if (now - lastReconnectAt < reconnectCooldownMs) {
      return;
    }
    reconnectInFlight = true;
    lastReconnectAt = now;
    liveSocket.connect();
    setTimeout(() => {
      reconnectInFlight = false;
    }, reconnectCooldownMs);
  }

  function scheduleProbe(delay = probeIntervalMs) {
    if (stopped || isSocketConnected()) {
      return;
    }
    probeTimer = clearTimer(probeTimer);
    probeTimer = window.setTimeout(runProbe, delay);
  }

  async function runProbe(immediate = false) {
    if (stopped || probeInFlight || (!immediate && isSocketConnected())) {
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

      if (!isSocketConnected()) {
        scheduleProbe();
      }
    } catch (_error) {
      if (!isSocketConnected()) {
        setBuzzStatus("[offline]", "service-status-red");
        scheduleProbe();
      }
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

  function handleWakeEvent() {
    wakeCooldownTimer = clearTimer(wakeCooldownTimer);
    if (stopped || isSocketConnected()) {
      return;
    }
    wakeCooldownTimer = window.setTimeout(() => {
      wakeCooldownTimer = null;
      runProbe(true);
    }, wakeCooldownMs);
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
        showReachableStatus({ ui_status: "ready", status: "ready" });
        setBuzzConsole("connected to the server!", "service-status-green");
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

  function bindWakeListeners() {
    wakeListeners = [
      { event: "visibilitychange", target: document, handler: handleWakeEvent },
      { event: "pageshow", target: window, handler: handleWakeEvent },
      { event: "focus", target: window, handler: handleWakeEvent },
      { event: "online", target: window, handler: handleWakeEvent },
    ];
    wakeListeners.forEach(({ event, target, handler }) => {
      target.addEventListener(event, handler);
    });
  }

  function unbindWakeListeners() {
    wakeListeners.forEach(({ event, target, handler }) => {
      target.removeEventListener(event, handler);
    });
    wakeListeners = [];
  }

  function start() {
    if (!bindSocketCallbacks()) {
      retryTimer = window.setTimeout(start, 100);
      return;
    }
    bindWakeListeners();
  }

  function stop() {
    stopped = true;
    retryTimer = clearTimer(retryTimer);
    probeTimer = clearTimer(probeTimer);
    wakeCooldownTimer = clearTimer(wakeCooldownTimer);
    unbindWakeListeners();
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
