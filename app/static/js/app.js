const SYSTEMS = [
  ["ES", "Электроснабжение"],
  ["EO", "Электроосвещение"],
  ["PS", "Пожарная сигнализация"],
  ["SOUE", "СОУЭ"],
  ["PT", "Пожаротушение"],
  ["SKS", "СКС"],
  ["LVS", "ЛВС"],
  ["VOLS", "ВОЛС"],
  ["CCTV", "Видеонаблюдение"],
  ["SKUD", "СКУД"],
  ["OS", "Охранная сигнализация"],
  ["ASU", "АСУ"],
  ["ASUTP", "АСУТП"],
];

const state = {
  token: localStorage.getItem("ntd_token") || "",
  user: null,
  page: "dash",
  mode: "hybrid",
  auditId: null,
  poll: null,
};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

async function api(path, opts = {}) {
  const headers = opts.headers || {};
  if (!(opts.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    logout(false);
    throw new Error("Сессия истекла");
  }
  const ct = res.headers.get("content-type") || "";
  if (opts.blob) {
    if (!res.ok) throw new Error(await res.text());
    return res.blob();
  }
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(data.detail || data.message || JSON.stringify(data));
  return data;
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 3200);
}

function logout(call = true) {
  state.token = "";
  localStorage.removeItem("ntd_token");
  if (state.poll) clearInterval(state.poll);
  $("#view-app").hidden = true;
  $("#view-login").hidden = false;
}

function showPage(name) {
  state.page = name;
  $$(".page").forEach((p) => { p.hidden = p.dataset.page !== name; });
  $$(".sidebar nav button").forEach((b) => b.classList.toggle("active", b.dataset.nav === name));
}

async function boot() {
  SYSTEMS.forEach(([code, name]) => {
    const lab = document.createElement("label");
    lab.innerHTML = `<input type="checkbox" value="${code}" /> ${name}`;
    $("#sys-box").appendChild(lab);
  });
  $("#login-form").addEventListener("submit", onLogin);
  $("#btn-logout").onclick = () => logout();
  $$(".sidebar nav button").forEach((b) => b.onclick = () => navigate(b.dataset.nav));
  $$("[data-go]").forEach((b) => b.onclick = () => navigate(b.dataset.go));
  $$("#mode-box button").forEach((b) => b.onclick = () => {
    state.mode = b.dataset.mode;
    $$("#mode-box button").forEach((x) => x.classList.toggle("on", x === b));
    renderModels();
  });
  $("#btn-create").onclick = createAudit;
  $("#btn-start").onclick = startAudit;
  $("#pick-files").onclick = (e) => { e.preventDefault(); $("#file-input").click(); };
  $("#file-input").onchange = () => uploadFiles($("#file-input").files);
  const drop = $("#drop");
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); uploadFiles(e.dataTransfer.files); };
  $("#ask-form").onsubmit = askDialog;
  $("#btn-doc").onclick = () => downloadExport("doc");
  $("#btn-xls").onclick = () => downloadExport("xls");
  $("#btn-bov").onclick = () => downloadExport("bov");
  $("#btn-ntd-check").onclick = checkNtd;
  $("#btn-ntd-new").onclick = () => editNtd(null);
  $("#btn-user-new").onclick = () => editUser(null);
  $("#btn-save-set").onclick = saveSettings;
  $("#btn-save-pass").onclick = savePass;
  $("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").hidden = true; };

  if (state.token) {
    try {
      state.user = await api("/api/auth/me");
      enterApp();
    } catch {
      logout(false);
    }
  }
}

async function onLogin(e) {
  e.preventDefault();
  $("#login-error").hidden = true;
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("#login-user").value.trim(),
        password: $("#login-pass").value,
      }),
    });
    state.token = data.token;
    state.user = data.user;
    localStorage.setItem("ntd_token", data.token);
    enterApp();
    if (data.user.must_change_password) toast("Рекомендуется сменить пароль администратора в настройках");
  } catch (err) {
    $("#login-error").hidden = false;
    $("#login-error").textContent = err.message;
  }
}

function enterApp() {
  $("#view-login").hidden = true;
  $("#view-app").hidden = false;
  $("#whoami").textContent = `${state.user.full_name || state.user.username} · ${state.user.role}`;
  $("#nav-users").hidden = state.user.role !== "admin";
  $("#btn-ntd-new").hidden = state.user.role !== "admin";
  navigate("dash");
}

async function navigate(name) {
  if (name === "users" && state.user.role !== "admin") return;
  showPage(name === "new" ? "new" : name);
  if (name === "dash") return loadDash();
  if (name === "new") return loadModels();
  if (name === "history") return loadHistory();
  if (name === "ntd") return loadNtd();
  if (name === "users") return loadUsers();
  if (name === "settings") return loadSettings();
}

async function loadDash() {
  const list = await api("/audits".replace(/^/, "/api"));
  const crit = list.reduce((s, a) => s + (a.summary.critical || 0), 0);
  const non = list.reduce((s, a) => s + (a.summary.noncritical || 0), 0);
  const done = list.filter((a) => a.status === "done").length;
  $("#dash-cards").innerHTML = `
    <div class="card"><div class="k">Всего проверок</div><div class="v">${list.length}</div></div>
    <div class="card ok"><div class="k">Завершено</div><div class="v">${done}</div></div>
    <div class="card crit"><div class="k">Критических замечаний</div><div class="v">${crit}</div></div>
    <div class="card warn"><div class="k">Некритических</div><div class="v">${non}</div></div>`;
  $("#dash-list").innerHTML = auditTable(list.slice(0, 12));
  bindAuditLinks();
}

async function loadHistory() {
  const list = await api("/api/audits");
  $("#hist-list").innerHTML = auditTable(list);
  bindAuditLinks();
}

function auditTable(list) {
  if (!list.length) return "<p class='muted'>Проверок ещё нет.</p>";
  return `<table><thead><tr><th>ID</th><th>Наименование</th><th>Объект</th><th>Режим</th><th>Статус</th><th>Крит.</th><th></th></tr></thead><tbody>
    ${list.map((a) => `<tr>
      <td>${a.id}</td><td>${esc(a.title)}</td><td>${esc(a.object_name)}</td>
      <td>${a.mode}</td><td>${a.status}</td><td>${a.summary.critical || 0}</td>
      <td><button class="btn" data-open="${a.id}">Открыть</button></td>
    </tr>`).join("")}
  </tbody></table>`;
}

function bindAuditLinks() {
  $$("[data-open]").forEach((b) => b.onclick = () => openAudit(+b.dataset.open));
}

let modelCatalog = null;
async function loadModels() {
  modelCatalog = await api("/api/models");
  renderModels();
}

function renderModels() {
  if (!modelCatalog) return;
  const box = $("#models-box");
  box.innerHTML = "";
  const groups = state.mode === "local" ? [["Локальные", modelCatalog.local]]
    : state.mode === "cloud" ? [["Облачные", modelCatalog.cloud]]
    : [["Локальные", modelCatalog.local], ["Облачные", modelCatalog.cloud]];
  for (const [title, arr] of groups) {
    const h = document.createElement("div");
    h.className = "muted";
    h.textContent = title;
    box.appendChild(h);
    arr.forEach((m) => {
      if (!m.id || m.id.endsWith(":")) {
        const p = document.createElement("div");
        p.className = "muted";
        p.textContent = m.note;
        box.appendChild(p);
        return;
      }
      const lab = document.createElement("label");
      if (!m.ready) lab.classList.add("off");
      lab.innerHTML = `<input type="checkbox" value="${esc(m.id)}" ${m.ready ? "" : "disabled"} />
        <span>${esc(m.name)}<small>${esc(m.note)}</small></span>`;
      box.appendChild(lab);
    });
  }
}

async function createAudit() {
  const systems = $$("#sys-box input:checked").map((i) => i.value);
  const models = $$("#models-box input:checked").map((i) => i.value);
  try {
    const a = await api("/api/audits", {
      method: "POST",
      body: JSON.stringify({
        title: $("#new-title").value.trim() || "Проверка без названия",
        object_name: $("#new-object").value.trim(),
        systems,
        mode: state.mode,
        models,
      }),
    });
    openAudit(a.id);
  } catch (e) { toast(e.message); }
}

async function openAudit(id) {
  state.auditId = id;
  showPage("workspace");
  $$(".sidebar nav button").forEach((b) => b.classList.remove("active"));
  await refreshWorkspace();
  if (state.poll) clearInterval(state.poll);
  state.poll = setInterval(refreshWorkspace, 2000);
}

async function refreshWorkspace() {
  if (!state.auditId) return;
  const a = await api(`/api/audits/${state.auditId}`);
  $("#ws-title").textContent = a.title;
  $("#ws-meta").textContent = `${a.object_name || "объект не указан"} · ${a.mode} · ${a.systems.join(", ") || "системы не выбраны"} · статус: ${a.status}`;
  $("#file-list").innerHTML = a.files.map((f) => `
    <div class="file-row">
      <div>${esc(f.filename)} <span class="muted">${f.classified_as} · ${f.parse_status}</span>
        ${f.parse_notes ? `<div class="muted">${esc(f.parse_notes)}</div>` : ""}</div>
      <span class="badge">${Math.round(f.size / 1024)} КБ</span>
      <button class="btn danger" data-del="${f.id}">×</button>
    </div>`).join("") || "<p class='muted'>Файлы не загружены.</p>";
  $$("[data-del]").forEach((b) => b.onclick = () => delFile(+b.dataset.del));
  $("#check-list").innerHTML = a.checks.map((c) => `
    <div class="check-row">
      <span class="badge ${c.status}">${c.status === "done" ? "●" : c.status === "skipped" ? "○" : "·"}</span>
      <div>${esc(c.title)}${c.reason ? `<div class="muted">${esc(c.reason)}</div>` : ""}</div>
      <span class="badge ${c.status}">${ruStatus(c.status)}</span>
    </div>`).join("") || "<p class='muted'>Перечень появится после запуска.</p>";
  $("#find-list").innerHTML = a.findings.map((f) => `
    <div class="find-row">
      <span class="badge ${f.severity}">${ruSev(f.severity)}</span>
      <div><b>${esc(f.title)}</b><div>${esc(f.description)}</div>
        <div class="muted">${(f.ntd_refs || []).map(esc).join("; ")}</div></div>
    </div>`).join("") || "<p class='muted'>Замечаний нет либо проверка не завершена.</p>";
  const msgs = await api(`/api/audits/${state.auditId}/dialog`);
  const box = $("#dialog");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  box.innerHTML = msgs.map((m) => `<div class="dlg ${m.role}"><span class="ts">${(m.ts || "").slice(11, 19)}</span>${esc(m.text)}</div>`).join("");
  if (atBottom) box.scrollTop = box.scrollHeight;
  if (a.status === "done" || a.status === "error") {
    /* keep polling lightly for dialog answers */
  }
}

function ruStatus(s) {
  return { done: "выполнена", skipped: "не проводилась", pending: "ожидает", error: "ошибка", running: "идёт" }[s] || s;
}
function ruSev(s) {
  return { critical: "критич.", noncritical: "некритич.", info: "сведение" }[s] || s;
}

async function uploadFiles(fileList) {
  if (!state.auditId) return toast("Сначала создайте проверку");
  const cls = $("#file-class").value;
  for (const file of fileList) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("classified_as", cls);
    try {
      await api(`/api/audits/${state.auditId}/files`, { method: "POST", body: fd });
    } catch (e) { toast(`${file.name}: ${e.message}`); }
  }
  refreshWorkspace();
}

async function delFile(id) {
  await api(`/api/audits/${state.auditId}/files/${id}`, { method: "DELETE" });
  refreshWorkspace();
}

async function startAudit() {
  try {
    await api(`/api/audits/${state.auditId}/start`, { method: "POST" });
    toast("Проверка запущена");
    refreshWorkspace();
  } catch (e) { toast(e.message); }
}

async function askDialog(e) {
  e.preventDefault();
  const text = $("#ask-input").value.trim();
  if (!text) return;
  $("#ask-input").value = "";
  try {
    await api(`/api/audits/${state.auditId}/dialog`, { method: "POST", body: JSON.stringify({ text }) });
    refreshWorkspace();
  } catch (err) { toast(err.message); }
}

async function downloadExport(kind) {
  try {
    const blob = await api(`/api/audits/${state.auditId}/export/${kind}`, { blob: true });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = kind === "bov" ? `vedomost_${state.auditId}.xlsx` : `otchet_${state.auditId}.${kind === "doc" ? "docx" : "xlsx"}`;
    a.click();
  } catch (e) { toast(e.message); }
}

async function loadNtd() {
  const list = await api("/api/ntd");
  $("#ntd-note").textContent = "Статусы: active — действует; replaced — заменён; partial — применяется частично; check — требует подтверждения. Перед проверкой выполняется актуализация.";
  $("#ntd-list").innerHTML = `<table><thead><tr><th>Шифр</th><th>Наименование</th><th>Вид</th><th>Статус</th><th>С</th><th>Заменён</th><th></th></tr></thead><tbody>
    ${list.map((d) => `<tr>
      <td>${esc(d.code)}</td><td>${esc(d.title)}</td><td>${esc(d.doc_type)}</td>
      <td><span class="badge ${d.status === "active" ? "done" : d.status === "replaced" ? "critical" : "skipped"}">${d.status}</span></td>
      <td>${esc(d.in_force_from)}</td><td>${esc(d.replaced_by)}</td>
      <td>${state.user.role === "admin" ? `<button class="btn" data-ntd="${d.id}">Изменить</button>` : ""}</td>
    </tr>`).join("")}
  </tbody></table>`;
  $$("[data-ntd]").forEach((b) => b.onclick = async () => {
    const d = list.find((x) => x.id === +b.dataset.ntd);
    editNtd(d);
  });
}

function editNtd(d) {
  const isNew = !d;
  d = d || { code: "", title: "", doc_type: "СП", status: "check", in_force_from: "", replaced_by: "", notes: "", body_text: "", applies_to: [], clauses: [] };
  $("#modal-card").innerHTML = `
    <h3>${isNew ? "Новый документ" : "Правка НТД"}</h3>
    <label>Шифр<input id="n-code" value="${esc(d.code)}" /></label>
    <label>Наименование<input id="n-title" value="${esc(d.title)}" /></label>
    <label>Вид<input id="n-type" value="${esc(d.doc_type)}" /></label>
    <label>Статус
      <select id="n-status">
        ${["active", "replaced", "partial", "check", "cancelled"].map((s) => `<option ${s === d.status ? "selected" : ""}>${s}</option>`).join("")}
      </select>
    </label>
    <label>Дата введения<input id="n-from" value="${esc(d.in_force_from)}" /></label>
    <label>Заменён на<input id="n-rep" value="${esc(d.replaced_by)}" /></label>
    <label>Примечание<textarea id="n-notes" style="width:100%;min-height:70px;background:#0d121a;color:inherit;border:1px solid var(--line);border-radius:10px;padding:8px">${esc(d.notes)}</textarea></label>
    <label>Полный текст (необязательно)<textarea id="n-body" style="width:100%;min-height:90px;background:#0d121a;color:inherit;border:1px solid var(--line);border-radius:10px;padding:8px">${esc(d.body_text || "")}</textarea></label>
    <div class="row" style="margin-top:12px">
      <button class="btn primary" id="n-save">Сохранить</button>
      ${isNew ? "" : `<button class="btn danger" id="n-del">Удалить</button>`}
      <button class="btn" id="n-cancel">Закрыть</button>
    </div>`;
  $("#modal").hidden = false;
  $("#n-cancel").onclick = () => $("#modal").hidden = true;
  $("#n-save").onclick = async () => {
    const payload = {
      code: $("#n-code").value, title: $("#n-title").value, doc_type: $("#n-type").value,
      status: $("#n-status").value, in_force_from: $("#n-from").value, replaced_by: $("#n-rep").value,
      notes: $("#n-notes").value, body_text: $("#n-body").value, applies_to: d.applies_to || [], clauses: d.clauses || [],
    };
    try {
      if (isNew) await api("/api/ntd", { method: "POST", body: JSON.stringify(payload) });
      else await api(`/api/ntd/${d.id}`, { method: "PUT", body: JSON.stringify(payload) });
      $("#modal").hidden = true;
      loadNtd();
    } catch (e) { toast(e.message); }
  };
  if (!isNew) $("#n-del").onclick = async () => {
    if (!confirm("Удалить документ из базы?")) return;
    await api(`/api/ntd/${d.id}`, { method: "DELETE" });
    $("#modal").hidden = true;
    loadNtd();
  };
}

async function checkNtd() {
  try {
    const r = await api("/api/ntd/check-actuality", { method: "POST" });
    toast(`Проверено документов: ${r.documents.length}. Онлайн: ${r.online ? "да" : "нет"}`);
    loadNtd();
  } catch (e) { toast(e.message); }
}

async function loadUsers() {
  const list = await api("/api/users");
  $("#user-list").innerHTML = `<table><thead><tr><th>Логин</th><th>Имя</th><th>Роль</th><th>Статус</th><th></th></tr></thead><tbody>
    ${list.map((u) => `<tr>
      <td>${esc(u.username)}</td><td>${esc(u.full_name)}</td><td>${u.role}</td>
      <td>${u.is_active ? "активен" : "заблокирован"}</td>
      <td>
        <button class="btn" data-uedit="${u.id}">Изменить</button>
        <button class="btn" data-ublk="${u.id}">${u.is_active ? "Блок" : "Снять блок"}</button>
        <button class="btn danger" data-udel="${u.id}">Удалить</button>
      </td>
    </tr>`).join("")}
  </tbody></table>`;
  $$("[data-uedit]").forEach((b) => b.onclick = () => {
    const u = list.find((x) => x.id === +b.dataset.uedit);
    editUser(u);
  });
  $$("[data-ublk]").forEach((b) => b.onclick = async () => {
    const u = list.find((x) => x.id === +b.dataset.ublk);
    await api(`/api/users/${u.id}/${u.is_active ? "block" : "unblock"}`, { method: "POST" });
    loadUsers();
  });
  $$("[data-udel]").forEach((b) => b.onclick = async () => {
    if (!confirm("Удалить пользователя?")) return;
    try { await api(`/api/users/${b.dataset.udel}`, { method: "DELETE" }); loadUsers(); }
    catch (e) { toast(e.message); }
  });
}

function editUser(u) {
  const isNew = !u;
  u = u || { username: "", full_name: "", role: "engineer", is_active: true };
  $("#modal-card").innerHTML = `
    <h3>${isNew ? "Новый пользователь" : "Пользователь"}</h3>
    <label>Логин<input id="u-name" value="${esc(u.username)}" ${isNew ? "" : "disabled"} /></label>
    <label>ФИО<input id="u-full" value="${esc(u.full_name)}" /></label>
    <label>Роль<select id="u-role">
      ${["admin", "engineer", "viewer"].map((r) => `<option ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}
    </select></label>
    <label>Пароль ${isNew ? "" : "(пусто — не менять)"}<input id="u-pass" type="password" /></label>
    <label><input id="u-act" type="checkbox" ${u.is_active ? "checked" : ""} /> активен</label>
    <div class="row" style="margin-top:12px">
      <button class="btn primary" id="u-save">Сохранить</button>
      <button class="btn" id="u-cancel">Закрыть</button>
    </div>`;
  $("#modal").hidden = false;
  $("#u-cancel").onclick = () => $("#modal").hidden = true;
  $("#u-save").onclick = async () => {
    const payload = {
      username: $("#u-name").value, full_name: $("#u-full").value, role: $("#u-role").value,
      is_active: $("#u-act").checked, password: $("#u-pass").value || null,
    };
    try {
      if (isNew) await api("/api/users", { method: "POST", body: JSON.stringify(payload) });
      else await api(`/api/users/${u.id}`, { method: "PATCH", body: JSON.stringify(payload) });
      $("#modal").hidden = true;
      loadUsers();
    } catch (e) { toast(e.message); }
  };
}

async function loadSettings() {
  const s = await api("/api/settings");
  $("#set-company").value = s.company_name || "";
  $("#set-qty").value = s.qty_tolerance_pct || "5";
  $("#set-len").value = s.length_tolerance_pct || "10";
}

async function saveSettings() {
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        values: {
          company_name: $("#set-company").value,
          qty_tolerance_pct: $("#set-qty").value,
          length_tolerance_pct: $("#set-len").value,
        },
      }),
    });
    toast("Сохранено");
  } catch (e) { toast(e.message); }
}

async function savePass() {
  const p = $("#set-pass").value;
  if (p.length < 8) return toast("Пароль не короче 8 символов");
  await api("/api/auth/password", { method: "POST", body: JSON.stringify({ password: p }) });
  $("#set-pass").value = "";
  toast("Пароль изменён");
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

boot();
