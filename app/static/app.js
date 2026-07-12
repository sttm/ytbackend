const $ = (id) => document.getElementById(id);
const state = {
  page: 1,
  pages: 1,
  total: 0,
  logSeq: 0,
};

function logStep(message, data) {
  const list = $("activity-log");
  const item = document.createElement("li");
  const time = new Date().toLocaleTimeString();
  item.innerHTML = `<strong>${String(++state.logSeq).padStart(2, "0")}</strong> ${time} · ${message}`;
  if (data) {
    const details = document.createElement("pre");
    details.textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
    item.appendChild(details);
  }
  list.prepend(item);
}

function setImportOutput(message, data) {
  $("import-output").textContent = data ? `${message}\n${JSON.stringify(data, null, 2)}` : message;
  logStep(message, data);
}

function protocolForProxySource(urlOrText) {
  const value = String(urlOrText || "").toLowerCase();
  const selected = $("proxy-protocol").value;
  if (selected !== "auto") return selected;
  if (value.includes("socks5")) return "socks5";
  if (value.includes("socks4")) return "socks4";
  if (value.includes("http")) return "http";
  return "http";
}

async function withButtonBusy(button, label, work) {
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try {
    return await work();
  } catch (error) {
    setImportOutput(`Error: ${error.message || error}`);
  } finally {
    button.disabled = false;
    button.textContent = previousText;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { raw: text };
  }
  if (!response.ok) {
    throw new Error(data.detail || data.error || response.statusText);
  }
  return data;
}

function renderStats(data) {
  const items = [
    ["Total proxies", data.proxies.total],
    ["Verified", data.proxies.verified],
    ["Blocked", data.proxies.blocked],
    ["Dead", data.proxies.dead],
    ["Avg latency", `${data.proxies.avg_latency_ms} ms`],
    ["Cached streams", data.streams.cached],
  ];
  $("stats").innerHTML = items
    .map(([label, value]) => `<div class="stat"><span>${label}</span><strong>${value}</strong></div>`)
    .join("");
}

function renderProxies(rows) {
  $("proxy-table").innerHTML = rows
    .map((row) => {
      const statusClass = row.is_verified ? "verified" : row.is_active ? "" : "bad";
      return `<tr>
        <td>${row.id}</td>
        <td><code>${row.proxy_url}</code></td>
        <td><span class="badge ${statusClass}">${row.status}</span></td>
        <td>${row.score}</td>
        <td>${row.latency_ms} ms</td>
        <td>${row.download_ms || 0} ms</td>
        <td>${row.youtube_success}/${row.youtube_fail}</td>
        <td title="${row.last_error || ""}">${(row.last_error || "").slice(0, 120)}</td>
        <td class="row-actions">
          <button data-check="${row.id}">Ping</button>
          <button data-delete="${row.id}" class="danger">Delete</button>
        </td>
      </tr>`;
    })
    .join("");
}

async function refresh() {
  logStep("Refresh stats and proxy table");
  const limit = Number($("page-size").value || 50);
  const offset = (state.page - 1) * limit;
  const status = $("status-filter").value;
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status) params.set("status", status);
  const [stats, proxies] = await Promise.all([api("/api/stats"), api(`/api/proxies?${params}`)]);
  state.page = proxies.page;
  state.pages = proxies.pages;
  state.total = proxies.total;
  renderStats(stats);
  renderProxies(proxies.proxies);
  $("page-info").textContent = `Page ${state.page} / ${state.pages} · ${state.total} proxies`;
  $("prev-page").disabled = state.page <= 1;
  $("next-page").disabled = state.page >= state.pages;
}

$("refresh").addEventListener("click", refresh);

$("resolve-stream").addEventListener("click", async () => {
  $("stream-output").textContent = "Resolving...";
  logStep("Resolve stream start");
  try {
    const url = encodeURIComponent($("youtube-url").value.trim());
    const data = await api(`/api/stream?url=${url}`);
    $("stream-output").textContent = JSON.stringify(data, null, 2);
    logStep("Resolve stream complete", {
      cached: data.cached,
      format_id: data.format_id,
      proxy_used: data.proxy_used || "direct",
    });
  } catch (error) {
    $("stream-output").textContent = error.message;
    logStep(`Resolve stream failed: ${error.message || error}`);
  }
});

$("import-proxies").addEventListener("click", async (event) => {
  await withButtonBusy(event.currentTarget, "Importing...", async () => {
    const proxies = $("proxy-input").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (!proxies.length) {
      setImportOutput("Paste at least one proxy first.");
      return;
    }
    logStep("Parse pasted proxy list", { loaded: proxies.length });
    const protocol = protocolForProxySource(proxies.join("\n"));
    setImportOutput(`Importing ${proxies.length} proxies...\nProtocol: ${protocol}\nMode: ${$("check-mode").value}`);
    const result = await api("/api/proxies/import", {
      method: "POST",
      body: JSON.stringify({
        proxies,
        source: "dashboard",
        protocol,
        check_before_add: $("check-before-add").checked,
        check_mode: $("check-mode").value,
        check_limit: Number($("check-limit").value || 100),
      }),
    });
    setImportOutput("Import complete.", result);
    $("proxy-input").value = "";
    state.page = 1;
    await refresh();
  });
});

$("import-proxy-url").addEventListener("click", async (event) => {
  await withButtonBusy(event.currentTarget, "Importing URL...", async () => {
    const url = $("proxy-url-input").value.trim();
    if (!url) {
      setImportOutput("Paste a proxy .txt URL first.");
      return;
    }
    const protocol = protocolForProxySource(url);
    logStep("Proxy URL import start", { url, protocol });
    setImportOutput(`Loading ${url}\nProtocol: ${protocol}\nCheck before add: ${$("check-before-add").checked ? "yes" : "no"}\nMode: ${$("check-mode").value}`);
    const result = await api("/api/proxies/import-url", {
      method: "POST",
      body: JSON.stringify({
        url,
        source: url,
        protocol,
        check_before_add: $("check-before-add").checked,
        check_mode: $("check-mode").value,
        check_limit: Number($("check-limit").value || 100),
      }),
    });
    setImportOutput("Import URL complete.", result);
    state.page = 1;
    await refresh();
  });
});

$("add-defaults").addEventListener("click", async () => {
  try {
    logStep("Add default proxy sources");
    const result = await api("/api/proxy-sources/defaults", { method: "POST", body: "{}" });
    setImportOutput("Default sources added.", result);
    await refresh();
  } catch (error) {
    setImportOutput(`Error: ${error.message || error}`);
  }
});

$("fetch-sources").addEventListener("click", async (event) => {
  await withButtonBusy(event.currentTarget, "Fetching...", async () => {
    logStep("Fetch proxy sources start", {
      check_before_add: $("check-before-add").checked,
      check_limit_per_source: Number($("check-limit").value || 100),
    });
    setImportOutput("Fetching default sources...");
    const result = await api(`/api/proxy-sources/fetch?check_before_add=${$("check-before-add").checked}&check_limit_per_source=${Number($("check-limit").value || 100)}`, { method: "POST", body: "{}" });
    setImportOutput("Fetch sources complete.", result);
    state.page = 1;
    await refresh();
  });
});

$("check-batch").addEventListener("click", async (event) => {
  await withButtonBusy(event.currentTarget, "Checking...", async () => {
    logStep("Batch proxy check start", { limit: 20, status: "new" });
    const result = await api("/api/proxies/check-batch?limit=20&status=new", { method: "POST", body: "{}" });
    setImportOutput("Batch check complete.", result);
    await refresh();
  });
});

$("clear-proxies").addEventListener("click", async (event) => {
  if (!confirm("Clear all proxies from the table?")) return;
  await withButtonBusy(event.currentTarget, "Clearing...", async () => {
    logStep("Clear proxy table start");
    const result = await api("/api/proxies", { method: "DELETE" });
    setImportOutput("Proxy table cleared.", result);
    state.page = 1;
    await refresh();
  });
});

$("proxy-table").addEventListener("click", async (event) => {
  const checkButton = event.target.closest("button[data-check]");
  if (checkButton) {
    checkButton.disabled = true;
    checkButton.textContent = "Checking...";
    try {
      logStep("Single proxy ping start", { id: checkButton.dataset.check });
      const result = await api(`/api/proxies/${checkButton.dataset.check}/check`, { method: "POST", body: "{}" });
      logStep("Single proxy ping complete", result.result);
      await refresh();
    } catch (error) {
      setImportOutput(`Error: ${error.message || error}`);
    } finally {
      checkButton.disabled = false;
      checkButton.textContent = "Ping";
    }
    return;
  }

  const deleteButton = event.target.closest("button[data-delete]");
  if (!deleteButton) return;
  logStep("Delete proxy", { id: deleteButton.dataset.delete });
  await api(`/api/proxies/${deleteButton.dataset.delete}`, { method: "DELETE" });
  await refresh();
});

$("clear-log").addEventListener("click", () => {
  $("activity-log").innerHTML = "";
  state.logSeq = 0;
});

$("prev-page").addEventListener("click", async () => {
  state.page = Math.max(1, state.page - 1);
  await refresh();
});

$("next-page").addEventListener("click", async () => {
  state.page = Math.min(state.pages, state.page + 1);
  await refresh();
});

$("page-size").addEventListener("change", async () => {
  state.page = 1;
  await refresh();
});

$("status-filter").addEventListener("change", async () => {
  state.page = 1;
  await refresh();
});

refresh().catch((error) => {
  console.error(error);
  setImportOutput(`Dashboard refresh failed: ${error.message || error}`);
});
