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
    if (successMsg) {
      setBuzzConsole(successMsg, "service-status-green");
    }
  } catch (_error) {
    if (successMsg) {
      setBuzzConsole("failed to copy.", "service-status-red");
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

async function buzzCopyTaskLogs(taskId) {
  const row = document.querySelector(`tr[phx-value-task_id="${taskId}"]`);
  if (!row) {
    return;
  }
  const logRow = row.nextElementSibling;
  if (!logRow || !logRow.classList.contains("thread-log-row")) {
    return;
  }
  const entries = logRow.querySelectorAll(".log-entry[data-copy-text]");
  const text = Array.from(entries)
    .map((entry) => entry.getAttribute("data-copy-text") || "")
    .filter((value) => value !== "")
    .join("\n");

  if (text) {
    await buzzCopyToClipboard(text, "task logs copied to clipboard!");
  }
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

  function buzzDedupeIdentityForms(root = document) {
    root.querySelectorAll(".identity-section").forEach((section) => {
      const forms = Array.from(section.querySelectorAll(".curator-title-form"));
      if (forms.length <= 1) return;
      const keep = forms.find((form) => form.querySelector(".identity-inputs")) || forms[0];
      forms.forEach((form) => {
        if (form !== keep) form.remove();
      });
    });
  }

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
    _classes() {
      try {
        const classes = JSON.parse(this.el.dataset.classes || "{}");
        return classes && typeof classes === "object" && !Array.isArray(classes)
          ? classes
          : {};
      } catch (_error) {
        return {};
      }
    },
    _setValue(value, classes) {
      Object.values(classes).forEach((className) => {
        if (typeof className === "string" && className) {
          this.el.classList.remove(className);
        }
      });
      this.el.textContent = value;
      const className = classes[value];
      if (typeof className === "string" && className) {
        this.el.classList.add(className);
      }
    },
    _start() {
      const values = this._values();
      const classes = this._classes();
      if (values.length <= 1) {
        if (values.length === 1) {
          this._setValue(values[0], classes);
        }
        return;
      }
      this._setValue(values[0], classes);
      this._timer = window.setInterval(() => {
        this._index = (this._index + 1) % values.length;
        this._setValue(values[this._index], classes);
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

  hooks.BuzzIdentityRevert = {
    mounted() {
      this._prevConsoleMsg = document.getElementById("meta-console-msg")?.textContent;
      this._updateOverriddenInputs = () => {
        const isDirty = (input) => {
          const saved = input.dataset.saved || "";
          const def = input.dataset.default || "";
          // Not dirty if value matches what's already saved.
          // Also not dirty if nothing is saved yet and value matches the
          // auto-derived default (pristine prefill, no action needed).
          return input.value !== saved && (saved !== "" || input.value !== def);
        };
        const inputs = this.el.querySelectorAll(".identity-inputs input");
        inputs.forEach((input) => {
          input.classList.toggle("input-overridden", isDirty(input));
        });
        this._identitySave?.classList.toggle("save-dirty", Array.from(inputs).some(isDirty));
        const regexDirty = this._regexInput ? isDirty(this._regexInput) : false;
        this._regexInput?.classList.toggle("input-overridden", regexDirty);
        this._regexSave?.classList.toggle("save-dirty", regexDirty);
      };
      this._maybeRebaselineSaved = () => {
        const msg = document.getElementById("meta-console-msg")?.textContent;
        const transitioned =
          msg === "curator title override updated" && this._prevConsoleMsg !== msg;
        this._prevConsoleMsg = msg;
        if (!transitioned) return;
        this.el.querySelectorAll(".identity-inputs input").forEach((input) => {
          input.dataset.saved = input.value;
        });
        if (this._regexInput) this._regexInput.dataset.saved = this._regexInput.value;
      };
      this._onSaveClick = () => {
        this.el.querySelectorAll(".identity-inputs input").forEach((input) => {
          input.dataset.saved = input.value;
        });
        if (this._regexInput) this._regexInput.dataset.saved = this._regexInput.value;
      };
      this._bind = () => {
        this._button?.removeEventListener("click", this._onClick);
        this._container?.removeEventListener("input", this._updateOverriddenInputs);
        this._regexInput?.removeEventListener("input", this._updateOverriddenInputs);
        this._identitySave?.removeEventListener("click", this._onSaveClick);
        this._regexSave?.removeEventListener("click", this._onSaveClick);
        this._dedupeForms();
        this._button = this.el.querySelector(".curator-title-form [data-revert]");
        this._button?.addEventListener("click", this._onClick);
        this._identitySave = this.el.querySelector(".curator-title-form [data-identity-save]");
        this._identitySave?.addEventListener("click", this._onSaveClick);
        this._container = this.el.querySelector(".identity-inputs");
        this._container?.addEventListener("input", this._updateOverriddenInputs);
        const idParts = this.el.id.replace("identity-section-", "").split("-");
        const kind = idParts.pop();
        const domId = idParts.join("-");
        this._fileSectionId = kind ? `file-section-${domId}` : "";
        const fileSection = this._fileSectionId
          ? document.getElementById(this._fileSectionId)
          : null;
        this._regexInput = fileSection?.querySelector("[data-parse-regex-input]");
        this._regexSave = fileSection?.querySelector("[data-regex-save]");
        this._regexInput?.addEventListener("input", this._updateOverriddenInputs);
        this._regexSave?.addEventListener("click", this._onSaveClick);
      };
      this._dedupeForms = () => {
        buzzDedupeIdentityForms(this.el);
      };
      this._onClick = (event) => {
        event.preventDefault();
        const form = this.el.querySelector(".curator-title-form");
        const inputs = form?.querySelectorAll(".identity-inputs input") || [];
        inputs.forEach((input) => {
          input.value = input.dataset.default || "";
        });
        this._updateOverriddenInputs();
      };
      this._bind();
      this._updateOverriddenInputs();
    },
    updated() {
      this._bind?.();
      this._maybeRebaselineSaved?.();
      this._updateOverriddenInputs?.();
    },
    destroyed() {
      this._button?.removeEventListener("click", this._onClick);
      this._identitySave?.removeEventListener("click", this._onSaveClick);
      this._regexSave?.removeEventListener("click", this._onSaveClick);
      this._container?.removeEventListener("input", this._updateOverriddenInputs);
      this._regexInput?.removeEventListener("input", this._updateOverriddenInputs);
    },
  };

  document.addEventListener("phx:update", () => buzzDedupeIdentityForms());

  window.Hooks = hooks;
}
