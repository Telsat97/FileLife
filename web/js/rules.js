// Regras do FileLife: categorias, tags, prazos e nomes. Funções puras (sem rede).

export const DAY = 86400 * 1000;

export const CATEGORIES = {
  "Documentos": ["pdf", "doc", "docx", "txt", "md", "odt", "rtf", "xls", "xlsx", "csv", "ppt", "pptx", "epub"],
  "Imagens": ["jpg", "jpeg", "png", "gif", "webp", "svg", "heic", "bmp"],
  "Vídeos": ["mp4", "mov", "avi", "mkv", "webm"],
  "Áudio": ["mp3", "wav", "flac", "m4a", "ogg"],
  "Arquivos Compactados": ["zip", "rar", "7z", "tar", "gz"],
  "Instaladores": ["exe", "msi", "dmg", "pkg", "deb", "appimage"],
};
export const ALL_CATEGORIES = [...Object.keys(CATEGORIES), "Outros"];
export const ICONS = { "Documentos": "📄", "Imagens": "🖼️", "Vídeos": "🎬", "Áudio": "🎵",
  "Arquivos Compactados": "🗜️", "Instaladores": "💿", "Outros": "📦" };

export const DEFAULT_SETTINGS = {
  default_expiry: { "Documentos": "1m", "Imagens": "1m", "Vídeos": "1w", "Áudio": "1m",
                    "Arquivos Compactados": "1w", "Instaladores": "1d", "Outros": "1w" },
  on_expire: "archive",            // "archive" (pasta _ArquivoFrio) ou "trash" (lixeira do Drive)
  remove_local_after_upload: true, // só depois de conferir a impressão digital no Drive
  tags: {
    "URGENTE": ["urgente", "boleto", "fatura", "vencimento", "prazo", "multa"],
    "LEITURA": ["relatorio", "relatório", "artigo", "ebook", "livro", "paper", "manual"],
    "CONTRATO": ["contrato", "proposta", "termo", "acordo"],
    "FINANCEIRO": ["nota fiscal", "nf-e", "nfe", "recibo", "extrato", "comprovante"],
  },
  stats: { duplicates: 0, duplicate_bytes: 0, expired: 0, local_freed_bytes: 0, uploaded: 0 },
};

/** Valida configurações vindas do Drive (podem estar corrompidas) e completa com os padrões. */
export function sanitizeSettings(raw) {
  const s = structuredClone(DEFAULT_SETTINGS);
  if (!raw || typeof raw !== "object") return s;
  if (raw.default_expiry && typeof raw.default_expiry === "object") {
    for (const cat of ALL_CATEGORIES) {
      const v = raw.default_expiry[cat];
      if (typeof v === "string" && parseDuration(v) !== undefined) s.default_expiry[cat] = v;
    }
  }
  if (raw.on_expire === "trash" || raw.on_expire === "archive") s.on_expire = raw.on_expire;
  if (typeof raw.remove_local_after_upload === "boolean") s.remove_local_after_upload = raw.remove_local_after_upload;
  if (raw.tags && typeof raw.tags === "object" &&
      Object.values(raw.tags).every(w => Array.isArray(w) && w.every(x => typeof x === "string"))) {
    s.tags = raw.tags;
  }
  if (raw.stats && typeof raw.stats === "object") {
    for (const k of Object.keys(s.stats)) {
      const v = Number(raw.stats[k]);
      if (Number.isFinite(v) && v >= 0) s.stats[k] = v;
    }
  }
  return s;
}

/** "3d", "2w", "1m", "1y" -> milissegundos; "never" -> null; inválido -> undefined. */
export function parseDuration(text) {
  const t = String(text).trim().toLowerCase();
  if (["never", "nunca", "sempre"].includes(t)) return null;
  const m = /^(\d{1,4})([dwmy]?)$/.exec(t);
  if (!m) return undefined;
  return Number(m[1]) * { d: 1, w: 7, m: 30, y: 365 }[m[2] || "d"] * DAY;
}

export function extOf(name) {
  const lower = name.toLowerCase();
  if (lower.endsWith(".tar.gz") || lower.endsWith(".tar.bz2")) return "tar";
  const i = lower.lastIndexOf(".");
  return i > 0 ? lower.slice(i + 1) : "";
}

export function categorize(name) {
  const ext = extOf(name);
  for (const [cat, exts] of Object.entries(CATEGORIES)) if (exts.includes(ext)) return cat;
  return "Outros";
}

export function detectTag(name, tags) {
  const n = name.toLowerCase().replace(/[_-]/g, " ");
  for (const [tag, words] of Object.entries(tags)) if (words.some(w => n.includes(w))) return tag;
  return null;
}

const pad = (n) => String(n).padStart(2, "0");

/** "[URGENTE] 2026-11-02_14-30-00_Boleto Luz.pdf" — sem caracteres problemáticos. */
export function buildName(original, tag, date = new Date()) {
  const dot = original.lastIndexOf(".");
  const base = dot > 0 ? original.slice(0, dot) : original;
  const ext = dot > 0 ? original.slice(dot).toLowerCase().replace(/[^.a-z0-9]/g, "") : "";
  const clean = base.normalize("NFC").replace(/[^\p{L}\p{N} ._-]/gu, "_").trim().slice(0, 50) || "arquivo";
  const stamp = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}_` +
                `${pad(date.getHours())}-${pad(date.getMinutes())}-${pad(date.getSeconds())}`;
  return `${tag ? `[${tag}] ` : ""}${stamp}_${clean}${ext}`;
}

const SYSTEM_FILES = new Set(["desktop.ini", "thumbs.db", ".ds_store"]);
const TEMP_EXT = ["crdownload", "part", "tmp", "download", "partial", "opdownload"];

/** Arquivos que nunca devem ser tocados (temporários, ocultos, de sistema, travas do Office). */
export function shouldIgnore(name) {
  const n = name.toLowerCase();
  return n.startsWith(".") || n.startsWith("~$") || n.startsWith("~lock.") ||
         SYSTEM_FILES.has(n) || TEMP_EXT.includes(extOf(n));
}

/** Corta um texto para caber nas propriedades do Drive (124 bytes por chave+valor). */
export function fitProperty(key, value) {
  const enc = new TextEncoder();
  let v = String(value);
  while (enc.encode(key + v).length > 120) v = v.slice(0, -1);
  return v;
}

export function humanSize(n) {
  n = Number(n) || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${n} B` : `${n.toFixed(1)} ${units[i]}`;
}

export function fmtDate(ms) {
  if (ms == null) return "nunca";
  return new Date(ms).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric",
                                                hour: "2-digit", minute: "2-digit" });
}
