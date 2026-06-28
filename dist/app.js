const output = document.querySelector("#output");
const accountsList = document.querySelector("#accountsList");
const sourcesList = document.querySelector("#sourcesList");
const itemsList = document.querySelector("#itemsList");
const monitorStatus = document.querySelector("#monitorStatus");
const accountForm = document.querySelector("#accountForm");
const sourceForm = document.querySelector("#sourceForm");
const runForm = document.querySelector("#runForm");
const settingsForm = document.querySelector("#settingsForm");
const settingsStatus = document.querySelector("#settingsStatus");
const llmModelOptions = document.querySelector("#llmModelOptions");
const fetchLlmModelsButton = document.querySelector("#fetchLlmModels");
const llmModelSelect = document.querySelector("#llmModelSelect");
const llmModelsStatus = document.querySelector("#llmModelsStatus");
const llmApiKeyHint = document.querySelector("#llmApiKeyHint");
const dashscopeApiKeyHint = document.querySelector("#dashscopeApiKeyHint");

function show(value) {
  output.textContent =
    typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function setLlmModels(models, selectedModel = "") {
  const uniqueModels = Array.from(new Set(models.filter(Boolean)));
  llmModelOptions.innerHTML = "";
  llmModelSelect.innerHTML = "";

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = uniqueModels.length
    ? "选择一个模型"
    : "填好 URL 和 Key 后获取模型";
  llmModelSelect.appendChild(placeholder);

  for (const model of uniqueModels) {
    const dataOption = document.createElement("option");
    dataOption.value = model;
    llmModelOptions.appendChild(dataOption);

    const selectOption = document.createElement("option");
    selectOption.value = model;
    selectOption.textContent = model;
    llmModelSelect.appendChild(selectOption);
  }

  llmModelSelect.value = uniqueModels.includes(selectedModel) ? selectedModel : "";
}

function setFieldHint(element, configured, maskedValue) {
  if (!element) return;
  element.textContent = configured
    ? `当前已保存：${maskedValue || "已配置"}`
    : "当前未保存";
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || data.error || response.statusText);
  }
  return data;
}

async function loadAccounts() {
  const accounts = await requestJson("/api/accounts");
  accountsList.innerHTML = "";
  if (!accounts.length) {
    accountsList.innerHTML = '<div class="muted">还没有账号，先添加 Cookie。</div>';
    return;
  }
  for (const account of accounts) {
    const item = document.createElement("div");
    item.className = "account-item";
    item.innerHTML = `
      <strong>${escapeHtml(account.name || account.account_key)}</strong>
      <div class="muted">key: ${escapeHtml(account.account_key)}</div>
      <div class="muted">cookie: ${
        account.cookie_saved ? "已保存" : "缺失"
      } (${account.cookie_length})</div>
      <div class="muted">cookie names: ${escapeHtml(
        (account.cookie_names || []).slice(0, 8).join(", ") || "无"
      )}</div>
      <div class="mini-actions">
        <button type="button" data-delete-account="${escapeHtml(
          account.account_key
        )}">删除</button>
      </div>
    `;
    accountsList.appendChild(item);
  }
}

async function loadSources() {
  const sources = await requestJson("/api/material-sources");
  sourcesList.innerHTML = "";
  if (!sources.length) {
    sourcesList.innerHTML = '<div class="muted">还没有素材源。</div>';
    return;
  }
  for (const source of sources) {
    const item = document.createElement("div");
    item.className = "account-item";
    item.innerHTML = `
      <strong>${escapeHtml(source.name)}</strong>
      <div class="muted">${escapeHtml(source.url)}</div>
      <div class="muted">last: ${escapeHtml(source.last_checked_at || "未采集")}</div>
      ${
        source.last_error
          ? `<div class="muted">error: ${escapeHtml(source.last_error)}</div>`
          : ""
      }
      <div class="mini-actions">
        <button type="button" data-check-source="${source.id}">采集这个源</button>
        <button type="button" data-delete-source="${source.id}">删除</button>
      </div>
    `;
    sourcesList.appendChild(item);
  }
}

async function loadItems() {
  const items = await requestJson("/api/material-items?status=new&limit=30");
  itemsList.innerHTML = "";
  if (!items.length) {
    itemsList.innerHTML = '<div class="muted">暂无待使用素材。</div>';
    return;
  }
  for (const material of items) {
    const item = document.createElement("div");
    item.className = "material-item";
    const preview =
      material.content.length > 180
        ? `${material.content.slice(0, 180)}...`
        : material.content;
    item.innerHTML = `
      <strong>${escapeHtml(
        material.title || material.source_name || `素材 #${material.id}`
      )}</strong>
      <div class="muted">${escapeHtml(material.author || "")} ${escapeHtml(
      material.url || ""
    )}</div>
      <p>${escapeHtml(preview)}</p>
      <div class="mini-actions">
        <button type="button" data-run-material="${material.id}">运行</button>
      </div>
    `;
    itemsList.appendChild(item);
  }
}

async function loadMonitorStatus() {
  const status = await requestJson("/api/material-monitor");
  monitorStatus.textContent = JSON.stringify(
    {
      running: status.running,
      interval: `${status.poll_interval_seconds}s`,
      ttl: `${status.ttl_seconds}s`,
      auto_consume: status.auto_consume_materials,
      consume_batch_size: status.consume_batch_size,
      last_started_at: status.last_started_at,
      last_finished_at: status.last_finished_at,
      expired_count: status.expired_count,
      last_error: status.last_error,
      last_results: status.last_results,
      last_tag_results: status.last_tag_results,
      last_consume_results: status.last_consume_results,
    },
    null,
    2
  );
}

async function loadSettings() {
  const settings = await requestJson("/api/settings");
  settingsForm.elements.llm_api_key.value = settings.llm_api_key_masked || "";
  setFieldHint(
    llmApiKeyHint,
    settings.llm_api_key_configured,
    settings.llm_api_key_masked
  );
  settingsForm.elements.llm_base_url.value = settings.llm_base_url || "";
  settingsForm.elements.llm_model.value = settings.llm_model || "";
  settingsForm.elements.dashscope_api_key.value =
    settings.dashscope_api_key_masked || "";
  setFieldHint(
    dashscopeApiKeyHint,
    settings.dashscope_api_key_configured,
    settings.dashscope_api_key_masked
  );
  settingsForm.elements.dashscope_embedding_model.value =
    settings.dashscope_embedding_model || "";
  settingsForm.elements.auto_publish.checked = Boolean(settings.auto_publish);
  settingsForm.elements.auto_consume_materials.checked = Boolean(
    settings.auto_consume_materials
  );
  settingsForm.elements.material_poll_interval_seconds.value =
    settings.material_poll_interval_seconds || 300;
  settingsForm.elements.material_ttl_seconds.value =
    settings.material_ttl_seconds || 7200;
  settingsForm.elements.material_consume_batch_size.value =
    settings.material_consume_batch_size || 1;

  setLlmModels(settings.llm_model_options || [], settings.llm_model || "");
  if (llmModelsStatus) {
    llmModelsStatus.textContent = settings.llm_model
      ? `当前模型：${settings.llm_model}`
      : "填好 URL 和 Key 后点击获取模型";
  }

  settingsStatus.textContent = JSON.stringify(
    {
      llm_api_key: settings.llm_api_key_masked || "缺失",
      dashscope_api_key: settings.dashscope_api_key_masked || "缺失",
      llm_base_url: settings.llm_base_url || "缺失",
      llm_model: settings.llm_model || "缺失",
    },
    null,
    2
  );
}

accountForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(accountForm);
  const payload = {
    account_key: form.get("key").trim(),
    name: form.get("name").trim(),
    cookie: form.get("cookie").trim(),
  };
  show("保存中...");
  await requestJson("/api/accounts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  accountForm.reset();
  await loadAccounts();
  show("账号已保存。");
});

sourceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(sourceForm);
  const payload = {
    name: form.get("name").trim(),
    url: form.get("url").trim(),
    source_type: "binance_square",
    enabled: true,
  };
  show("保存素材源...");
  await requestJson("/api/material-sources", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  sourceForm.reset();
  await loadSources();
  show("素材源已保存。");
});

settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await saveSettingsForm();
});

async function saveSettingsForm() {
  const form = new FormData(settingsForm);
  const llmApiKey = form.get("llm_api_key").trim();
  const dashscopeApiKey = form.get("dashscope_api_key").trim();
  const unchangedSecret = (value) => value.includes("*") || value.includes("•");
  const payload = {
    llm_api_key: llmApiKey && !unchangedSecret(llmApiKey) ? llmApiKey : null,
    llm_base_url: form.get("llm_base_url").trim(),
    llm_model: form.get("llm_model").trim(),
    dashscope_api_key:
      dashscopeApiKey && !unchangedSecret(dashscopeApiKey)
        ? dashscopeApiKey
        : null,
    dashscope_embedding_model: form.get("dashscope_embedding_model").trim(),
    auto_publish: form.get("auto_publish") === "on",
    auto_consume_materials: form.get("auto_consume_materials") === "on",
    material_poll_interval_seconds: Number(
      form.get("material_poll_interval_seconds") || 300
    ),
    material_ttl_seconds: Number(form.get("material_ttl_seconds") || 7200),
    material_consume_batch_size: Number(
      form.get("material_consume_batch_size") || 1
    ),
  };
  show("保存配置...");
  const result = await requestJson("/api/settings", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  await loadSettings();
  await loadMonitorStatus();
  show(result);
}

runForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(runForm);
  const payload = {
    title: form.get("title").trim() || null,
    url: form.get("url").trim() || null,
    content: form.get("content").trim(),
    auto_publish: form.get("publish") === "on",
  };
  show("运行中...");
  const data = await requestJson("/api/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  show(data);
});

document.querySelector("#refreshAccounts").addEventListener("click", loadAccounts);
document.querySelector("#refreshSettings").addEventListener("click", loadSettings);
document.querySelector("#testLlm").addEventListener("click", async () => {
  await saveSettingsForm();
  show("测试 LLM 连接...");
  const data = await requestJson("/api/settings/test-llm", { method: "POST" });
  show(data);
  await loadSettings();
});
document.querySelector("#testEmbedding").addEventListener("click", async () => {
  await saveSettingsForm();
  show("测试 Embedding 连接...");
  const data = await requestJson("/api/settings/test-embedding", {
    method: "POST",
  });
  show(data);
  await loadSettings();
});
fetchLlmModelsButton.addEventListener("click", async () => {
  try {
    fetchLlmModelsButton.disabled = true;
    if (llmModelsStatus) {
      llmModelsStatus.textContent = "正在保存配置并获取模型列表...";
    }
    show("正在保存配置并获取模型列表...");
    await saveSettingsForm();
    const data = await requestJson("/api/settings/models", { method: "POST" });
    if (!data.ok) {
      throw new Error(data.message || "获取模型失败");
    }
    const currentModel = settingsForm.elements.llm_model.value.trim();
    const models = data.models || [];
    const selectedModel = models.includes(currentModel) ? currentModel : models[0] || "";
    if (selectedModel && selectedModel !== currentModel) {
      settingsForm.elements.llm_model.value = selectedModel;
      await saveSettingsForm();
    }
    setLlmModels(models, selectedModel);
    if (llmModelsStatus) {
      llmModelsStatus.textContent = selectedModel
        ? `已获取 ${models.length} 个模型，当前模型：${selectedModel}`
        : `已获取 ${models.length} 个模型`;
    }
    show({
      ok: true,
      selected_model: selectedModel || null,
      count: models.length,
      models,
    });
  } catch (error) {
    if (llmModelsStatus) {
      llmModelsStatus.textContent = `获取失败：${error.message}`;
    }
    show(error.message);
  } finally {
    fetchLlmModelsButton.disabled = false;
  }
});
llmModelSelect.addEventListener("change", () => {
  if (llmModelSelect.value) {
    settingsForm.elements.llm_model.value = llmModelSelect.value;
  }
});
document.querySelector("#refreshItems").addEventListener("click", loadItems);
document.querySelector("#checkSources").addEventListener("click", async () => {
  show("采集中...");
  const data = await requestJson("/api/material-sources/check", { method: "POST" });
  await loadSources();
  await loadItems();
  await loadMonitorStatus();
  show(data);
});
document.querySelector("#checkTools").addEventListener("click", async () => {
  show("检查中...");
  show(await requestJson("/api/mcp/tools"));
});

accountsList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-delete-account]");
  if (!button) return;
  const key = button.dataset.deleteAccount;
  if (!confirm(`删除账号 ${key}？`)) return;
  show("删除账号...");
  await requestJson(`/api/accounts/${encodeURIComponent(key)}`, { method: "DELETE" });
  await loadAccounts();
  show("账号已删除。");
});

sourcesList.addEventListener("click", async (event) => {
  const deleteButton = event.target.closest("[data-delete-source]");
  if (deleteButton) {
    if (!confirm("删除这个素材源？")) return;
    show("删除素材源...");
    await requestJson(`/api/material-sources/${deleteButton.dataset.deleteSource}`, {
      method: "DELETE",
    });
    await loadSources();
    show("素材源已删除。");
    return;
  }

  const button = event.target.closest("[data-check-source]");
  if (!button) return;
  show("采集中...");
  const data = await requestJson(`/api/material-sources/${button.dataset.checkSource}/check`, {
    method: "POST",
  });
  await loadSources();
  await loadItems();
  await loadMonitorStatus();
  show(data);
});

itemsList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-run-material]");
  if (!button) return;
  show("运行素材...");
  const data = await requestJson("/api/material-items/run", {
    method: "POST",
    body: JSON.stringify({
      material_item_id: Number(button.dataset.runMaterial),
      auto_publish: runForm.elements.publish.checked,
    }),
  });
  await loadItems();
  show(data);
});

setInterval(() => {
  loadMonitorStatus().catch(() => {});
  loadItems().catch(() => {});
}, 30000);

Promise.all([
  loadAccounts(),
  loadSources(),
  loadItems(),
  loadMonitorStatus(),
  loadSettings(),
]).catch((error) => show(error.message));
