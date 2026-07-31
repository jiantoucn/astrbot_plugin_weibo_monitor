const bridge = window.AstrBotPluginPage;
const rowsElement = document.querySelector("#rows");
const template = document.querySelector("#row-template");
const emptyState = document.querySelector("#empty-state");
const saveButton = document.querySelector("#save");
const statusElement = document.querySelector("#status");
let invalidRows = [];
let monitoredAccounts = [];
let monitorUrls = [];
let statisticsDays = [];
let selectedStatisticsDate = "";

function setStatus(message, tone = "") {
  statusElement.textContent = message;
  statusElement.className = tone;
}

function updateRowMode(row) {
  const isAll = row.querySelector(".mode").value === "all";
  const checklist = row.querySelector(".uid-checklist");
  checklist.classList.toggle("is-disabled", isAll);
  checklist.querySelectorAll("input").forEach((input) => { input.disabled = isAll; });
  if (isAll) checklist.querySelectorAll("input").forEach((input) => { input.checked = false; });
}

function setAccountOptions(checklist, selectedUids = []) {
  const selected = new Set(selectedUids);
  const options = [...monitoredAccounts];
  selectedUids.filter((uid) => !options.some((account) => account.uid === uid)).forEach((uid) => {
    options.push({ uid, label: `${uid}（当前不在监控列表中）` });
  });
  checklist.replaceChildren(...options.map((account) => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = account.uid;
    input.checked = selected.has(account.uid);
    const text = document.createElement("span");
    text.textContent = account.label;
    label.append(input, text);
    return label;
  }));
}

function toAccountOption(raw) {
  const value = raw.trim();
  if (/^\d+$/.test(value)) return { uid: value, label: `UID ${value}` };
  const match = value.match(/weibo\.(?:com|cn)\/u\/(\d+)/);
  return match ? { uid: match[1], label: `UID ${match[1]}` } : null;
}

function refreshAccountSelectors() {
  document.querySelectorAll(".uid-checklist").forEach((checklist) => {
    const selected = [...checklist.querySelectorAll("input:checked")].map((input) => input.value);
    setAccountOptions(checklist, selected);
    updateRowMode(checklist.closest(".mapping-row"));
  });
}

function renderMonitors() {
  const list = document.querySelector("#monitor-list");
  const empty = document.querySelector("#monitor-empty");
  list.replaceChildren();
  monitorUrls.forEach((url) => {
    const item = document.createElement("span");
    item.className = "monitor-chip";
    const text = document.createElement("code");
    text.textContent = url;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", `移除 ${url}`);
    remove.addEventListener("click", () => {
      monitorUrls = monitorUrls.filter((item) => item !== url);
      monitoredAccounts = monitorUrls.map(toAccountOption).filter(Boolean);
      renderMonitors();
      refreshAccountSelectors();
    });
    item.append(text, remove);
    list.append(item);
  });
  empty.hidden = monitorUrls.length > 0;
}

function addMonitor() {
  const input = document.querySelector("#monitor-input");
  const raw = input.value.trim();
  if (!raw) return;
  if (monitorUrls.includes(raw)) {
    setStatus("该监控博主已在列表中。", "error");
    return;
  }
  monitorUrls.push(raw);
  monitoredAccounts = monitorUrls.map(toAccountOption).filter(Boolean);
  input.value = "";
  renderMonitors();
  refreshAccountSelectors();
}

function updateEmptyState() {
  emptyState.hidden = rowsElement.children.length > 0;
}

function addRow(data = {}) {
  const fragment = template.content.cloneNode(true);
  const row = fragment.querySelector(".mapping-row");
  row.querySelector(".session-id").value = data.session_id || "";
  row.querySelector(".mode").value = data.mode || "all";
  setAccountOptions(row.querySelector(".uid-checklist"), data.uids || []);
  const defaultDeliveries = row.querySelector(".mode").value === "all";
  row.querySelector(".receive-hotsearch").checked = data.receive_hotsearch ?? defaultDeliveries;
  row.querySelector(".receive-daily-summary").checked = data.receive_daily_summary ?? defaultDeliveries;
  row.querySelector(".mode").addEventListener("change", () => updateRowMode(row));
  row.querySelector(".remove").addEventListener("click", () => {
    row.remove();
    updateEmptyState();
  });
  rowsElement.append(fragment);
  updateRowMode(rowsElement.lastElementChild);
  updateEmptyState();
}

function readRows() {
  return [...rowsElement.querySelectorAll(".mapping-row")].map((row) => ({
    session_id: row.querySelector(".session-id").value.trim(),
    mode: row.querySelector(".mode").value,
    uids: [...row.querySelectorAll(".uid-checklist input:checked")].map((input) => input.value),
    receive_hotsearch: row.querySelector(".receive-hotsearch").checked,
    receive_daily_summary: row.querySelector(".receive-daily-summary").checked,
  }));
}

function renderInvalidRows(invalidRows) {
  const section = document.querySelector("#invalid-section");
  const list = document.querySelector("#invalid-list");
  section.hidden = invalidRows.length === 0;
  list.replaceChildren();
  invalidRows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "invalid-row";
    item.innerHTML = `<code></code><span></span>`;
    item.querySelector("code").textContent = row.raw || "（空白行）";
    item.querySelector("span").textContent = row.error;
    list.append(item);
  });
}

function renderPushStatistics(days) {
  statisticsDays = Array.isArray(days) ? days : [];
  if (!statisticsDays.some((day) => day.date === selectedStatisticsDate)) {
    selectedStatisticsDate = statisticsDays.length ? statisticsDays[statisticsDays.length - 1].date : "";
  }
  const selectedDay = statisticsDays.find((day) => day.date === selectedStatisticsDate) || { total: 0, hourly: [], accounts: [] };
  document.querySelector("#stats-total").textContent = `${selectedDay.date || "今日"} ${selectedDay.total || 0} 条`;

  const dayButtons = document.querySelector("#stats-days");
  dayButtons.replaceChildren(...statisticsDays.map((day) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `stats-day${day.date === selectedStatisticsDate ? " is-active" : ""}`;
    button.textContent = `${day.date.slice(5)} · ${day.total} 条`;
    button.addEventListener("click", () => { selectedStatisticsDate = day.date; renderPushStatistics(statisticsDays); });
    return button;
  }));

  const maximum = Math.max(...(selectedDay.hourly || []), 1);
  const chart = document.querySelector("#hourly-chart");
  chart.replaceChildren(...Array.from({ length: 24 }, (_, hour) => {
    const item = document.createElement("div");
    item.className = "hour-bar";
    item.title = `${String(hour).padStart(2, "0")}:00 · ${(selectedDay.hourly || [])[hour] || 0} 条`;
    const value = document.createElement("span");
    value.style.height = `${(((selectedDay.hourly || [])[hour] || 0) / maximum) * 100}%`;
    const label = document.createElement("small");
    label.textContent = hour % 3 === 0 ? String(hour).padStart(2, "0") : "";
    item.append(value, label);
    return item;
  }));

  const ranking = document.querySelector("#account-ranking");
  const accounts = (selectedDay.accounts || []).slice(0, 5);
  ranking.replaceChildren(...accounts.map((account, index) => {
    const item = document.createElement("li");
    item.innerHTML = `<span class="rank">${index + 1}</span><span class="account-name"></span><strong>${account.count} 条</strong>`;
    item.querySelector(".account-name").textContent = account.username;
    return item;
  }));
  document.querySelector("#ranking-empty").hidden = accounts.length > 0;
}

async function load() {
  try {
    await bridge.ready();
    const data = await bridge.apiGet("subscription-mappings");
    monitorUrls = data.monitor_urls || [];
    monitoredAccounts = data.monitored_accounts || monitorUrls.map(toAccountOption).filter(Boolean);
    renderMonitors();
    (data.rows || []).forEach(addRow);
    invalidRows = data.invalid_rows || [];
    renderInvalidRows(invalidRows);
    updateEmptyState();
    try {
      const statistics = await bridge.apiGet("push-statistics");
      renderPushStatistics(statistics.days);
    } catch (error) {
      renderPushStatistics([]);
    }
  } catch (error) {
    setStatus(`加载失败：${error.message}`, "error");
  }
}

document.querySelector("#add-row").addEventListener("click", () => addRow({ mode: "all" }));
document.querySelector("#add-monitor").addEventListener("click", addMonitor);
document.querySelector("#monitor-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); addMonitor(); }
});
saveButton.addEventListener("click", async () => {
  if (invalidRows.length && !window.confirm("存在需要修复的旧配置。继续保存会移除这些旧记录；确定已不再需要它们吗？")) {
    return;
  }
  saveButton.disabled = true;
  setStatus("正在保存…");
  try {
    const result = await bridge.apiPost("subscription-mappings", { rows: readRows(), monitor_urls: monitorUrls });
    setStatus(`已保存 ${result.rows.length} 个会话配置。`, "success");
    monitorUrls = result.monitor_urls || monitorUrls;
    monitoredAccounts = monitorUrls.map(toAccountOption).filter(Boolean);
    renderMonitors();
    refreshAccountSelectors();
    invalidRows = [];
    renderInvalidRows([]);
  } catch (error) {
    setStatus(`保存失败：${error.message}`, "error");
  } finally {
    saveButton.disabled = false;
  }
});

load();
