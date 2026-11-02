// Login com Google (Google Identity Services) e chamadas à API do Drive.
// O token de acesso fica só na memória desta aba: nunca é salvo nem enviado a outro servidor.

const SCOPE = "https://www.googleapis.com/auth/drive.file"; // só arquivos criados pelo FileLife
const API = "https://www.googleapis.com/drive/v3";
const UPLOAD = "https://www.googleapis.com/upload/drive/v3";
export const FOLDER_MIME = "application/vnd.google-apps.folder";

let tokenClient = null;
let token = null;          // { access_token, expires_at }
let pendingLogin = null;

export class AuthError extends Error {}

export function initGoogle(clientId) {
  if (!window.google?.accounts?.oauth2) throw new Error("Não foi possível carregar o login do Google.");
  tokenClient = google.accounts.oauth2.initTokenClient({
    client_id: clientId,
    scope: SCOPE,
    callback: (resp) => {
      const p = pendingLogin; pendingLogin = null;
      if (resp.error) { p?.reject(new Error(resp.error_description || resp.error)); return; }
      if (!google.accounts.oauth2.hasGrantedAllScopes(resp, SCOPE)) {
        p?.reject(new Error("Para funcionar, o FileLife precisa da permissão de acesso aos arquivos dele no Drive."));
        return;
      }
      token = { access_token: resp.access_token, expires_at: Date.now() + (resp.expires_in - 60) * 1000 };
      p?.resolve();
    },
    error_callback: (err) => {
      const p = pendingLogin; pendingLogin = null;
      p?.reject(new Error(err.type === "popup_closed" ? "Login cancelado." :
                          err.type === "popup_failed_to_open" ? "O navegador bloqueou a janela de login. Permita pop-ups para este site." :
                          "Falha no login do Google."));
    },
  });
}

/** Precisa ser chamado a partir de um clique (o navegador bloqueia pop-ups automáticos). */
export function login(prompt = "") {
  return new Promise((resolve, reject) => {
    pendingLogin = { resolve, reject };
    tokenClient.requestAccessToken({ prompt });
  });
}

export function isLoggedIn() { return !!token && Date.now() < token.expires_at; }

export function logout() {
  if (token) google.accounts.oauth2.revoke(token.access_token, () => {});
  token = null;
}

export async function api(path, { method = "GET", query, body, raw, headers = {}, base = API } = {}) {
  if (!isLoggedIn()) throw new AuthError("Sua sessão do Google expirou. Clique em “Entrar com Google”.");
  const url = new URL(base + path);
  for (const [k, v] of Object.entries(query || {})) if (v !== undefined) url.searchParams.set(k, v);
  const init = { method, headers: { Authorization: `Bearer ${token.access_token}`, ...headers } };
  if (body !== undefined) {
    if (body instanceof Blob || typeof body === "string") init.body = body;
    else { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json; charset=UTF-8"; }
  }
  let resp;
  for (let attempt = 0; ; attempt++) {
    resp = await fetch(url, init);
    if ((resp.status === 429 || resp.status >= 500) && attempt < 3) {
      await new Promise(r => setTimeout(r, 1000 * 2 ** attempt));
      continue;
    }
    break;
  }
  if (resp.status === 401) { token = null; throw new AuthError("Sua sessão do Google expirou. Clique em “Entrar com Google”."); }
  if (!resp.ok) {
    let msg = `Erro ${resp.status} do Google Drive`;
    try { msg = (await resp.json()).error?.message || msg; } catch {}
    const err = new Error(msg); err.status = resp.status; throw err;
  }
  if (raw) return resp;
  if (resp.status === 204) return null;
  return resp.json();
}

/** Escapa um texto para uso dentro de aspas simples numa busca do Drive. */
export const q = (s) => String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'");

export async function listAll(query, fields) {
  const out = [];
  let pageToken;
  do {
    const r = await api("/files", { query: { q: query, fields: `nextPageToken, files(${fields})`,
                                             pageSize: 1000, pageToken, spaces: "drive" } });
    out.push(...r.files);
    pageToken = r.nextPageToken;
  } while (pageToken);
  return out;
}

const folderCache = new Map();

export async function folder(name, parent = "root") {
  const key = parent + "/" + name;
  if (folderCache.has(key)) return folderCache.get(key);
  const found = await listAll(`name = '${q(name)}' and mimeType = '${FOLDER_MIME}' and '${q(parent)}' in parents and trashed = false`, "id");
  const id = found.length ? found[0].id :
    (await api("/files", { method: "POST", query: { fields: "id" },
                           body: { name, mimeType: FOLDER_MIME, parents: [parent] } })).id;
  folderCache.set(key, id);
  return id;
}

export function clearFolderCache() { folderCache.clear(); }

const FILE_FIELDS = "id,name,size,mimeType,webViewLink,appProperties,createdTime,modifiedTime,description,sha256Checksum,parents";
export { FILE_FIELDS };

/** Envio em partes (resumable): funciona para arquivos grandes e conexões instáveis. */
export async function upload(blob, metadata, onProgress) {
  const start = await api("/files", {
    base: UPLOAD, method: "POST", raw: true,
    query: { uploadType: "resumable", fields: FILE_FIELDS },
    headers: { "X-Upload-Content-Type": blob.type || "application/octet-stream",
               "X-Upload-Content-Length": String(blob.size) },
    body: metadata,
  });
  const session = start.headers.get("Location");
  if (!session || !session.startsWith("https://www.googleapis.com/")) throw new Error("O Google não iniciou o envio.");
  const CHUNK = 8 * 1024 * 1024; // múltiplo de 256 KB, como o Drive exige
  let offset = 0;
  if (blob.size === 0) {
    const r = await fetch(session, { method: "PUT", headers: { "Content-Range": "bytes */0" } });
    return r.json();
  }
  let failures = 0;
  const rangeEnd = (resp) => {
    const range = resp.headers.get("Range");      // ex.: "bytes=0-8388607"
    return range ? Number(range.split("-")[1]) + 1 : 0;
  };
  while (offset < blob.size) {
    const end = Math.min(offset + CHUNK, blob.size);
    let r = null;
    try {
      r = await fetch(session, { method: "PUT", body: blob.slice(offset, end),
                                 headers: { "Content-Range": `bytes ${offset}-${end - 1}/${blob.size}` } });
    } catch { /* queda de conexão: tenta retomar abaixo */ }
    if (r && r.status === 308) { offset = rangeEnd(r) || end; failures = 0; onProgress?.(offset / blob.size); continue; }
    if (r && r.ok) { onProgress?.(1); return r.json(); }
    if (r && r.status < 500 && r.status !== 429) throw new Error(`O Drive recusou o envio (erro ${r.status}).`);
    if (++failures > 5) throw new Error("A conexão caiu várias vezes durante o envio. Tente de novo.");
    await new Promise(res => setTimeout(res, 1000 * 2 ** failures));
    try {  // pergunta ao Drive até onde ele recebeu e continua dali
      const st = await fetch(session, { method: "PUT", headers: { "Content-Range": `bytes */${blob.size}` } });
      if (st.status === 308) offset = rangeEnd(st);
      else if (st.ok) { onProgress?.(1); return st.json(); }
    } catch { /* ainda sem conexão: o laço tenta de novo */ }
  }
  throw new Error("Envio incompleto.");
}

/** Cria/atualiza um arquivo JSON pequeno (configurações do FileLife). */
export async function saveJson(fileId, name, parent, data, appProperties) {
  const blob = new Blob([JSON.stringify(data)], { type: "application/json" });
  if (fileId) {
    return api(`/files/${encodeURIComponent(fileId)}`, { base: UPLOAD, method: "PATCH",
      query: { uploadType: "media", fields: "id" }, body: blob,
      headers: { "Content-Type": "application/json" } });
  }
  const created = await api("/files", { method: "POST", query: { fields: "id" },
    body: { name, parents: [parent], mimeType: "application/json", appProperties } });
  await api(`/files/${encodeURIComponent(created.id)}`, { base: UPLOAD, method: "PATCH",
    query: { uploadType: "media", fields: "id" }, body: blob, headers: { "Content-Type": "application/json" } });
  return created;
}

export async function download(fileId) {
  const r = await api(`/files/${encodeURIComponent(fileId)}`, { query: { alt: "media" }, raw: true });
  return r.blob();
}
