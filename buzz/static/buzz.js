let buzzPageConfig = {
  tableId: "torrent-table",
  rebuildStatusTarget: null,
  statusSyncId: "status-sync",
  statusLastSyncId: "status-last-sync",
  statusReadyId: "status-ready",
  statusReadyLabelId: "status-ready-label",
  pollIntervalMs: 3000,
};

function getBuzzElement(id) {
  return id ? document.getElementById(id) : null;
}

function setReadyLabel(isReady, offline) {
  const readyLabel = getBuzzElement(buzzPageConfig.statusReadyLabelId);
  if (!readyLabel) {
    return;
  }

  if (offline) {
    readyLabel.innerText = "[offline]";
    readyLabel.style.color = "var(--cyan)";
    return;
  }

  if (isReady) {
    readyLabel.innerText = "[ready]";
    readyLabel.style.color = "var(--green)";
  } else {
    readyLabel.innerText = "[starting]";
    readyLabel.style.color = "var(--orange)";
  }
}

function createPromptStatusNode() {
  const prompt = document.querySelector(".prompt");
  if (!prompt) {
    return null;
  }

  const status = document.createElement("span");
  prompt.appendChild(status);
  return status;
}

function getRebuildStatusNode() {
  if (buzzPageConfig.rebuildStatusTarget === "prompt") {
    return createPromptStatusNode();
  }

  if (typeof buzzPageConfig.rebuildStatusTarget === "string") {
    return document.querySelector(buzzPageConfig.rebuildStatusTarget);
  }

  return null;
}

async function triggerManualRebuild() {
  const status = getRebuildStatusNode();
  if (status) {
    status.innerText = buzzPageConfig.rebuildStatusTarget === "prompt"
      ? " Resyncing library..."
      : "Resyncing library...";
    status.style.color = "var(--orange)";
  }

  try {
    const res = await fetch("/api/curator/rebuild", { method: "POST" });
    const data = await res.json();
    if (data.error) {
      throw new Error(data.error);
    }

    if (status) {
      status.innerText = buzzPageConfig.rebuildStatusTarget === "prompt"
        ? " Library resynced!"
        : "Library resynced!";
      status.style.color = "var(--green)";
    }
  } catch (err) {
    if (status) {
      status.innerText = buzzPageConfig.rebuildStatusTarget === "prompt"
        ? " Resync failed: " + err.message
        : "Resync failed: " + err.message;
      status.style.color = "var(--red)";
    }
  }

  if (status && buzzPageConfig.rebuildStatusTarget === "prompt") {
    setTimeout(() => status.remove(), 3000);
  }
}

async function pollStatus() {
  const statusSync = getBuzzElement(buzzPageConfig.statusSyncId);
  const statusLastSync = getBuzzElement(buzzPageConfig.statusLastSyncId);

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
