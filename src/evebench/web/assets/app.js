const EXAMPLE = {
  ddl: `CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  age INTEGER
);`,
  left: `SELECT name
FROM users
WHERE age >= 25;`,
  right: `SELECT name
FROM users
WHERE age > 24;`,
};

const state = { checkers: [] };

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  const node = document.createElement("div");
  node.textContent = value ?? "";
  return node.innerHTML;
}

function labelFor(id) {
  return state.checkers.find((item) => item.checker.checker_id === id)?.display_name || id;
}

function loadExample() {
  $("#schema-ddl").value = EXAMPLE.ddl;
  $("#query-left").value = EXAMPLE.left;
  $("#query-right").value = EXAMPLE.right;
}

function backendOptions(id) {
  if (id === "verieql") {
    return `<div class="advanced-grid">
      <div class="field">
        <label for="verieql-bound">Row bound <span>1–100</span></label>
        <input id="verieql-bound" type="number" value="2" min="1" max="100">
      </div>

    </div>`;
  }
  if (id === "parseval") {
    return `<div class="field">
      <label for="parseval-connection">Database server URL <span>required</span></label>
      <input id="parseval-connection" type="text" placeholder="sqlite:////tmp/evebench-case.db" autocomplete="off">
      <small>ParSEval creates and removes an isolated scratch database for each attempt.</small>
    </div>`;
  }
  return `<p class="config-note">No other per-run configuration is required. This backend uses the server's configured runtime.</p>`;
}

function toggleBackendCard(input) {
  const card = input.closest(".backend-card");
  card.classList.toggle("is-enabled", input.checked);
  input.nextElementSibling.setAttribute("aria-expanded", String(input.checked));
}

async function loadCheckers() {
  const container = $("#backend-options");
  try {
    const response = await fetch("/v1/checkers");
    if (!response.ok) throw new Error("Could not load checker capabilities.");
    state.checkers = await response.json();
    container.innerHTML = state.checkers.map((item) => {
      const id = item.checker.checker_id;
      const reason = item.unavailable_reason ? ` title="${escapeHtml(item.unavailable_reason)}"` : "";
      const version = escapeHtml(item.checker.backend_version);
      return `<div class="backend-card backend-check"${reason}>
        <input type="checkbox" id="checker-${id}" data-checker-id="${id}" ${item.available ? "" : "disabled"}>
        <label for="checker-${id}" aria-expanded="false">
          <span class="check-ui">✓</span>
          <span class="backend-title">${escapeHtml(item.display_name)}<small>Backend v${version}</small></span>
          <span class="collapse-icon" aria-hidden="true">›</span>
        </label>
        <div class="backend-config">
          <div class="backend-config-inner">
            <div class="backend-config-body">${backendOptions(id)}</div>
          </div>
        </div>
      </div>`;
    }).join("");
    container.querySelectorAll(".backend-check > input").forEach((input) => {
      input.addEventListener("change", () => toggleBackendCard(input));
    });
  } catch (error) {
    container.innerHTML = `<div class="error-box">${escapeHtml(error.message)}</div>`;
  }
}

function selectedCheckers() {
  const knownIds = new Set(state.checkers.map((item) => item.checker.checker_id));
  return [...document.querySelectorAll("input[data-checker-id]:checked")]
    .map((input) => input.dataset.checkerId)
    .filter((id) => knownIds.has(id));
}

function resultCard(result, index) {
  const verdict = result.verdict || (result.execution_status === "completed" ? "unknown" : "error");
  const message = result.message ? `<p class="result-message">${escapeHtml(result.message)}</p>` : "";
  return `<article class="result-card" style="animation-delay:${index * 55}ms">
    <div class="result-card-top">
      <div>
        <div class="backend-name">${escapeHtml(labelFor(result.checker.checker_id))}</div>
        <div class="backend-version">v${escapeHtml(result.checker.backend_version)}</div>
      </div>
      <span class="verdict ${escapeHtml(verdict)}">${escapeHtml(verdict.replaceAll("_", " "))}</span>
    </div>
    <div class="result-meta">
      <span>${escapeHtml(result.duration_ms)} ms</span>
      ${result._dialect ? `<span>${escapeHtml(result._dialect)} dialect</span>` : ""}
      <span>${escapeHtml(result.execution_status)}</span>
      ${result.reason_code ? `<span>${escapeHtml(result.reason_code)}</span>` : ""}
    </div>
    ${message}
  </article>`;
}

function errorMessage(payload, fallback) {
  if (Array.isArray(payload?.detail)) {
    return payload.detail.map((item) => `${item.loc?.at(-1) || "request"}: ${item.msg}`).join(" · ");
  }
  if (typeof payload?.detail === "string") return payload.detail;
  if (payload?.detail?.code) return payload.detail.code.replaceAll("_", " ");
  return fallback;
}

function optionsForBackend(id) {
  if (id === "verieql") {
    return {
      bound_size: Number($("#verieql-bound").value),
    };
  }
  if (id === "parseval") {
    const connectionString = $("#parseval-connection")?.value.trim();
    return connectionString ? { connection_string: connectionString } : {};
  }
  return {};
}

async function runBackend(id, baseCase, timeoutMs) {
  const descriptor = state.checkers.find((item) => item.checker.checker_id === id);
  const dialect = baseCase.dialect;
  const payload = {
    case: {
      ...baseCase,
      dialect,
    },
    checker_ids: [id],
    options: {
      timeout_ms: timeoutMs,
      seed: 1,
      backend_options: { [id]: optionsForBackend(id) },
    },
  };

  try {
    const response = await fetch("/v1/checks", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(errorMessage(data, "The check request failed."));
    return { ...data.results[0], _dialect: dialect };
  } catch (error) {
    return {
      checker: descriptor?.checker || { checker_id: id, backend_version: "unknown", adapter_version: "unknown" },
      execution_status: "error",
      verdict: null,
      duration_ms: 0,
      reason_code: "request_failed",
      message: error.message,
      _dialect: dialect,
    };
  }
}

async function runChecks(event) {
  event.preventDefault();
  const button = $("#run-button");
  const results = $("#results");
  const count = $("#result-count");
  const checkerIds = selectedCheckers();

  if (!checkerIds.length) {
    results.innerHTML = `<div class="error-box">Select at least one available backend.</div>`;
    return;
  }

  const baseCase = {
    schema_ddl: $("#schema-ddl").value,
    query_left: $("#query-left").value,
    query_right: $("#query-right").value,
    dialect: $("#dialect").value,
    semantics: $("#semantics").value,
  };
  const timeoutMs = Number($("#timeout").value) * 1000;

  button.disabled = true;
  button.textContent = "Checking…";
  count.textContent = `${checkerIds.length} running`;
  results.innerHTML = `<div class="loading-line"></div><div class="empty-state">
    <div><div class="empty-icon">···</div><strong>Consulting the backends</strong><p>Each checker runs independently. Some approaches may take longer than others.</p></div>
  </div>`;

  try {
    const backendResults = await Promise.all(
      checkerIds.map((id) => runBackend(id, baseCase, timeoutMs)),
    );
    count.textContent = `${backendResults.length} verdict${backendResults.length === 1 ? "" : "s"}`;
    results.innerHTML = `<div class="result-list">${backendResults.map(resultCard).join("")}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = "Run all checks →";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  loadExample();
  loadCheckers();
  $("#load-example").addEventListener("click", loadExample);
  $("#check-form").addEventListener("submit", runChecks);
});
