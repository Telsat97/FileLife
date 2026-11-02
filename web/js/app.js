// FileLife Web — organiza, expira e resume arquivos no seu Google Drive.
// Tudo roda no seu navegador: não existe servidor do FileLife guardando seus dados.

import * as G from "./google.js";
import * as R from "./rules.js";
import { sha256File } from "./sha256.js";

const CONFIG_NAME = "_filelife_config.json";
const ROOT_NAME = "FileLife";
const COLD_NAME = "_ArquivoFrio";
const AI_MAX_BYTES = 20 * 1024 * 1024;
const AI_MAX_CHARS = 600_000;
const AI_MODEL = "claude-opus-5-5";
const AI_KEY_STORE = "filelife_anthropic_key";
const MIN_FILE_AGE = 5000;

const S = {
  user: null, rootId: null, configId: null, settings: R.sanitizeSettings(null),
  files: [], archived: [], filter: "Todos", tab: "active", search: "",
  activity: [], busy: 0, localDir: null, autoLocal: false, autoSince: 0, timers: [],
};

// ---------------------------------------------------------------------------
// DOM seguro: nunca usamos innerHTML com dados (nomes de arquivo, respostas da IA…)
// ---------------------------------------------------------------------------
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const $ = (id) => document.getElementById(id);

function toast(msg, isError) {
  const t = $("toast");
  t.textContent = (isError ? "⚠️ " : "") + msg;
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), isError ? 6000 : 3500);
}

function log(msg) {
  S.activity.unshift({ ts: Date.now(), msg });
  S.activity.length = Math.min(S.activity.length, 80);
  renderFeed();
}

function fail(e) {
  console.error(e);
  if (e instanceof G.AuthError) { S.user = null; render(); }
  toast(e.message || "Algo deu errado.", true);
}

async function busy(label, fn) {
  S.busy++; renderBusy(label);
  try { return await fn(); } finally { S.busy--; renderBusy(); }
}

function openSafe(url) {
  if (typeof url === "string" && /^https:\/\/(drive|docs)\.google\.com\//.test(url)) {
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

// ---------------------------------------------------------------------------
// Configurações (ficam no próprio Drive do usuário: FileLife/_filelife_config.json)
// ---------------------------------------------------------------------------
async function loadSettings() {
  const found = await G.listAll(
    `name = '${CONFIG_NAME}' and '${G.q(S.rootId)}' in parents and trashed = false`, "id");
  if (found.length) {
    S.configId = found[0].id;
    try {
      S.settings = R.sanitizeSettings(JSON.parse(await (await G.download(S.configId)).text()));
    } catch { S.settings = R.sanitizeSettings(null); }
  } else {
    S.settings = R.sanitizeSettings(null);
    S.configId = (await G.saveJson(null, CONFIG_NAME, S.rootId, S.settings, { fl_config: "1" })).id;
  }
}

let saveTimer = null;
function saveSettings() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => G.saveJson(S.configId, CONFIG_NAME, S.rootId, S.settings).catch(fail), 800);
}

// ---------------------------------------------------------------------------
// Arquivos no Drive
// ---------------------------------------------------------------------------
const props = (f) => f.appProperties || {};
const expiryOf = (f) => props(f).fl_exp === "never" ? null : Number(props(f).fl_exp) || null;

async function refreshFiles() {
  const all = await G.listAll(
    "appProperties has { key='fl_app' and value='1' } and trashed = false", G.FILE_FIELDS);
  S.files = all.filter(f => props(f).fl_status !== "archived");
  S.archived = all.filter(f => props(f).fl_status === "archived");
  render();
}

async function setProps(file, changes, extra = {}) {
  const updated = await G.api(`/files/${encodeURIComponent(file.id)}`, {
    method: "PATCH", query: { fields: G.FILE_FIELDS, ...extra }, body: { appProperties: changes } });
  Object.assign(file, updated);
  return file;
}

/** Envia um arquivo ao Drive já organizado. Retorna {file, verified} ou {duplicate}. */
async function addFile(blob, originalName, onProgress) {
  if (R.shouldIgnore(originalName)) return { skipped: "ignorado" };
  if (blob.size === 0) return { skipped: "vazio" };
  const hash = await sha256File(blob, p => onProgress?.(p * 0.3));

  // Dedupe: o mesmo conteúdo já está no Drive?
  const dupes = await G.listAll(
    `appProperties has { key='fl_sha' and value='${hash}' } and trashed = false`, G.FILE_FIELDS);
  const dup = dupes.find(f => f.sha256Checksum === hash) || dupes[0];
  if (dup) {
    S.settings.stats.duplicates++; S.settings.stats.duplicate_bytes += blob.size; saveSettings();
    return { duplicate: dup, verified: dup.sha256Checksum === hash };
  }

  const category = R.categorize(originalName);
  const tag = R.detectTag(originalName, S.settings.tags);
  const ms = R.parseDuration(S.settings.default_expiry[category] || "1w");
  const parent = await G.folder(category, S.rootId);
  const metadata = {
    name: R.buildName(originalName, tag),
    parents: [parent],
    appProperties: {
      fl_app: "1", fl_status: "active", fl_cat: category, fl_tag: tag || "",
      fl_exp: ms == null ? "never" : String(Date.now() + ms), fl_sha: hash,
      fl_created: String(Date.now()), fl_orig: R.fitProperty("fl_orig", originalName),
    },
  };
  let file = await G.upload(blob, metadata, p => onProgress?.(0.3 + p * 0.7));

  // Confere a impressão digital calculada pelo Google com a nossa
  for (let i = 0; i < 3 && !file.sha256Checksum; i++) {
    await new Promise(r => setTimeout(r, 1500));
    file = await G.api(`/files/${encodeURIComponent(file.id)}`, { query: { fields: G.FILE_FIELDS } });
  }
  if (file.sha256Checksum && file.sha256Checksum !== hash) {
    await G.api(`/files/${encodeURIComponent(file.id)}`, { method: "PATCH", body: { trashed: true } }).catch(() => {});
    throw new Error(`${originalName} chegou diferente no Drive. Nada foi apagado; tente de novo.`);
  }
  S.settings.stats.uploaded++; saveSettings();
  S.files.unshift(file);
  log(`📥 ${originalName} → ${category}${tag ? ` [${tag}]` : ""} · expira ${R.fmtDate(ms == null ? null : Date.now() + ms)}`);
  return { file, verified: file.sha256Checksum === hash };
}

async function uploadFiles(fileList) {
  const list = [...fileList];
  if (!list.length) return;
  await busy(`Enviando ${list.length} arquivo(s)...`, async () => {
    let done = 0;
    for (const f of list) {
      try {
        const r = await addFile(f, f.name, p => renderBusy(`Enviando ${f.name} (${Math.round(p * 100)}%)`));
        if (r.duplicate) log(`♻️ ${f.name} já está no seu Drive — não foi enviado de novo.`);
        if (r.file) done++;
      } catch (e) { fail(e); if (e instanceof G.AuthError) break; }
    }
    if (done) toast(`${done} arquivo(s) organizado(s) no seu Drive.`);
  });
  render();
}

// ---------------------------------------------------------------------------
// Expiração e Arquivo Frio
// ---------------------------------------------------------------------------
async function expireFile(file) {
  if (S.settings.on_expire === "trash") {
    await G.api(`/files/${encodeURIComponent(file.id)}`, { method: "PATCH", body: { trashed: true } });
    log(`🗑️ ${file.name} expirou e foi para a lixeira do Drive (recuperável por 30 dias).`);
  } else {
    const cold = await G.folder(COLD_NAME, S.rootId);
    await setProps(file, { fl_status: "archived", fl_archived: String(Date.now()) },
                   { addParents: cold, removeParents: (file.parents || []).join(",") });
    log(`🧊 ${file.name} expirou → Arquivo Frio.`);
  }
  S.settings.stats.expired++; saveSettings();
}

async function runCleanup(manual) {
  await refreshFiles(); // pega mudanças feitas em outro aparelho/aba
  const now = Date.now();
  const expired = S.files.filter(f => { const e = expiryOf(f); return e != null && e <= now; });
  for (const f of expired) {
    try { await expireFile(f); } catch (e) { if (e instanceof G.AuthError) return fail(e); console.error(e); }
  }
  if (expired.length) await refreshFiles();
  if (manual) toast(expired.length ? `${expired.length} arquivo(s) expirado(s).` : "Nenhum arquivo expirado.");
}

async function restoreFile(file) {
  const cat = R.ALL_CATEGORIES.includes(props(file).fl_cat) ? props(file).fl_cat : "Outros";
  const ms = R.parseDuration(S.settings.default_expiry[cat] || "1w");
  const parent = await G.folder(cat, S.rootId);
  await setProps(file, { fl_status: "active", fl_exp: ms == null ? "never" : String(Date.now() + ms) },
                 { addParents: parent, removeParents: (file.parents || []).join(",") });
  log(`↩️ ${file.name} voltou para ${cat}.`);
  await refreshFiles();
}

async function extend(file, value) {
  const ms = R.parseDuration(value);
  if (ms === undefined) return;
  await setProps(file, { fl_exp: ms == null ? "never" : String(Date.now() + ms) });
  toast(ms == null ? "Esse arquivo não vai mais expirar." : `Novo prazo: ${R.fmtDate(Date.now() + ms)}`);
  render();
}

// ---------------------------------------------------------------------------
// Pasta do computador (Chrome/Edge): envia para o Drive e libera espaço
// ---------------------------------------------------------------------------
const supportsFolders = "showDirectoryPicker" in window;

function idb() {
  return new Promise((res, rej) => {
    const r = indexedDB.open("filelife", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("kv");
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
}
async function kv(key, value) {
  const db = await idb();
  return new Promise((res, rej) => {
    const tx = db.transaction("kv", value === undefined ? "readonly" : "readwrite");
    const store = tx.objectStore("kv");
    const r = value === undefined ? store.get(key) : value === null ? store.delete(key) : store.put(value, key);
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
}

async function pickLocalFolder() {
  try {
    const dir = await window.showDirectoryPicker({ id: "filelife", mode: "readwrite", startIn: "downloads" });
    S.localDir = dir; S.autoLocal = false;
    await kv("localDir", dir).catch(() => {}); // lembrar a pasta é só conveniência
    render();
    await scanLocal(true);
  } catch (e) { if (e.name !== "AbortError") fail(e); }
}

async function localPermission(ask) {
  if (!S.localDir) return false;
  const opts = { mode: "readwrite" };
  if (await S.localDir.queryPermission(opts) === "granted") return true;
  return ask ? (await S.localDir.requestPermission(opts)) === "granted" : false;
}

async function localCandidates() {
  const out = [];
  const now = Date.now();
  for await (const entry of S.localDir.values()) {
    if (entry.kind !== "file" || R.shouldIgnore(entry.name)) continue;
    const file = await entry.getFile();
    if (file.size === 0 || now - file.lastModified < MIN_FILE_AGE) continue; // ainda baixando
    out.push(file);
  }
  return out;
}

/** manual=true: mostra a lista e pede confirmação. Automático: só arquivos novos. */
async function scanLocal(manual) {
  if (!(await localPermission(manual))) { if (manual) toast("Permissão para a pasta negada.", true); return; }
  let files = await localCandidates();
  if (!manual) files = files.filter(f => f.lastModified >= S.autoSince);
  if (!files.length) { if (manual) toast("Nenhum arquivo novo nessa pasta."); return; }
  if (manual) {
    const total = files.reduce((a, f) => a + f.size, 0);
    const remove = S.settings.remove_local_after_upload;
    const ok = await confirmModal("Enviar arquivos da pasta para o Drive?",
      [h("p", {}, `${files.length} arquivo(s), ${R.humanSize(total)}, da pasta “${S.localDir.name}”.`),
       h("ul", { class: "mini-list" }, files.slice(0, 12).map(f => h("li", {}, `${f.name} (${R.humanSize(f.size)})`)),
         files.length > 12 ? h("li", {}, `…e mais ${files.length - 12}`) : null),
       h("p", { class: "muted" }, remove
         ? "Depois de confirmar que cada arquivo chegou idêntico ao Drive, a cópia do computador é removida para liberar espaço."
         : "As cópias do computador serão mantidas (você pode mudar isso nas Configurações).")],
      "Enviar");
    if (!ok) return;
  }
  await busy("Enviando da pasta do computador...", async () => {
    for (const f of files) {
      try {
        const r = await addFile(f, f.name, p => renderBusy(`Enviando ${f.name} (${Math.round(p * 100)}%)`));
        if (r.duplicate) log(`♻️ ${f.name} já estava no Drive.`);
        if (S.settings.remove_local_after_upload && r.verified) {
          await S.localDir.removeEntry(f.name);
          S.settings.stats.local_freed_bytes += f.size; saveSettings();
          log(`💾 ${f.name} removido do computador (cópia conferida no Drive).`);
        }
      } catch (e) { fail(e); if (e instanceof G.AuthError) break; }
    }
  });
  render();
}

async function toggleAutoLocal() {
  if (!S.autoLocal) {
    if (!(await localPermission(true))) return toast("Permissão para a pasta negada.", true);
    S.autoLocal = true; S.autoSince = Date.now();
    toast("Novos arquivos dessa pasta serão enviados enquanto esta aba estiver aberta.");
  } else {
    S.autoLocal = false;
  }
  render();
}

// ---------------------------------------------------------------------------
// Resumo com IA (Claude) — usa a chave do próprio usuário, que fica só neste navegador
// ---------------------------------------------------------------------------
function getAiKey() {
  try { return localStorage.getItem(AI_KEY_STORE) || sessionStorage.getItem(AI_KEY_STORE) || ""; }
  catch { return ""; }
}
function setAiKey(key, remember) {
  try {
    localStorage.removeItem(AI_KEY_STORE); sessionStorage.removeItem(AI_KEY_STORE);
    if (key) (remember ? localStorage : sessionStorage).setItem(AI_KEY_STORE, key);
  } catch {}
}

function canSummarize(file) {
  const ext = R.extOf(file.name);
  return ["pdf", "txt", "md", "csv"].includes(ext) && Number(file.size) <= AI_MAX_BYTES;
}

function blobToBase64(blob) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(String(r.result).split(",")[1]);
    r.onerror = () => rej(r.error);
    r.readAsDataURL(blob);
  });
}

async function summarize(file) {
  if (file.description) return showSummary(file);
  const key = getAiKey();
  if (!key) return openSettings("ai");
  await busy("Resumindo com IA...", async () => {
    const blob = await G.download(file.id);
    let doc;
    if (R.extOf(file.name) === "pdf") {
      doc = { type: "document", source: { type: "base64", media_type: "application/pdf", data: await blobToBase64(blob) } };
    } else {
      const text = await blob.text();
      if (text.length > AI_MAX_CHARS) throw new Error("Documento grande demais para resumir.");
      doc = { type: "document", source: { type: "text", media_type: "text/plain", data: text }, title: file.name };
    }
    const resp = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2026-10-21",
        "anthropic-beta": "server-side-fallback-2026-07-01",
        "anthropic-dangerous-direct-browser-access": "true",
      },
      body: JSON.stringify({
        model: AI_MODEL,
        max_tokens: 16000,
        output_config: { effort: "low" },
        fallbacks: "default", // se o modelo recusar o conteúdo, a API tenta outro modelo
        messages: [{ role: "user", content: [doc, { type: "text", text:
          "Resuma este documento em exatamente 3 tópicos curtos, em português do Brasil, no formato " +
          "'- texto'. Foque no que importa para quem baixou o arquivo: do que se trata, os pontos " +
          "principais e qualquer prazo, valor ou ação necessária. Responda apenas com os 3 tópicos." }] }],
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 401) throw new Error("Chave da Anthropic inválida. Confira nas Configurações.");
    if (resp.status === 429) throw new Error("Limite de uso da IA atingido. Tente daqui a pouco.");
    if (!resp.ok) throw new Error(data.error?.message || `Erro ${resp.status} da IA.`);
    if (data.stop_reason === "refusal") throw new Error("A IA não pôde resumir esse conteúdo.");
    const text = (data.content || []).filter(b => b.type === "text").map(b => b.text).join("\n")
      .replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, "").trim().slice(0, 4000);
    if (!text) throw new Error("A IA não devolveu um resumo.");
    const updated = await G.api(`/files/${encodeURIComponent(file.id)}`, {
      method: "PATCH", query: { fields: G.FILE_FIELDS }, body: { description: text } });
    Object.assign(file, updated);
    log(`🧠 Resumo pronto: ${file.name}`);
    showSummary(file);
  });
}

// ---------------------------------------------------------------------------
// Modais
// ---------------------------------------------------------------------------
function modal(title, body, actions) {
  const overlay = $("overlay");
  overlay.replaceChildren(h("div", { class: "modal", role: "dialog", "aria-modal": "true" },
    h("header", {}, h("h3", {}, title)),
    h("div", { class: "body" }, body),
    h("div", { class: "foot" }, actions || h("button", { class: "btn", onclick: closeModal }, "Fechar"))));
  overlay.classList.add("open");
}
function closeModal() { $("overlay").classList.remove("open"); $("overlay").replaceChildren(); }

function confirmModal(title, body, okLabel = "Confirmar", danger) {
  return new Promise(res => {
    const done = (v) => { closeModal(); res(v); };
    modal(title, body, [
      h("button", { class: "btn", onclick: () => done(false) }, "Cancelar"),
      h("button", { class: `btn ${danger ? "danger-fill" : "primary"}`, onclick: () => done(true) }, okLabel)]);
  });
}

function showSummary(file) {
  modal(`Resumo: ${props(file).fl_orig || file.name}`,
    [h("div", { class: "summary-text" }, file.description),
     h("p", { class: "muted small" }, "Gerado por IA (Claude). Confira as informações importantes no documento.")],
    [h("button", { class: "btn", onclick: async () => {
        closeModal();
        try { await G.api(`/files/${encodeURIComponent(file.id)}`, { method: "PATCH", body: { description: "" } });
              file.description = ""; await summarize(file); } catch (e) { fail(e); } } }, "Gerar de novo"),
     h("button", { class: "btn primary", onclick: closeModal }, "Fechar")]);
}

function openSettings(focus) {
  const st = S.settings;
  const selects = {};
  const durOpts = [["1d", "1 dia"], ["3d", "3 dias"], ["1w", "1 semana"], ["2w", "2 semanas"], ["1m", "1 mês"],
                   ["3m", "3 meses"], ["1y", "1 ano"], ["never", "Nunca"]];
  const select = (opts, cur) => h("select", {}, opts.map(([v, l]) => h("option", { value: v, selected: v === cur }, l)));
  const rows = R.ALL_CATEGORIES.map(cat => {
    selects[cat] = select(durOpts, st.default_expiry[cat]);
    return h("div", { class: "form-row" }, h("label", {}, `${R.ICONS[cat]} ${cat}`), selects[cat]);
  });
  const onExpire = select([["archive", "Mover para o Arquivo Frio (pasta no Drive)"],
                           ["trash", "Mandar para a lixeira do Drive (recuperável por 30 dias)"]], st.on_expire);
  const removeLocal = select([["1", "Sim, depois de conferir que chegou idêntico"], ["0", "Não, manter a cópia"]],
                             st.remove_local_after_upload ? "1" : "0");
  const hasKey = !!getAiKey();
  const keyInput = h("input", { type: "password", autocomplete: "off", spellcheck: "false",
                                placeholder: hasKey ? "Chave salva neste navegador. Cole outra para trocar" : "sk-ant-..." });
  const remember = h("input", { type: "checkbox", checked: !!localStorage.getItem(AI_KEY_STORE) || !hasKey });

  modal("Configurações", [
    h("div", { class: "section-title" }, "Prazo padrão por categoria"), rows,
    h("div", { class: "section-title" }, "Quando um arquivo expira"), h("div", { class: "form-row" }, h("label", {}, "Ação"), onExpire),
    h("div", { class: "section-title" }, "Pasta do computador"),
    h("div", { class: "form-row" }, h("label", {}, "Remover do computador após enviar"), removeLocal),
    h("div", { class: "section-title", id: "ai-section" }, "Resumo com IA (opcional)"),
    h("p", { class: "muted small" },
      "Usa a sua própria chave da Anthropic (crie em platform.claude.com). A chave fica só neste navegador ",
      "e vai direto daqui para a Anthropic, sem passar por nenhum servidor do FileLife. Ao resumir, o conteúdo ",
      "do documento é enviado à Anthropic. Não use em documentos confidenciais. O uso é cobrado na sua chave."),
    h("div", { class: "form-row" }, h("label", {}, `Chave da Anthropic ${hasKey ? "✅" : ""}`), keyInput),
    h("label", { class: "check" }, remember, " Lembrar neste navegador (desmarque em computadores compartilhados)"),
    hasKey ? h("button", { class: "btn small danger", onclick: () => { setAiKey(""); closeModal(); toast("Chave removida."); } }, "Remover chave") : null,
  ], [
    h("button", { class: "btn", onclick: closeModal }, "Cancelar"),
    h("button", { class: "btn primary", onclick: () => {
      const key = keyInput.value.trim();
      if (key && !/^sk-ant-[A-Za-z0-9_-]{20,300}$/.test(key)) return toast("Isso não parece uma chave da Anthropic (começa com sk-ant-).", true);
      for (const cat of R.ALL_CATEGORIES) st.default_expiry[cat] = selects[cat].value;
      st.on_expire = onExpire.value === "trash" ? "trash" : "archive";
      st.remove_local_after_upload = removeLocal.value === "1";
      if (key) setAiKey(key, remember.checked);
      saveSettings(); closeModal(); toast("Configurações salvas."); render();
    } }, "Salvar")]);
  if (focus === "ai") { $("ai-section")?.scrollIntoView(); keyInput.focus(); }
}

function openReport() {
  const st = S.settings.stats;
  const inDrive = S.files.reduce((a, f) => a + Number(f.size || 0), 0);
  const byCat = {};
  for (const f of S.files) { const c = props(f).fl_cat || "Outros"; byCat[c] = (byCat[c] || 0) + 1; }
  modal("Relatório do FileLife", [
    h("div", { class: "report-grid" },
      [["Arquivos organizados", st.uploaded], ["Duplicatas evitadas", st.duplicates],
       ["Espaço poupado com duplicatas", R.humanSize(st.duplicate_bytes)],
       ["Espaço liberado no computador", R.humanSize(st.local_freed_bytes)],
       ["Arquivos expirados", st.expired], ["Tempo economizado", `${(st.uploaded * 1.5 / 60).toFixed(1)} h`],
       ["Ativos no Drive", `${S.files.length} (${R.humanSize(inDrive)})`], ["No Arquivo Frio", S.archived.length]]
        .map(([l, v]) => h("div", { class: "card stat" }, h("div", { class: "label" }, l), h("div", { class: "value" }, v)))),
    h("div", { class: "section-title" }, "Por categoria"),
    h("table", {}, h("tbody", {}, Object.entries(byCat).map(([c, n]) => h("tr", {}, h("td", {}, `${R.ICONS[c] || ""} ${c}`), h("td", {}, n))))),
  ], [h("button", { class: "btn", onclick: () => window.print() }, "🖨️ Imprimir / salvar PDF"),
      h("button", { class: "btn primary", onclick: closeModal }, "Fechar")]);
}

// ---------------------------------------------------------------------------
// Renderização
// ---------------------------------------------------------------------------
function render() {
  const logged = !!S.user;
  $("login-screen").hidden = logged;
  $("app-screen").hidden = !logged;
  renderAccount();
  if (!logged) return;
  renderStats(); renderLocal(); renderTabs(); renderChips(); renderFiles(); renderFeed();
}

function renderBusy(label) {
  const b = $("busy");
  b.hidden = !S.busy;
  if (label) $("busy-label").textContent = label;
}

function renderAccount() {
  const slot = $("account");
  if (!S.user) { slot.replaceChildren(); return; }
  const initial = (S.user.displayName || S.user.emailAddress || "?")[0].toUpperCase();
  const photo = S.user.photoLink && /^https:\/\/[a-z0-9.-]+\.googleusercontent\.com\//.test(S.user.photoLink)
    ? h("img", { src: S.user.photoLink, alt: "", referrerpolicy: "no-referrer" }) : initial;
  slot.replaceChildren(h("div", { class: "account" },
    h("div", { class: "avatar" }, photo),
    h("div", { class: "who" }, h("b", {}, S.user.displayName || "Conta Google"), h("span", {}, S.user.emailAddress || "")),
    h("button", { class: "btn small ghost", title: "Abrir a pasta FileLife no Google Drive",
                  onclick: () => openSafe(`https://drive.google.com/drive/folders/${encodeURIComponent(S.rootId)}`) }, "Drive ↗"),
    h("button", { class: "btn small ghost danger", onclick: doLogout }, "Sair")));
}

function renderStats() {
  const st = S.settings.stats;
  const inDrive = S.files.reduce((a, f) => a + Number(f.size || 0), 0);
  const cards = [
    ["📁", "Arquivos ativos", S.files.length, R.humanSize(inDrive)],
    ["💾", "Liberado no PC", R.humanSize(st.local_freed_bytes + st.duplicate_bytes), "envios + duplicatas"],
    ["⏱️", "Tempo economizado", `${(st.uploaded * 1.5 / 60).toFixed(1)} h`, `${st.uploaded} arquivos organizados`],
    ["♻️", "Duplicatas evitadas", st.duplicates, ""],
    ["🧊", "Arquivo Frio", S.archived.length, `${st.expired} expirados`],
  ];
  $("stats").replaceChildren(...cards.map(([i, l, v, sub]) => h("div", { class: "card stat" },
    h("div", { class: "label" }, `${i} ${l}`), h("div", { class: "value" }, v), h("div", { class: "label small" }, sub))));
}

function renderLocal() {
  const box = $("local");
  if (!supportsFolders) {
    box.replaceChildren(h("p", { class: "muted small" },
      "💡 No Chrome ou Edge do computador você também pode conectar uma pasta (ex.: Downloads) para enviar arquivos ao Drive e liberar espaço."));
    return;
  }
  if (!S.localDir) {
    box.replaceChildren(h("div", { class: "local-row" },
      h("div", {}, h("b", {}, "Pasta do computador"),
        h("p", { class: "muted small" }, "Escolha sua pasta Downloads: o FileLife envia os arquivos para o Drive, organizados, e libera espaço no PC.")),
      h("button", { class: "btn", onclick: pickLocalFolder }, "📂 Conectar pasta")));
    return;
  }
  box.replaceChildren(h("div", { class: "local-row" },
    h("div", {}, h("b", {}, `📂 ${S.localDir.name}`),
      h("p", { class: "muted small" }, S.autoLocal
        ? "Enviando novos arquivos automaticamente enquanto esta aba estiver aberta."
        : "Conectada. Envie quando quiser ou ligue o envio automático.")),
    h("div", { class: "row-actions" },
      h("button", { class: "btn", onclick: () => scanLocal(true).catch(fail) }, "⬆️ Enviar agora"),
      h("button", { class: `btn ${S.autoLocal ? "primary" : ""}`, onclick: () => toggleAutoLocal().catch(fail) },
        S.autoLocal ? "⏸ Automático ligado" : "▶ Automático"),
      h("button", { class: "btn ghost small", onclick: async () => { S.localDir = null; S.autoLocal = false; await kv("localDir", null); render(); } }, "Desconectar"))));
}

function renderTabs() {
  for (const b of document.querySelectorAll("[data-tab]")) b.classList.toggle("on", b.dataset.tab === S.tab);
  $("tab-archived-count").textContent = S.archived.length ? ` (${S.archived.length})` : "";
}

function currentList() {
  const list = S.tab === "archived" ? S.archived : S.files;
  const q = S.search.toLowerCase();
  return list
    .filter(f => S.filter === "Todos" || (props(f).fl_cat || "Outros") === S.filter)
    .filter(f => !q || f.name.toLowerCase().includes(q) || (props(f).fl_orig || "").toLowerCase().includes(q))
    .sort((a, b) => (expiryOf(a) ?? Infinity) - (expiryOf(b) ?? Infinity));
}

function renderChips() {
  const list = S.tab === "archived" ? S.archived : S.files;
  const cats = ["Todos", ...new Set(list.map(f => props(f).fl_cat || "Outros"))];
  if (!cats.includes(S.filter)) S.filter = "Todos";
  $("chips").replaceChildren(...cats.map(c => h("button", { class: `chip ${c === S.filter ? "on" : ""}`,
    onclick: () => { S.filter = c; renderChips(); renderFiles(); } }, c)));
}

function expiryBadge(f) {
  const e = expiryOf(f);
  if (e == null) return h("span", { class: "badge b-ok" }, "📌 sem prazo");
  const days = (e - Date.now()) / R.DAY;
  const cls = days <= 1 ? "b-danger" : days <= 7 ? "b-warn" : "b-ok";
  const txt = days <= 0 ? "expira agora" : days < 1 ? "expira hoje" : `expira em ${Math.round(days)}d`;
  return h("span", { class: `badge ${cls}`, title: R.fmtDate(e) }, `⏳ ${txt}`);
}

function renderFiles() {
  const list = currentList();
  const ul = $("files");
  if (!list.length) {
    const empty = S.tab === "archived"
      ? ["Arquivo Frio vazio", "Arquivos expirados aparecem aqui e podem ser restaurados."]
      : (S.files.length ? ["Nada encontrado", "Tente outra busca ou categoria."]
                        : ["Tudo limpo por aqui ✨", "Arraste arquivos para cá ou conecte uma pasta do computador."]);
    ul.replaceChildren(h("li", { class: "empty" }, h("b", {}, empty[0]), empty[1]));
    return;
  }
  const durations = [["", "Prazo..."], ["1d", "+1 dia"], ["1w", "+1 semana"], ["1m", "+1 mês"],
                     ["3m", "+3 meses"], ["1y", "+1 ano"], ["never", "Manter sempre"]];
  ul.replaceChildren(...list.map(f => {
    const p = props(f), cat = p.fl_cat || "Outros";
    const actions = S.tab === "archived"
      ? [h("button", { class: "btn small", onclick: () => openSafe(f.webViewLink) }, "Abrir"),
         h("button", { class: "btn small primary", onclick: () => restoreFile(f).catch(fail) }, "↩️ Restaurar")]
      : [h("button", { class: "btn small", onclick: () => openSafe(f.webViewLink) }, "Abrir"),
         canSummarize(f) ? h("button", { class: "btn small", onclick: () => summarize(f).catch(fail) },
                             f.description ? "🧠 Resumo" : "🧠 Resumir") : null,
         h("select", { "aria-label": "Mudar prazo", onchange: (ev) => { const v = ev.target.value; ev.target.value = ""; if (v) extend(f, v).catch(fail); } },
           durations.map(([v, l]) => h("option", { value: v }, l))),
         h("button", { class: "btn small ghost danger", title: "Expirar agora", onclick: async () => {
           const where = S.settings.on_expire === "trash" ? "para a lixeira do Drive" : "para o Arquivo Frio";
           if (await confirmModal("Expirar agora?", h("p", {}, `“${f.name}” vai ${where}.`), "Expirar", true)) {
             try { await expireFile(f); await refreshFiles(); } catch (e) { fail(e); }
           } } }, "✕")];
    return h("li", { class: "file" },
      h("div", { class: "ficon" }, R.ICONS[cat] || "📦"),
      h("div", { class: "fmain" },
        h("div", { class: "fname", title: f.name }, p.fl_orig || f.name),
        h("div", { class: "fmeta" },
          p.fl_tag ? h("span", { class: `badge b-tag tag-${p.fl_tag}` }, p.fl_tag) : null,
          S.tab === "archived" ? h("span", { class: "badge b-pc" }, `🧊 desde ${R.fmtDate(Number(p.fl_archived) || null)}`) : expiryBadge(f),
          h("span", {}, R.humanSize(f.size)))),
      h("div", { class: "factions" }, actions));
  }));
}

function renderFeed() {
  const feed = $("feed");
  if (!feed) return;
  feed.replaceChildren(...(S.activity.length ? S.activity.map(a => h("li", {},
    h("time", {}, new Date(a.ts).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })), a.msg))
    : [h("li", { class: "muted" }, "Nenhuma atividade nesta sessão ainda.")]));
}

// ---------------------------------------------------------------------------
// Login e inicialização
// ---------------------------------------------------------------------------
async function doLogin() {
  try {
    await G.login();
    await busy("Abrindo seu Drive...", async () => {
      S.user = (await G.api("/about", { query: { fields: "user(displayName,emailAddress,photoLink)" } })).user;
      S.rootId = await G.folder(ROOT_NAME);
      await loadSettings();
      await refreshFiles();
      await runCleanup(false);
    });
    try { localStorage.setItem("filelife_seen", "1"); } catch {}
    startTimers();
    render();
  } catch (e) { fail(e); }
}

function doLogout() {
  G.logout(); G.clearFolderCache();
  S.timers.forEach(clearInterval); S.timers = [];
  Object.assign(S, { user: null, rootId: null, configId: null, files: [], archived: [], autoLocal: false });
  render();
  toast("Você saiu. O acesso do FileLife a esta sessão foi revogado.");
}

function startTimers() {
  S.timers.forEach(clearInterval);
  S.timers = [
    setInterval(() => { if (G.isLoggedIn()) runCleanup(false).catch(fail); }, 10 * 60 * 1000),
    setInterval(() => { if (S.autoLocal && G.isLoggedIn() && !S.busy) scanLocal(false).catch(fail); }, 15 * 1000),
  ];
}

function setupDropzone() {
  const zone = $("dropzone"), input = $("file-input");
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => { uploadFiles(input.files).catch(fail); input.value = ""; });
  for (const ev of ["dragenter", "dragover"]) zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); });
  for (const ev of ["dragleave", "drop"]) zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("over"); });
  zone.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files).catch(fail));
}

function boot() {
  // Impede que outro site carregue o FileLife escondido dentro de uma moldura (clickjacking)
  if (window.top !== window.self) { document.body.replaceChildren(); return; }

  const clientId = window.FILELIFE_CONFIG?.googleClientId;
  for (const b of document.querySelectorAll(".js-login")) b.addEventListener("click", doLogin);
  $("btn-settings").addEventListener("click", () => openSettings());
  $("btn-report").addEventListener("click", openReport);
  $("btn-cleanup").addEventListener("click", () => runCleanup(true).catch(fail));
  $("search").addEventListener("input", (e) => { S.search = e.target.value; renderFiles(); });
  for (const b of document.querySelectorAll("[data-tab]")) {
    b.addEventListener("click", () => { S.tab = b.dataset.tab; S.filter = "Todos"; render(); });
  }
  $("overlay").addEventListener("click", (e) => { if (e.target === $("overlay")) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  setupDropzone();
  window.addEventListener("beforeunload", (e) => { if (S.busy) { e.preventDefault(); e.returnValue = ""; } });

  if (!clientId || !/^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$/.test(clientId)) {
    $("setup-warning").hidden = false;
    for (const b of document.querySelectorAll(".js-login")) b.disabled = true;
  } else {
    const start = () => { try { G.initGoogle(clientId); } catch (e) { fail(e); } };
    if (window.google?.accounts?.oauth2) start(); else window.addEventListener("gis-loaded", start, { once: true });
  }
  if (supportsFolders) kv("localDir").then(d => { if (d) { S.localDir = d; render(); } }).catch(() => {});
  render();
}

boot();
