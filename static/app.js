/* ──────────────────────────────────────────────
   StoreRip — frontend logic
   fetch · poll · ASCII progress · download
   ────────────────────────────────────────────── */

(function () {
  "use strict";

  const POLL_MS = 1500;
  const BAR_WIDTH = 32;

  let currentJob = null;
  let pollTimer = null;
  let lastLogLen = 0;

  // ── Element refs ──
  const $ = (id) => document.getElementById(id);
  const steps = {
    input: $("step-input"),
    progress: $("step-progress"),
    done: $("step-done"),
    error: $("step-error"),
  };

  // ── ASCII progress bar ──
  function asciiBar(pct, width = BAR_WIDTH) {
    pct = Math.max(0, Math.min(100, Math.round(pct)));
    const filled = Math.floor((pct / 100) * width);
    return "█".repeat(filled) + "░".repeat(width - filled) + `  ${pct}%`;
  }

  // ── Step switching ──
  function show(name) {
    Object.values(steps).forEach((el) => el.classList.add("hidden"));
    steps[name].classList.remove("hidden");
  }

  // ── Toggle groups ──
  function wireToggle(groupId) {
    const group = $(groupId);
    group.querySelectorAll(".toggle").forEach((btn) => {
      btn.addEventListener("click", () => {
        group.querySelectorAll(".toggle").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        group.dataset.value = btn.dataset.value;
      });
    });
  }
  function toggleValue(groupId) {
    return $(groupId).dataset.value;
  }
  function setToggle(groupId, value) {
    const group = $(groupId);
    let matched = false;
    group.querySelectorAll(".toggle").forEach((btn) => {
      const on = btn.dataset.value === value;
      btn.classList.toggle("active", on);
      if (on) matched = true;
    });
    if (matched) group.dataset.value = value;
  }

  wireToggle("platform-group");
  wireToggle("output-group");

  // ── Detect platform ──
  $("btn-detect").addEventListener("click", async () => {
    const url = $("url").value.trim();
    const result = $("detect-result");
    if (!url) {
      result.textContent = "// enter a URL first";
      result.className = "detect-result none";
      return;
    }
    result.textContent = "// detecting...";
    result.className = "detect-result";
    try {
      const r = await fetch("/api/detect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const data = await r.json();
      if (data.platform) {
        result.textContent = `// ${data.platform.toUpperCase()} detected`;
        result.className = "detect-result found";
        setToggle("platform-group", data.platform);
      } else {
        result.textContent = "// platform not detected";
        result.className = "detect-result none";
      }
    } catch (e) {
      result.textContent = "// detection failed";
      result.className = "detect-result none";
    }
  });

  // ── Run scrape ──
  $("btn-run").addEventListener("click", async () => {
    const url = $("url").value.trim();
    if (!url) {
      const result = $("detect-result");
      result.textContent = "// URL required";
      result.className = "detect-result none";
      return;
    }
    const body = {
      url,
      target: toggleValue("platform-group"),
      output: toggleValue("output-group"),
      enhance: $("enhance").checked,
    };

    try {
      const r = await fetch("/api/scrape", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json();
      if (!r.ok) {
        showError(data.error || "Could not start scrape.");
        return;
      }
      currentJob = data.job_id;
      lastLogLen = 0;
      $("p-jobid").textContent = currentJob;
      $("p-status").textContent = "RUNNING";
      $("p-log").textContent = "";
      $("p-bar").textContent = asciiBar(0);
      show("progress");
      startPolling();
    } catch (e) {
      showError("Network error starting scrape.");
    }
  });

  // ── Polling ──
  function startPolling() {
    stopPolling();
    pollTimer = setInterval(poll, POLL_MS);
    poll();
  }
  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  async function poll() {
    if (!currentJob) return;
    try {
      const r = await fetch(`/api/status/${currentJob}`);
      if (r.status === 404) {
        stopPolling();
        return;
      }
      const s = await r.json();

      // Log
      if (Array.isArray(s.log)) {
        $("p-log").textContent = s.log.join("\n");
        $("p-log").scrollTop = $("p-log").scrollHeight;
      }
      // Bar
      $("p-bar").textContent = asciiBar(s.progress || 0);
      $("p-status").textContent = (s.status || "running").toUpperCase();

      if (s.status === "done") {
        stopPolling();
        renderDone(s);
      } else if (s.status === "error") {
        stopPolling();
        showError(s.error || "Scrape failed.");
      }
    } catch (e) {
      // transient — keep polling
    }
  }

  // ── Done ──
  function renderDone(s) {
    $("d-products").textContent = s.product_count ?? 0;
    $("d-rows").textContent = s.row_count ?? 0;
    $("d-platform").textContent = s.platform ? s.platform.toUpperCase() : "—";
    $("d-output").textContent = s.output ? s.output.toUpperCase() : "—";
    $("d-enhanced").textContent = s.enhanced ? "Yes" : "No";
    show("done");
  }

  $("btn-download").addEventListener("click", () => {
    if (currentJob) {
      window.location = `/api/download/${currentJob}`;
      // job is consumed server-side after download
    }
  });

  // ── Abort ──
  $("btn-abort").addEventListener("click", async () => {
    stopPolling();
    if (currentJob) {
      try {
        await fetch(`/api/abort/${currentJob}`, { method: "POST" });
      } catch (e) { /* ignore */ }
    }
    resetToInput();
  });

  // ── Error ──
  function showError(msg) {
    $("e-message").textContent = msg;
    show("error");
  }

  // ── Reset ──
  function resetToInput() {
    stopPolling();
    currentJob = null;
    show("input");
  }
  $("btn-new").addEventListener("click", resetToInput);
  $("btn-retry").addEventListener("click", resetToInput);

  // ── Init ──
  show("input");
})();
