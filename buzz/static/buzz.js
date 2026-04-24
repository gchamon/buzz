const buzzTableId = "torrent-table";
let _truncObserver = null;

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

document.addEventListener("DOMContentLoaded", initTableIfPresent);
window.addEventListener("phx:navigate", initTableIfPresent);
