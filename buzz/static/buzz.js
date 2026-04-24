const buzzTableId = "torrent-table";

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

document.addEventListener("DOMContentLoaded", function () {
  if (document.getElementById(buzzTableId)) {
    initTruncCells();
    initTableFit();
  }
});
