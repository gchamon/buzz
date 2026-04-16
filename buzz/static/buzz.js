let buzzPageConfig = {
  tableId: "torrent-table",
  statusSyncId: "status-sync",
  statusLastSyncId: "status-last-sync",
  statusReadyId: "status-ready",
  statusReadyLabelId: "status-ready-label",
  navLogsId: "nav-logs",
  consoleMsgId: "meta-console-msg",
  pollIntervalMs: 3000,
};

function getBuzzElement(id) {
  return id ? document.getElementById(id) : null;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (char) => {
    const entities = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return entities[char];
  });
}

function setReadyLabel(isReady, offline) {
  const readyLabel = getBuzzElement(buzzPageConfig.statusReadyLabelId);
  if (!readyLabel) {
    return;
  }

  readyLabel.classList.remove("service-status-green", "service-status-orange", "service-status-cyan");

  if (offline) {
    readyLabel.innerText = "[offline]";
    readyLabel.classList.add("service-status-cyan");
    return;
  }

  if (isReady) {
    readyLabel.innerText = "[ready]";
    readyLabel.classList.add("service-status-green");
  } else {
    readyLabel.innerText = "[starting]";
    readyLabel.classList.add("service-status-orange");
  }
}

function getRebuildStatusNode() {
  return document.getElementById(buzzPageConfig.consoleMsgId);
}

async function triggerManualRebuild() {
  const status = getRebuildStatusNode();
  if (status) {
    status.innerText = "resyncing library...";
    status.className = "service-status-orange";
  }

  try {
    const res = await fetch("/api/curator/rebuild", { method: "POST" });
    const data = await res.json();
    if (data.error) {
      throw new Error(data.error);
    }

    if (status) {
      status.innerText = "library resynced!";
      status.className = "service-status-green";
    }
  } catch (err) {
    if (status) {
      status.innerText = "resync failed: " + err.message;
      status.className = "service-status-red";
    }
  }
}

async function triggerRestart() {
  if (!confirm("Are you sure you want to restart the stack?")) {
    return;
  }

  const status = getRebuildStatusNode();
  if (status) {
    status.innerText = "restarting service...";
    status.className = "service-status-orange";
  }

  try {
    await fetch("/api/restart", { method: "POST" });
    setTimeout(() => location.reload(), 5000);
  } catch (err) {
    if (status) {
      status.innerText = "restart failed: " + err.message;
      status.className = "service-status-red";
    }
  }
}

async function copyToClipboard(text, successMsg = "copied to clipboard!") {
  const consoleMsg = document.getElementById("meta-console-msg");

  function fallbackCopy(str) {
    const el = document.createElement("textarea");
    el.value = str;
    el.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(el);
    el.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(el);
    if (!ok) throw new Error("execCommand copy failed");
  }

  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      fallbackCopy(text);
    }
    if (consoleMsg) {
      consoleMsg.innerText = successMsg;
      consoleMsg.className = "service-status-green";
      setTimeout(() => {
        if (consoleMsg.innerText === successMsg) consoleMsg.innerText = "";
      }, 3000);
    }
    return true;
  } catch (err) {
    console.error("Failed to copy:", err);
    if (consoleMsg) {
      consoleMsg.innerText = "failed to copy.";
      consoleMsg.className = "service-status-red";
    }
    return false;
  }
}

async function copyLogs() {
  const container = document.getElementById("log-container");
  if (!container || container.innerText.trim() === "Loading logs...") {
    return;
  }

  const entries = container.querySelectorAll(".log-content");
  const logText = Array.from(entries)
    .map((entry) => entry.innerText)
    .join("\n");

  await copyToClipboard(logText, "logs copied to clipboard!");
}

async function pollLogs() {
  const container = document.getElementById("log-container");
  if (!container) {
    return;
  }

  try {
    const res = await fetch("/api/logs?limit=100");
    if (!res.ok) {
      throw new Error("log fetch failed");
    }

    const logs = await res.json();
    if (logs.length === 0) {
      return;
    }

    container.innerHTML = logs
      .map(log => {
        const tsMatch = log.timestamp.match(/T(\d{2}:\d{2}:\d{2})/);
        const ts = tsMatch ? tsMatch[1] : log.timestamp;
        const level = (log.level || "info").toLowerCase();
        const levelClass = `log-level-${level}`;
        const levelLabel = `[${level.toUpperCase()}]`;
        const source = log.source === "curator" ? "buzz-curator" : "buzz-dav";
        const messageText = `${source} ${ts} [${level.toUpperCase()}] ${log.message}`;
        const escapedMessage = escapeHtml(log.message);
        const escapedCopyText = escapeHtml(messageText);
        return `
          <div class="log-entry" data-copy-text="${escapedCopyText}">
            <div class="log-content">
              <span class="log-source">${source}</span>
              <span class="log-ts">${ts}</span>
              <span class="log-level ${levelClass}">${levelLabel}</span>
              <span class="log-message">${escapedMessage}</span>
            </div>
            <button type="button" class="log-copy-btn" title="Copy line" aria-label="Copy log line">
              <i class="fa-regular fa-copy"></i>
            </button>
          </div>`;
      })
      .join("");

    container.scrollTop = container.scrollHeight;
  } catch (err) {
    console.error("Failed to poll logs:", err);
  }
}

document.addEventListener("click", async (event) => {
  const entry = event.target.closest(".log-entry[data-copy-text]");
  if (!entry) {
    return;
  }

  const text = entry.dataset.copyText;
  if (!text) {
    return;
  }

  await copyToClipboard(text);
});

async function pollStatus() {
  const statusSync = getBuzzElement(buzzPageConfig.statusSyncId);
  const statusLastSync = getBuzzElement(buzzPageConfig.statusLastSyncId);
  const navLogs = getBuzzElement(buzzPageConfig.navLogsId);

  try {
    const res = await fetch("/healthz");
    if (!res.ok) {
      throw new Error("Offline");
    }

    const data = await res.json();
    if (statusSync) {
      statusSync.innerText = data.sync_in_progress ? "syncing" : "idle";
    }
    if (statusLastSync) {
      statusLastSync.innerText = data.last_sync_at || "never";
    }
    if (navLogs) {
      const logCount = data.log_count || 0;
      navLogs.innerText = `📜 logs(${logCount})`;
      
      const isLogsPage = window.location.pathname === "/logs";
      
      if (isLogsPage) {
        localStorage.setItem("buzz_seen_logs", logCount.toString());
        localStorage.setItem("buzz_logs_glow", "false");
        navLogs.classList.remove("nav-logs-new");
      } else {
        const seenLogs = parseInt(localStorage.getItem("buzz_seen_logs") || "0");
        if (logCount > seenLogs) {
          localStorage.setItem("buzz_logs_glow", "true");
        }
        
        if (localStorage.getItem("buzz_logs_glow") === "true") {
          navLogs.classList.add("nav-logs-new");
        } else {
          navLogs.classList.remove("nav-logs-new");
        }
      }
    }
    setReadyLabel(data.snapshot_loaded, false);
  } catch (err) {
    if (statusSync) {
      statusSync.innerText = "unknown";
    }
    setReadyLabel(false, true);
  }
}

function initializeReadyLabel() {
  const statusReady = getBuzzElement(buzzPageConfig.statusReadyId);
  if (!statusReady) {
    return;
  }

  setReadyLabel(statusReady.innerText === "true", false);
}

function initBuzzPage(config) {
  buzzPageConfig = {
    ...buzzPageConfig,
    ...config,
  };

  initializeReadyLabel();

  if (buzzPageConfig.pollIntervalMs > 0) {
    setInterval(pollStatus, buzzPageConfig.pollIntervalMs);
    
    // Initial log fetch
    pollLogs();
    // Log polling
    setInterval(() => {
      const autoRefresh = document.getElementById("auto-refresh-logs");
      if (autoRefresh && autoRefresh.checked) {
        pollLogs();
      }
    }, buzzPageConfig.pollIntervalMs);
  }
}

function sortTable(n) {
  const table = getBuzzElement(buzzPageConfig.tableId);
  if (!table) {
    return;
  }

  const headers = table.getElementsByTagName("th");
  let dir = "asc";
  for (let h = 0; h < headers.length; h++) {
    if (h === n) {
      if (headers[h].classList.contains("sort-asc")) {
        headers[h].classList.replace("sort-asc", "sort-desc");
        dir = "desc";
      } else if (headers[h].classList.contains("sort-desc")) {
        headers[h].classList.replace("sort-desc", "sort-asc");
        dir = "asc";
      } else {
        headers[h].classList.add("sort-asc");
        dir = "asc";
      }
    } else {
      headers[h].classList.remove("sort-asc", "sort-desc");
    }
  }

  const tbody = table.tBodies[0];
  if (!tbody) {
    return;
  }

  const rows = Array.from(tbody.rows);
  const rowData = rows.map((row, index) => {
    const cell = row.cells[n];
    const rawValue = cell
      ? cell.getAttribute("data-value") || cell.textContent || ""
      : "";
    const trimmed = rawValue.trim();
    const numValue = trimmed === "" ? Number.NaN : Number(trimmed);
    return {
      row,
      index,
      value: Number.isNaN(numValue) ? trimmed.toLowerCase() : numValue,
      isNumber: !Number.isNaN(numValue),
    };
  });

  rowData.sort((a, b) => {
    let result;
    if (a.isNumber && b.isNumber) {
      result = a.value - b.value;
    } else {
      result = String(a.value).localeCompare(String(b.value));
    }

    if (result === 0) {
      result = a.index - b.index;
    }

    return dir === "asc" ? result : -result;
  });

  const fragment = document.createDocumentFragment();
  rowData.forEach(entry => {
    fragment.appendChild(entry.row);
  });
  tbody.appendChild(fragment);
}


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
  const table = getBuzzElement(buzzPageConfig.tableId);
  if (!table || typeof ResizeObserver === "undefined") return;
  new ResizeObserver(markTruncatedCells).observe(table);
}

function fitTableToViewport() {
  const el = document.getElementById("torrent-table-container");
  if (!el) return;
  const top = el.getBoundingClientRect().top;
  const bottomPadding = 20;
  el.style.height = (window.innerHeight - top - bottomPadding) + "px";
}

function initTableFit() {
  fitTableToViewport();
  window.addEventListener("resize", fitTableToViewport);
}