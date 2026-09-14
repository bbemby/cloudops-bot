/* CloudOps 管理面板前端。
 *
 * 约束：不引框架、不引 CDN（容器内离线可用），且不能有内联脚本/样式
 * （服务端下发的 CSP 是 script-src 'self'）。所有文案来自 /api/bootstrap，
 * 动态数据一律用 textContent 落 DOM，避免把云端返回的内容当 HTML 解析。
 */
"use strict";

const $ = (id) => document.getElementById(id);
const CSRF = document.querySelector('meta[name="csrf-token"]').content;

const state = {
  labels: {},
  providers: [],
  readonly: false,
  language: "zh",
  view: "overview",
};

/* ------------------------------------------------------------------ */
/* 文案与格式化                                                        */
/* ------------------------------------------------------------------ */
function t(key, params) {
  let text = state.labels[key] || key;
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.split("{" + name + "}").join(String(value));
    }
  }
  return text;
}

function fmtTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function fmtUptime(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  return t("web.uptime.format", {
    d: Math.floor(total / 86400),
    h: Math.floor((total % 86400) / 3600),
    m: Math.floor((total % 3600) / 60),
  });
}

/* ------------------------------------------------------------------ */
/* DOM 工具                                                            */
/* ------------------------------------------------------------------ */
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function cell(text, className) {
  return el("td", className, text === null || text === undefined || text === "" ? "-" : text);
}

function emptyRow(colspan, message) {
  const row = el("tr");
  const td = el("td", "empty", message);
  td.colSpan = colspan;
  row.appendChild(td);
  return row;
}

function statusCell(status) {
  const td = el("td");
  const dot = el("span", "dot " + String(status || "").toLowerCase());
  td.appendChild(dot);
  td.appendChild(document.createTextNode(String(status || "unknown")));
  return td;
}

function toast(message, kind) {
  const host = $("toasts");
  const node = el("div", "toast " + (kind || ""), message);
  host.appendChild(node);
  setTimeout(() => node.remove(), kind === "error" ? 8000 : 4000);
}

/* ------------------------------------------------------------------ */
/* 请求                                                                */
/* ------------------------------------------------------------------ */
async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  if (opts.body !== undefined && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  if (opts.method && opts.method !== "GET") {
    opts.headers["X-CSRF-Token"] = CSRF;
  }
  const response = await fetch(path, opts);
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const detail = (payload && payload.error) || response.statusText;
    throw new Error(detail);
  }
  return payload;
}

/* ------------------------------------------------------------------ */
/* 确认对话框                                                          */
/* ------------------------------------------------------------------ */
function askConfirm({ title, text, expect }) {
  return new Promise((resolve) => {
    const dialog = $("confirm-dialog");
    const input = $("confirm-input");
    const ok = $("confirm-ok");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    input.value = "";
    input.hidden = !expect;
    ok.disabled = Boolean(expect);
    ok.textContent = t("web.confirm");
    $("confirm-cancel").textContent = t("web.cancel");

    const check = () => { ok.disabled = Boolean(expect) && input.value.trim() !== expect; };
    input.oninput = check;
    const finish = (value) => {
      const typed = input.value.trim();
      ok.onclick = null;
      input.oninput = null;
      $("confirm-cancel").onclick = null;
      $("confirm-dialog").close();
      resolve({ ok: value, typed: typed });
    };
    ok.onclick = () => finish(true);
    $("confirm-cancel").onclick = () => finish(false);
    dialog.showModal();
    if (expect) input.focus();
  });
}

/* ------------------------------------------------------------------ */
/* 视图：概览                                                          */
/* ------------------------------------------------------------------ */
function card(label, value, small) {
  const box = el("div", "card");
  box.appendChild(el("div", "k", label));
  box.appendChild(el("div", "v" + (small ? " small" : ""), value));
  return box;
}

async function loadOverview() {
  const data = await api("/api/overview");
  $("overview-hint").textContent = t("web.overview.hint");
  const cards = $("overview-cards");
  cards.replaceChildren(
    card(t("web.card.users"), data.users),
    card(t("web.card.credentials"), data.credentials),
    card(t("web.card.operations"), data.operations),
    card(t("web.card.tasks"), data.tasks),
    card(t("web.card.jobs"), data.active_jobs),
    card(t("web.card.instances"), data.instances_tracked),
    card(t("web.card.uptime"), fmtUptime(data.uptime_seconds), true),
    card(t("web.card.version"), "v" + data.version, true),
  );

  const body = $("overview-providers");
  body.replaceChildren();
  if (!data.providers.length) {
    body.appendChild(emptyRow(3, t("web.credentials.empty")));
    return;
  }
  const bound = new Set(data.bound_providers || []);
  for (const provider of data.providers) {
    const row = el("tr");
    row.appendChild(cell(provider.label + " (" + provider.name + ")"));
    row.appendChild(cell(String(data.provider_counts[provider.name] || 0)));
    const active = el("td");
    const isBound = bound.has(provider.name);
    const badge = el("span", "badge " + (isBound ? "ok" : ""),
      isBound ? t("web.bound.yes") : t("web.bound.no"));
    active.appendChild(badge);
    row.appendChild(active);
    body.appendChild(row);
  }
}

/* ------------------------------------------------------------------ */
/* 视图：实例                                                          */
/* ------------------------------------------------------------------ */
function showErrors(container, errors, label) {
  container.replaceChildren();
  if (!errors || !errors.length) return;
  const box = el("div", "notice error");
  box.appendChild(el("div", null, label));
  for (const item of errors) {
    box.appendChild(el("div", "mono", `${item.provider}: ${item.error}`));
  }
  container.appendChild(box);
}

async function loadInstances() {
  $("instances-hint").textContent = t("web.instances.hint");
  const provider = $("instance-provider").value;
  const query = provider ? "?provider=" + encodeURIComponent(provider) : "";
  const data = await api("/api/instances" + query);
  showErrors($("instances-errors"), data.errors, t("web.instances.errors"));

  const chips = $("instances-chips");
  chips.replaceChildren();
  for (const [status, count] of Object.entries(data.by_status)) {
    chips.appendChild(el("span", "chip", `${status}: ${count}`));
  }

  const body = $("instances-body");
  body.replaceChildren();
  if (!data.instances.length) {
    const message = data.providers.length ? t("web.instances.empty") : t("web.instances.no_credential");
    body.appendChild(emptyRow(7, message));
    return;
  }
  for (const item of data.instances) {
    const row = el("tr");
    row.appendChild(cell(item.provider));
    row.appendChild(cell(item.name + " (" + item.id + ")"));
    row.appendChild(statusCell(item.status));
    row.appendChild(cell(item.address, "mono"));
    row.appendChild(cell(item.region));
    row.appendChild(cell(item.size));
    const actions = el("td");
    // 只有适配器声明了 destroy 才画按钮，否则这一个格子留空
    if (!item.actions || item.actions.includes("destroy")) {
      const destroy = el("button", "small danger", t("web.instances.destroy"));
      destroy.disabled = state.readonly;
      destroy.onclick = () => destroyInstance(item, destroy);
      actions.appendChild(destroy);
    }
    row.appendChild(actions);
    body.appendChild(row);
  }
}

async function destroyInstance(item, button) {
  const accepted = [String(item.id), String(item.name)];
  const answer = await askConfirm({
    title: t("web.instances.destroy"),
    text: t("web.instances.destroy.prompt", { name: item.name || item.id, provider: item.provider }),
    expect: item.name || item.id,
  });
  if (!answer.ok || !accepted.includes(answer.typed)) return;
  button.disabled = true;
  try {
    await api(`/api/instances/${encodeURIComponent(item.provider)}/${encodeURIComponent(item.id)}/destroy`,
      { method: "POST", body: { confirm: answer.typed } });
    toast(t("web.instances.destroy.done", { name: item.name || item.id }), "ok");
    await loadInstances();
  } catch (error) {
    toast(t("web.action.failed", { detail: error.message }), "error");
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* 视图：凭证                                                          */
/* ------------------------------------------------------------------ */
function renderCredentialFields() {
  const providerName = $("cred-provider").value;
  const form = state.providers.find((item) => item.name === providerName);
  const host = $("cred-fields");
  host.replaceChildren();
  if (!form) return;
  for (const field of form.fields) {
    const box = el("div", "field" + (field.required ? " required" : ""));
    const label = el("label", null, field.name);
    label.htmlFor = "cred-field-" + field.name;
    box.appendChild(label);
    const input = el("input");
    input.id = "cred-field-" + field.name;
    input.type = field.secret ? "password" : "text";
    input.autocomplete = "off";
    if (field.example) input.placeholder = field.example;
    input.dataset.field = field.name;
    box.appendChild(input);
    box.appendChild(el("div", "desc", field.description));
    host.appendChild(box);
  }
  if (form.usage) {
    const usage = el("div", "field");
    usage.appendChild(el("div", "desc mono", form.usage));
    host.appendChild(usage);
  }
}

async function loadCredentials() {
  $("credentials-hint").textContent = t("web.credentials.hint");
  const data = await api("/api/credentials");
  const body = $("credentials-body");
  body.replaceChildren();
  if (!data.credentials.length) {
    body.appendChild(emptyRow(8, t("web.credentials.empty")));
    return;
  }
  for (const item of data.credentials) {
    const row = el("tr");
    row.appendChild(cell(item.provider));
    row.appendChild(cell(item.label));
    row.appendChild(cell(item.identity));
    const fields = Object.entries(item.fields || {})
      .map(([key, value]) => `${key}=${value}`).join(" · ");
    row.appendChild(cell(fields, "mono"));
    row.appendChild(cell(item.owner));
    row.appendChild(cell(item.source));
    const active = el("td");
    active.appendChild(el("span", "badge " + (item.is_active ? "ok" : ""),
      item.is_active ? "active" : "-"));
    row.appendChild(active);

    const actions = el("td");
    if (!item.is_active) {
      const activate = el("button", "small", t("web.credentials.activate"));
      activate.disabled = state.readonly;
      activate.onclick = async () => {
        try {
          await api(`/api/credentials/${item.id}/activate`, { method: "POST", body: {} });
          toast(t("web.activated"), "ok");
          state.view = "credentials";
          await loadCredentials();
        } catch (error) {
          toast(t("web.action.failed", { detail: error.message }), "error");
        }
      };
      actions.appendChild(activate);
    }
    const remove = el("button", "small danger", t("web.credentials.delete"));
    remove.disabled = state.readonly;
    remove.onclick = async () => {
      const answer = await askConfirm({
        title: t("web.credentials.delete"),
        text: t("web.credentials.delete.prompt", { label: item.label }),
      });
      if (!answer.ok) return;
      try {
        await api(`/api/credentials/${item.id}`, { method: "DELETE" });
        toast(t("web.deleted"), "ok");
        await loadCredentials();
      } catch (error) {
        toast(t("web.action.failed", { detail: error.message }), "error");
      }
    };
    actions.appendChild(remove);
    row.appendChild(actions);
    body.appendChild(row);
  }
}

async function submitCredential() {
  const button = $("cred-submit");
  const fields = {};
  for (const input of $("cred-fields").querySelectorAll("input[data-field]")) {
    if (input.value) fields[input.dataset.field] = input.value;
  }
  button.disabled = true;
  try {
    const result = await api("/api/credentials", {
      method: "POST",
      body: {
        provider: $("cred-provider").value,
        label: $("cred-label").value,
        fields: fields,
      },
    });
    toast(`${t("web.saved")} · ${result.provider}:${result.label} (${result.identity})`, "ok");
    for (const input of $("cred-fields").querySelectorAll("input[data-field]")) input.value = "";
    await loadCredentials();
  } catch (error) {
    toast(t("web.action.failed", { detail: error.message }), "error");
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* 视图：用户 / 审计 / 任务                                            */
/* ------------------------------------------------------------------ */
async function loadUsers() {
  const data = await api("/api/users");
  $("users-hint").textContent = t("web.users.hint");
  const body = $("users-body");
  body.replaceChildren();
  if (!data.users.length) {
    body.appendChild(emptyRow(4, t("web.users.empty")));
    return;
  }
  for (const user of data.users) {
    const row = el("tr");
    row.appendChild(cell(user.telegram_id, "mono"));
    row.appendChild(cell(user.username));
    const role = el("td");
    const select = el("select");
    for (const name of data.roles) {
      const option = el("option", null, name);
      option.value = name;
      if (name === user.role) option.selected = true;
      select.appendChild(option);
    }
    select.disabled = state.readonly;
    select.onchange = async () => {
      try {
        await api(`/api/users/${user.telegram_id}/role`,
          { method: "POST", body: { role: select.value } });
        toast(t("web.saved"), "ok");
      } catch (error) {
        toast(t("web.action.failed", { detail: error.message }), "error");
      }
    };
    role.appendChild(select);
    row.appendChild(role);
    row.appendChild(cell(fmtTime(user.last_seen_at || user.created_at)));
    body.appendChild(row);
  }
}

async function loadAudit() {
  const data = await api("/api/logs?limit=100");
  $("audit-hint").textContent = t("web.audit.hint");
  const body = $("audit-body");
  body.replaceChildren();
  if (!data.logs.length) {
    body.appendChild(emptyRow(6, t("web.audit.empty")));
    return;
  }
  for (const log of data.logs) {
    const row = el("tr");
    row.appendChild(cell(fmtTime(log.created_at)));
    row.appendChild(cell(log.action, "mono"));
    row.appendChild(cell(log.status));
    row.appendChild(cell(log.provider));
    row.appendChild(cell(log.target));
    row.appendChild(cell(log.detail));
    body.appendChild(row);
  }
}

async function loadTasks() {
  const data = await api("/api/tasks?limit=100");
  $("tasks-hint").textContent = t("web.tasks.hint");
  const body = $("tasks-body");
  body.replaceChildren();
  if (!data.tasks.length) {
    body.appendChild(emptyRow(6, t("web.tasks.empty")));
    return;
  }
  for (const task of data.tasks) {
    const row = el("tr");
    row.appendChild(cell(task.id, "mono"));
    row.appendChild(cell(task.kind));
    row.appendChild(statusCell(task.status));
    row.appendChild(cell(task.provider));
    row.appendChild(cell(task.detail));
    row.appendChild(cell(fmtTime(task.updated_at || task.created_at)));
    body.appendChild(row);
  }
}

/* ------------------------------------------------------------------ */
/* 视图切换                                                            */
/* ------------------------------------------------------------------ */
const LOADERS = {
  overview: loadOverview,
  instances: loadInstances,
  credentials: loadCredentials,
  users: loadUsers,
  audit: loadAudit,
  tasks: loadTasks,
};

async function show(view) {
  state.view = view;
  for (const button of document.querySelectorAll("#nav button")) {
    button.classList.toggle("active", button.dataset.view === view);
  }
  for (const pane of document.querySelectorAll(".pane")) {
    pane.classList.toggle("active", pane.id === "pane-" + view);
  }
  try {
    await LOADERS[view]();
  } catch (error) {
    if (error.message !== "unauthorized") {
      toast(t("web.action.failed", { detail: error.message }), "error");
    }
  }
}

/* ------------------------------------------------------------------ */
/* 启动                                                                */
/* ------------------------------------------------------------------ */
function applyLabels() {
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  document.title = t("web.title");
}

async function main() {
  const bootstrap = await api("/api/bootstrap");
  state.labels = bootstrap.labels || {};
  state.providers = bootstrap.providers || [];
  state.readonly = Boolean(bootstrap.readonly);
  state.language = bootstrap.language || "zh";
  applyLabels();

  $("acting-user").textContent = t("web.acting_user", { id: bootstrap.acting_user_id });
  $("readonly-badge").hidden = !state.readonly;

  const select = $("instance-provider");
  select.replaceChildren();
  const all = el("option", null, t("web.instances.all"));
  all.value = "";
  select.appendChild(all);
  const credSelect = $("cred-provider");
  credSelect.replaceChildren();
  for (const provider of state.providers) {
    const option = el("option", null, provider.label);
    option.value = provider.name;
    select.appendChild(option);
    const credOption = el("option", null, provider.label);
    credOption.value = provider.name;
    credSelect.appendChild(credOption);
  }
  select.onchange = () => show("instances");
  $("instances-refresh").onclick = () => show("instances");
  $("cred-submit").onclick = submitCredential;
  credSelect.onchange = renderCredentialFields;
  renderCredentialFields();

  for (const button of document.querySelectorAll("#nav button")) {
    button.onclick = () => show(button.dataset.view);
  }

  await show("overview");
}

main().catch((error) => {
  if (error.message !== "unauthorized") {
    document.body.replaceChildren(
      el("div", "notice error", t("web.action.failed", { detail: error.message })));
  }
});
