async function buzzCopyToClipboard(text, successMsg = "copied to clipboard!") {
  const consoleMsg = document.getElementById("meta-console-msg");

  function fallbackCopy(value) {
    const element = document.createElement("textarea");
    element.value = value;
    element.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(element);
    element.select();
    const copied = document.execCommand("copy");
    document.body.removeChild(element);
    if (!copied) {
      throw new Error("execCommand copy failed");
    }
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
    }
  } catch (_error) {
    if (consoleMsg) {
      consoleMsg.innerText = "failed to copy.";
      consoleMsg.className = "service-status-red";
    }
  }
}

async function buzzCopyTextById(elementId, successMsg) {
  const element = document.getElementById(elementId);
  if (!element) {
    return;
  }
  await buzzCopyToClipboard(element.innerText, successMsg);
}

async function buzzCopyVisibleLogs() {
  const entries = document.querySelectorAll(".log-entry[data-copy-text]");
  const text = Array.from(entries)
    .map((entry) => entry.getAttribute("data-copy-text") || "")
    .filter((value) => value !== "")
    .join("\n");

  if (text) {
    await buzzCopyToClipboard(text, "logs copied to clipboard!");
  }
}

async function buzzCopyLogLine(button) {
  const text = button.getAttribute("data-copy-text");
  if (!text) {
    return;
  }
  await buzzCopyToClipboard(text);
}

function buzzHighlightYamlElement(root) {
  if (
    typeof window === "undefined" ||
    typeof window.Prism === "undefined" ||
    typeof window.Prism.highlightElement !== "function" ||
    !root
  ) {
    return;
  }

  const code = root.matches("code")
    ? root
    : root.querySelector("code.language-yaml");
  if (code) {
    window.Prism.highlightElement(code);
  }
}

if (typeof window !== "undefined") {
  const hooks = window.Hooks || {};

  hooks.BuzzPrismYaml = {
    mounted() {
      window.requestAnimationFrame(() => {
        buzzHighlightYamlElement(this.el);
      });
    },

    updated() {
      window.requestAnimationFrame(() => {
        buzzHighlightYamlElement(this.el);
      });
    },
  };

  hooks.BuzzLogGlow = {
    mounted() {
      this._updateGlow();
      this._onClick = () => {
        const countSpan = document.getElementById("nav-log-count");
        const logCount = parseInt(countSpan?.innerText || "0", 10);
        localStorage.setItem("buzz_seen_logs", String(logCount));
        this._clearGlow();
      };
      this.el.addEventListener("click", this._onClick);
    },
    updated() {
      this._updateGlow();
    },
    destroyed() {
      this.el.removeEventListener("click", this._onClick);
    },
    _clearGlow() {
      this.el.classList.remove("nav-logs-new-warning", "nav-logs-new-error");
    },
    _setGlow(level) {
      this._clearGlow();
      if (level === "error") {
        this.el.classList.add("nav-logs-new-error");
      } else if (level === "warning") {
        this.el.classList.add("nav-logs-new-warning");
      }
    },
    _updateGlow() {
      const countSpan = document.getElementById("nav-log-count");
      const logCount = parseInt(countSpan?.innerText || "0", 10);
      const currentLevel = this.el.dataset.logLevel || "info";
      const isLogsPage = window.location.pathname === "/logs";
      if (isLogsPage) {
        localStorage.setItem("buzz_seen_logs", String(logCount));
        this._clearGlow();
        return;
      }
      const seenLogs = parseInt(
        localStorage.getItem("buzz_seen_logs") || "0", 10
      );
      const priority = { error: 3, warning: 2, info: 1, debug: 0 };
      const currentP = priority[currentLevel] || 0;
      if (logCount > seenLogs && currentP >= 2) {
        this._setGlow(currentLevel);
      } else {
        this._clearGlow();
      }
    },
  };

  hooks.BuzzMetaCycle = {
    mounted() {
      this._index = 0;
      this._start();
    },
    updated() {
      this._stop();
      this._index = 0;
      this._start();
    },
    destroyed() {
      this._stop();
    },
    _values() {
      try {
        const values = JSON.parse(this.el.dataset.values || "[]");
        return Array.isArray(values) ? values.map(String).filter(Boolean) : [];
      } catch (_error) {
        return [];
      }
    },
    _start() {
      const values = this._values();
      if (values.length <= 1) {
        if (values.length === 1) {
          this.el.textContent = values[0];
        }
        return;
      }
      this.el.textContent = values[0];
      this._timer = window.setInterval(() => {
        this._index = (this._index + 1) % values.length;
        this.el.textContent = values[this._index];
      }, 3000);
    },
    _stop() {
      if (this._timer) {
        window.clearInterval(this._timer);
        this._timer = null;
      }
    },
  };

  hooks.BuzzBulkMagnetDraft = {
    mounted() {
      this._textarea = this.el.querySelector(".bulk-magnet-input");
      this._onInput = () => {
        if (!this._textarea) return;
        window.buzzBulkMagnetDraft = this._textarea.value;
      };
      this._restore();
      if (this._textarea) {
        this._textarea.addEventListener("input", this._onInput);
      }
    },
    updated() {
      this._restore();
    },
    destroyed() {
      this._textarea?.removeEventListener("input", this._onInput);
    },
    _restore() {
      const consoleMsg = document.getElementById("meta-console-msg");
      if (consoleMsg?.textContent === "Items added and synced.") {
        window.buzzBulkMagnetDraft = "";
      }
      if (typeof window.buzzBulkMagnetDraft === "string") {
        if (!this._textarea) return;
        this._textarea.value = window.buzzBulkMagnetDraft;
      }
    },
  };

  hooks.BuzzOverflowMarquee = {
    mounted() {
      this._reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
      this._onReducedMotionChange = () => this._measureAll();
      this._reducedMotion.addEventListener("change", this._onReducedMotionChange);
      this._resizeObserver = new ResizeObserver(() => this._measureAll());
      this._resizeObserver.observe(this.el);
      this._measureAll();
    },
    updated() {
      this._measureAll();
    },
    destroyed() {
      this._resizeObserver?.disconnect();
      this._reducedMotion?.removeEventListener("change", this._onReducedMotionChange);
    },
    _measureAll() {
      const clips = this.el.querySelectorAll("[data-marquee-clip]");
      const reduced = this._reducedMotion.matches;
      clips.forEach((clip) => {
        const label = clip.querySelector("[data-marquee-label]");
        if (!label) return;
        const overflow = label.scrollWidth - clip.clientWidth;
        if (!reduced && overflow > 0) {
          clip.dataset.overflowing = "true";
          clip.style.setProperty("--marquee-distance", `${overflow}px`);
          const duration = Math.min(12, Math.max(3, overflow / 60));
          clip.style.setProperty("--marquee-duration", `${duration}s`);
        } else {
          delete clip.dataset.overflowing;
          clip.style.removeProperty("--marquee-distance");
          clip.style.removeProperty("--marquee-duration");
        }
      });
    },
  };

  window.Hooks = hooks;
}
