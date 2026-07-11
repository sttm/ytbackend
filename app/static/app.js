const $ = (id) => document.getElementById(id);

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
        <td>${row.youtube_success}/${row.youtube_fail}</td>
        <td title="${row.last_error || ""}">${(row.last_error || "").slice(0, 120)}</td>
        <td><button data-check="${row.id}">Check</button></td>
      </tr>`;
    })
    .join("");
}

async function refresh() {
  const [stats, proxies] = await Promise.all([api("/api/stats"), api("/api/proxies?limit=100")]);
  renderStats(stats);
  renderProxies(proxies.proxies);
}

$("refresh").addEventListener("click", refresh);

$("resolve-stream").addEventListener("click", async () => {
  $("stream-output").textContent = "Resolving...";
  try {
    const url = encodeURIComponent($("youtube-url").value.trim());
    const data = await api(`/api/stream?url=${url}`);
    $("stream-output").textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    $("stream-output").textContent = error.message;
  }
});

$("import-proxies").addEventListener("click", async () => {
  const proxies = $("proxy-input").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  await api("/api/proxies/import", {
    method: "POST",
    body: JSON.stringify({
      proxies,
      source: "dashboard",
      protocol: $("proxy-protocol").value,
    }),
  });
  $("proxy-input").value = "";
  await refresh();
});

$("add-defaults").addEventListener("click", async () => {
  await api("/api/proxy-sources/defaults", { method: "POST", body: "{}" });
  await refresh();
});

$("fetch-sources").addEventListener("click", async () => {
  await api("/api/proxy-sources/fetch", { method: "POST", body: "{}" });
  await refresh();
});

$("check-batch").addEventListener("click", async () => {
  await api("/api/proxies/check-batch?limit=20&status=new", { method: "POST", body: "{}" });
  await refresh();
});

$("proxy-table").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-check]");
  if (!button) return;
  button.disabled = true;
  button.textContent = "Checking...";
  await api(`/api/proxies/${button.dataset.check}/check`, { method: "POST", body: "{}" });
  await refresh();
});

refresh().catch((error) => {
  console.error(error);
});

