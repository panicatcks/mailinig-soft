"use strict";

// id поля в DOM -> ключ в конфиге
const FIELDS = {
  toFile: "to_file",
  emailCol: "email_col",
  template: "template",
  subject: "subject",
  smtp_host: "smtp_host",
  smtp_port: "smtp_port",
  smtp_user: "smtp_user",
  smtp_password: "smtp_password",
  from_email: "from_email",
  limit_min: "limit_min",
  limit_day: "limit_day",
  start_row: "start_row",
  allow_duplicate_emails: "allow_duplicate_emails",
  use_kind_template: "use_kind_template",
  kind_col: "kind_col",
  kind_filter: "kind_filter",
  hub_url: "hub_url",
  hub_connection_id: "hub_connection_id",
  hub_secret: "hub_secret",
  hub_insecure_ssl: "hub_insecure_ssl",
  cloud_enabled: "cloud_enabled",
  server_host: "server_host",
  server_port: "server_port",
  server_user: "server_user",
  server_password: "server_password",
  server_remote_dir: "server_remote_dir",
  test_email: "test_email",
};
const CHECKBOXES = new Set([
  "allow_duplicate_emails", "use_kind_template", "hub_insecure_ssl", "cloud_enabled",
]);

let pollTimer = null;
let activeStateFile = ".send_email_state.json";
const $ = (id) => document.getElementById(id);

async function api(path, body) {
  const opts = { method: body ? "POST" : "GET" };
  if (body) { opts.headers = { "Content-Type": "application/json" }; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  return res.json();
}

function toast(msg, kind = "") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast show " + kind;
  setTimeout(() => { el.className = "toast " + kind; }, 3200);
}

function collectSettings() {
  const s = {};
  for (const [id, key] of Object.entries(FIELDS)) {
    const el = $(id);
    if (!el) continue;
    s[key] = CHECKBOXES.has(id) ? el.checked : el.value.trim();
  }
  s.sheet = "ALL";              // веб-режим всегда объединяет вкладки
  s.state_file = activeStateFile;
  return s;
}

function applyConfig(cfg) {
  for (const [id, key] of Object.entries(FIELDS)) {
    const el = $(id);
    if (!el) continue;
    if (CHECKBOXES.has(id)) el.checked = !!cfg[key];
    else el.value = cfg[key] != null ? cfg[key] : "";
  }
  $("passNote").textContent = cfg.has_smtp_password ? "(сохранён, оставь пустым)" : "";
  $("hubNote").textContent = cfg.has_hub_secret ? "(сохранён)" : "";
  $("srvNote").textContent = cfg.has_server_password ? "(сохранён)" : "";
  activeStateFile = (cfg.state_file && cfg.state_file.trim()) || ".send_email_state.json";
  refreshCloudPill();
}

function refreshCloudPill() {
  const on = $("cloud_enabled").checked;
  const pill = $("cloudPill");
  pill.textContent = on ? "Облако: вкл" : "Облако: выкл";
  pill.className = "pill " + (on ? "pill-on" : "pill-muted");
}

let saveTimer = null;
function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => api("/api/config", { settings: collectSettings() }), 600);
}

// --- действия ---
async function browse(kind) {
  const r = await api("/api/browse", { kind });
  if (!r.ok || !r.path) return;
  if (kind === "excel") {
    $("toFile").value = r.path;
    scheduleSave();
    smartAnalyze(true);
  } else {
    $("template").value = r.path;
    scheduleSave();
  }
}

// Умный разбор: сам находит колонку email, строку начала данных и считает получателей.
async function smartAnalyze(silent) {
  const file = $("toFile").value.trim();
  if (!file) { if (!silent) toast("Сначала выбери базу", "err"); return; }
  $("detectHint").textContent = "🔍 разбираю файл…";
  $("detectHint").className = "hint detect-hint";
  const r = await api("/api/analyze", { to_file: file });
  if (r.ok) {
    if (r.email_col) $("emailCol").value = r.email_col;
    if (r.start_row) $("start_row").value = r.start_row;
    $("detectHint").textContent = "✓ " + r.message;
    $("detectHint").className = "hint detect-hint ok";
    scheduleSave();
  } else {
    $("detectHint").textContent = "✗ " + (r.message || "не получилось разобрать файл");
    $("detectHint").className = "hint detect-hint err";
  }
}

// --- сессии (профили) ---
async function loadProfiles() {
  const r = await api("/api/profiles");
  const sel = $("profileSelect");
  const current = r.active || "";
  sel.innerHTML = '<option value="">— без сессии —</option>' +
    (r.names || []).map((n) => `<option value="${n.replace(/"/g, "&quot;")}">${n}</option>`).join("");
  sel.value = current;
}

async function profileLoad() {
  const name = $("profileSelect").value;
  if (!name) return toast("Выбери сессию из списка", "err");
  const r = await api("/api/profiles/load", { name });
  if (r.ok) { applyConfig(r.settings); await loadProfiles(); toast("Сессия загружена: " + name, "ok"); }
  else toast(r.message || "Не удалось загрузить", "err");
}

async function profileSaveTo(name) {
  const r = await api("/api/profiles/save", { name, settings: collectSettings() });
  if (r.ok) { await loadProfiles(); $("profileSelect").value = r.active || name; toast("Сессия сохранена: " + name, "ok"); applyConfig(await api("/api/config")); }
  else toast(r.message || "Не удалось сохранить", "err");
}

function profileSave() {
  const name = $("profileSelect").value;
  if (!name) return profileNew();
  profileSaveTo(name);
}

function profileNew() {
  const name = (window.prompt("Название новой сессии:") || "").trim();
  if (!name) return;
  profileSaveTo(name);
}

async function profileDelete() {
  const name = $("profileSelect").value;
  if (!name) return toast("Выбери сессию", "err");
  if (!window.confirm(`Удалить сессию «${name}»?`)) return;
  const r = await api("/api/profiles/delete", { name });
  if (r.ok) { await loadProfiles(); toast("Сессия удалена", ""); }
}

async function check() {
  setReadout("Проверяю базу и шаблон…", "");
  const r = await api("/api/count", { settings: collectSettings() });
  if (r.ok) {
    setReadout(`✓ Готово к отправке. Получателей: ${r.total != null ? r.total : "?"}`, "ok");
  } else {
    setReadout("✗ " + (r.message || "Ошибка проверки"), "err");
  }
}

function setReadout(text, kind) {
  const el = $("readout");
  el.textContent = text;
  el.className = "readout " + kind;
}

async function start(dryRun, overrideTo) {
  const settings = collectSettings();
  if (!settings.to_file && !overrideTo) return toast("Шаг 1: выбери базу", "err");
  if (!settings.email_col && !overrideTo) return toast("Шаг 2: укажи колонку email", "err");
  if (!settings.template) return toast("Шаг 3: выбери HTML-письмо", "err");

  const body = { settings, dry_run: dryRun };
  if (overrideTo) body.override_to = overrideTo;
  const r = await api("/api/start", body);
  if (!r.ok) { toast(r.message || "Не удалось запустить", "err"); return; }
  toast(r.execution === "cloud" ? "Запущено в облаке" : "Запущено локально", "ok");
  setSending(true);
  startPolling();
}

async function stop() {
  const r = await api("/api/stop", {});
  toast(r.message || "Остановлено");
}

function setSending(on) {
  $("sendBtn").disabled = on;
  $("checkBtn").disabled = on;
  $("testBtn").disabled = on;
  $("stopBtn").disabled = !on;
}

// --- прогресс ---
function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(refreshProgress, 1000);
  refreshProgress();
}

async function refreshProgress() {
  const p = await api("/api/progress");
  const prog = p.progress || {};
  const total = prog.total || 0;
  const sent = prog.sent || 0;
  const failed = prog.failed || 0;
  const skipped = prog.skipped || 0;
  const processed = prog.processed != null ? prog.processed : (sent + failed + skipped);
  const percent = prog.percent != null ? prog.percent : (total ? Math.round(processed / total * 100) : 0);

  $("statSent").textContent = sent;
  $("statTotal").textContent = total;
  $("statFailed").textContent = failed;
  $("statSkipped").textContent = skipped;
  $("barFill").style.width = Math.min(percent, 100) + "%";
  $("percentText").textContent = Math.min(percent, 100) + "%";

  const status = $("statusLine");
  if (p.running) {
    status.textContent = (p.execution === "cloud" ? "Облако · " : "") +
      (p.dry_run ? "Проверка…" : "Отправка идёт…") + (p.started_at ? " (с " + p.started_at + ")" : "");
    status.className = "status-line run";
  } else if (p.finished) {
    const msg = prog.message || (prog.status === "completed" ? "Готово" : "Завершено");
    const failedRun = prog.status && prog.status !== "completed";
    status.textContent = (p.dry_run ? "Проверка завершена. " : "") + msg;
    status.className = "status-line " + (failedRun ? "fail" : "done");
  }
  if (p.log != null) {
    const log = $("log");
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    log.textContent = p.log;
    if (atBottom) log.scrollTop = log.scrollHeight;
  }

  if (!p.running && (p.finished || !p.progress)) {
    if (pollTimer && p.finished) { clearInterval(pollTimer); pollTimer = null; setSending(false); }
  }
}

async function resetDaily() {
  if (!window.confirm("Обнулить счётчик отправленных за сегодня?")) return;
  const r = await api("/api/reset-daily", { state_file: activeStateFile });
  toast(r.message || "Готово", r.ok ? "ok" : "err");
}

async function resetProgress() {
  if (!window.confirm("Сбросить прогресс? Рассылка начнётся с первой строки базы.")) return;
  const r = await api("/api/reset-progress", { state_file: activeStateFile });
  toast(r.message || "Готово", r.ok ? "ok" : "err");
}

// --- валидатор базы ---
let validateTimer = null;
async function validateStart() {
  const file = $("toFile").value.trim();
  if (!file) return toast("Сначала выбери базу (шаг 1)", "err");
  const col = $("emailCol").value.trim() || "A";
  const startRow = parseInt($("start_row").value.trim() || "2", 10);
  const r = await api("/api/validate-start", { to_file: file, email_col: col, start_row: startRow });
  if (!r.ok) return toast(r.message || "Не удалось запустить проверку", "err");
  $("validateStartBtn").disabled = true;
  $("validateStopBtn").disabled = false;
  $("validateStatus").textContent = "Запускаю…";
  if (validateTimer) clearInterval(validateTimer);
  validateTimer = setInterval(refreshValidate, 800);
  refreshValidate();
}

async function validateStop() {
  await api("/api/validate-stop", {});
}

async function refreshValidate() {
  const s = await api("/api/validate-progress");
  const total = s.total || 0;
  const done = s.done || 0;
  const pct = total ? Math.round(done / total * 100) : 0;
  $("validateBar").style.width = Math.min(pct, 100) + "%";
  const okN = s.ok_count || 0, badN = s.bad_count || 0;
  let line = s.message || "";
  if (s.running) line = `${done}/${total} · валидных ${okN}, плохих ${badN}`;
  $("validateStatus").textContent = line;
  $("validateStatus").className = "hint" + (s.finished && !s.running ? " ok" : "");
  if (s.finished && !s.running) {
    $("validateStartBtn").disabled = false;
    $("validateStopBtn").disabled = true;
    if (validateTimer) { clearInterval(validateTimer); validateTimer = null; }
    if (s.cleaned_path) toast("Очищенный файл: " + s.cleaned_path.split("/").pop(), "ok");
  }
}

async function hubCheck() {
  $("hubStatus").textContent = "проверяю…";
  const r = await api("/api/hub-check", { settings: collectSettings() });
  $("hubStatus").textContent = (r.ok ? "✓ " : "✗ ") + r.message;
}

async function cloudCheck() {
  $("cloudStatus").textContent = "подключаюсь…";
  const r = await api("/api/cloud-check", { settings: collectSettings() });
  $("cloudStatus").textContent = (r.ok ? "✓ " : "✗ ") + r.message;
}

// --- инициализация ---
async function init() {
  applyConfig(await api("/api/config"));

  document.querySelectorAll("[data-browse]").forEach((b) =>
    b.addEventListener("click", () => browse(b.dataset.browse)));
  $("detectBtn").addEventListener("click", () => smartAnalyze(false));
  $("profileLoad").addEventListener("click", profileLoad);
  $("profileSave").addEventListener("click", profileSave);
  $("profileNew").addEventListener("click", profileNew);
  $("profileDelete").addEventListener("click", profileDelete);
  $("checkBtn").addEventListener("click", check);
  $("sendBtn").addEventListener("click", () => start(false, null));
  $("stopBtn").addEventListener("click", stop);
  $("testBtn").addEventListener("click", () => {
    const email = $("test_email").value.trim();
    if (!email) return toast("Укажи email для теста", "err");
    start(false, [email]);
  });
  $("resetDailyBtn").addEventListener("click", resetDaily);
  $("resetProgressBtn").addEventListener("click", resetProgress);
  $("validateStartBtn").addEventListener("click", validateStart);
  $("validateStopBtn").addEventListener("click", validateStop);
  $("hubCheckBtn").addEventListener("click", hubCheck);
  $("cloudCheckBtn").addEventListener("click", cloudCheck);
  $("cloud_enabled").addEventListener("change", refreshCloudPill);
  $("settingsBtn").addEventListener("click", () => {
    document.querySelector(".advanced").open = true;
    document.querySelector(".advanced").scrollIntoView({ behavior: "smooth" });
  });

  // автосохранение при изменениях
  for (const id of Object.keys(FIELDS)) {
    const el = $(id);
    if (el) el.addEventListener("change", scheduleSave);
  }

  await loadProfiles();

  // если на сервере уже что-то выполняется — подхватим
  refreshProgress();
}

init();
