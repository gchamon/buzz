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
        this.el.classList.remove("nav-logs-new");
      };
      this.el.addEventListener("click", this._onClick);
    },
    updated() {
      this._updateGlow();
    },
    destroyed() {
      this.el.removeEventListener("click", this._onClick);
    },
    _updateGlow() {
      const countSpan = document.getElementById("nav-log-count");
      const logCount = parseInt(countSpan?.innerText || "0", 10);
      const isLogsPage = window.location.pathname === "/logs";
      if (isLogsPage) {
        localStorage.setItem("buzz_seen_logs", String(logCount));
        this.el.classList.remove("nav-logs-new");
        return;
      }
      const seenLogs = parseInt(
        localStorage.getItem("buzz_seen_logs") || "0", 10
      );
      this.el.classList.toggle("nav-logs-new", logCount > seenLogs);
    },
  };

  window.Hooks = hooks;
}
