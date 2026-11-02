"""
FileLifecycle Pro — Sistema de Gestão de Ciclo de Vida de Arquivos.

Monitora a pasta Downloads em segundo plano, categoriza, renomeia, organiza,
deduplica, versiona, resume (IA), envia à nuvem e expira arquivos automaticamente.

Uso:
    python filelifecycle.py                     # monitora ~/Downloads
    python filelifecycle.py --organize-existing # também organiza o que já está lá
    python filelifecycle.py --watch PASTA       # monitora outra pasta
"""

import argparse
import collections
import datetime
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

APP_DIR = Path.home() / ".filelife"
DB_PATH = APP_DIR / "file_lifecycle.db"
LOG_DIR = APP_DIR / "logs"
VERSIONS_DIR = APP_DIR / "versions"
CONFIG_PATH = APP_DIR / "config.json"
ORGANIZED_DIR = Path.home() / "OrganizedFiles"
COLD_DIR = ORGANIZED_DIR / "_ArquivoFrio"
REPORTS_DIR = ORGANIZED_DIR / "_Relatorios"

DAY = 86400

DEFAULT_CONFIG = {
    # Prazo padrão por categoria (sem perguntar nada ao usuário)
    "default_expiry": {
        "Documentos": "1m",
        "Imagens": "1m",
        "Vídeos": "1w",
        "Áudio": "1m",
        "Arquivos Compactados": "1w",
        "Instaladores": "1d",
        "Outros": "1w",
    },
    # O que fazer quando expira: "archive" (zip no Arquivo Frio) ou "delete"
    "on_expire": "archive",
    # Dedupe: "keep_old" apaga o novo download repetido; "keep_new" substitui o antigo
    "dedupe": "keep_old",
    # Tags por palavra-chave no nome do arquivo (função 5)
    "tags": {
        "URGENTE": ["urgente", "boleto", "fatura", "vencimento", "prazo", "multa"],
        "LEITURA": ["relatorio", "relatório", "artigo", "ebook", "livro", "paper", "manual"],
        "CONTRATO": ["contrato", "proposta", "termo", "acordo"],
        "FINANCEIRO": ["nota fiscal", "nf-e", "nfe", "recibo", "extrato", "comprovante"],
    },
    # Versionamento (função 6): categorias versionadas e tamanho máximo em MB
    "versioning_categories": ["Documentos"],
    "versioning_max_mb": 50,
    "max_versions": 10,
    # Intervalos (segundos)
    "scan_interval": 3,
    # Pasta monitorada (vazio = Downloads)
    "watch_dir": "",
    "cleanup_interval": 3600,
    # Minutos economizados por arquivo organizado (usado no relatório)
    "minutes_saved_per_file": 1.5,
    # Função 2: resumo com IA (precisa de `pip install anthropic` + ANTHROPIC_API_KEY)
    "ai_summary": True,
    "ai_model": "claude-opus-5-5",
    "ai_categories": ["Documentos"],
    "ai_max_mb": 30,
    # Função 4: Cloud Bridge — manda arquivos grandes para a nuvem.
    # cloud_mode "api": envia direto ao Google Drive pela internet (sem instalar app;
    #   abre no navegador). "pasta": usa a pasta do app Drive/OneDrive/Dropbox.
    "cloud_bridge": True,
    "cloud_mode": "api",
    "drive_folder": "FileLife",
    # Modo "pasta": "google_drive" (padrão) ou "outro" (OneDrive/Dropbox primeiro).
    # cloud_dir vazio = detecta automaticamente (ex.: G:/Meu Drive).
    "cloud_provider": "google_drive",
    "cloud_dir": "",
    "cloud_min_mb": 200,
    "cloud_categories": ["Vídeos", "Arquivos Compactados"],
}

CATEGORIES = {
    "Documentos": {".pdf", ".doc", ".docx", ".txt", ".md", ".odt", ".rtf",
                   ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".epub"},
    "Imagens": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".heic", ".bmp"},
    "Vídeos": {".mp4", ".mov", ".avi", ".mkv", ".webm"},
    "Áudio": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
    "Arquivos Compactados": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Instaladores": {".exe", ".msi", ".dmg", ".pkg", ".deb", ".appimage"},
}

TEMP_SUFFIXES = (".crdownload", ".part", ".tmp", ".download", ".partial")

DB_LOCK = threading.RLock()
PRINT_LOCK = threading.Lock()


# Últimas mensagens, mostradas no feed de atividade da interface gráfica
ACTIVITY = collections.deque(maxlen=200)


def say(msg):
    with PRINT_LOCK:
        ACTIVITY.append({"ts": int(time.time()), "msg": msg})
        try:
            print(msg, flush=True)
        except (OSError, ValueError, AttributeError):  # sem console (.exe sem terminal)
            pass


def load_config():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(user)
        except (json.JSONDecodeError, OSError) as e:
            say(f"⚠️  config.json inválido ({e}), usando padrões.")
    else:
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    return cfg


CONFIG = DEFAULT_CONFIG


def save_config(changes):
    """Atualiza e grava a configuração (usado pela interface)."""
    CONFIG.update(changes)
    CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")


def watch_folder():
    return Path(CONFIG.get("watch_dir") or Path.home() / "Downloads").expanduser()


def start_background(organize_existing=False, folder=None):
    """Carrega config e banco, inicia o monitor e os workers. Retorna o Watcher."""
    global CONFIG
    CONFIG = load_config()
    init_db()
    folder = Path(folder).expanduser() if folder else watch_folder()
    folder.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=summary_worker, daemon=True).start()
    threading.Thread(target=upload_worker, daemon=True).start()
    watcher = Watcher(folder, organize_existing=organize_existing)
    watcher.start()
    return watcher


def log_action(message):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"lifecycle_{datetime.date.today()}.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with DB_LOCK, db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                original_name TEXT,
                new_name TEXT,
                category TEXT,
                tag TEXT,
                sha256 TEXT,
                size INTEGER DEFAULT 0,
                mtime REAL,
                expiry_timestamp INTEGER,      -- NULL = nunca expira
                status TEXT DEFAULT 'active',  -- active | archived | deleted | missing
                archive_path TEXT,
                archive_password TEXT,
                created_at INTEGER,
                closed_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
            CREATE INDEX IF NOT EXISTS idx_files_hash ON files(sha256);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER,
                kind TEXT,   -- organized | duplicate | archived | deleted | version
                file_id INTEGER,
                bytes INTEGER DEFAULT 0
            );
        """)
        for col in ("summary_path TEXT", "shortcut_path TEXT", "cloud INTEGER DEFAULT 0",
                    "drive_id TEXT", "drive_link TEXT"):
            try:
                conn.execute(f"ALTER TABLE files ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # coluna já existe


def add_event(conn, kind, file_id=None, nbytes=0):
    conn.execute("INSERT INTO events (ts, kind, file_id, bytes) VALUES (?, ?, ?, ?)",
                 (int(time.time()), kind, file_id, nbytes))


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def parse_duration(text):
    """'3d', '2w', '1m', '1y', 'never' -> segundos (None = nunca)."""
    text = text.strip().lower()
    if text in ("never", "nunca", "keep", "sempre"):
        return None
    m = re.fullmatch(r"(\d+)\s*([dwmy]?)", text)
    if not m:
        raise ValueError(f"Duração inválida: {text!r} (use 3d, 2w, 1m, 1y ou never)")
    n, unit = int(m.group(1)), m.group(2) or "d"
    return n * {"d": DAY, "w": 7 * DAY, "m": 30 * DAY, "y": 365 * DAY}[unit]


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_ts(ts):
    if ts is None:
        return "nunca"
    return datetime.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")


def sha256_of(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def categorize_file(filename):
    ext = Path(filename).suffix.lower()
    if filename.lower().endswith((".tar.gz", ".tar.bz2")):
        return "Arquivos Compactados"
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Outros"


def detect_tag(filename):
    """Função 5: tag de prioridade a partir de palavras-chave no nome."""
    name = filename.lower().replace("_", " ").replace("-", " ")
    for tag, words in CONFIG["tags"].items():
        if any(w in name for w in words):
            return tag
    return None


def unique_path(target):
    if not target.exists():
        return target
    stem, ext = target.stem, target.suffix
    i = 1
    while True:
        cand = target.with_name(f"{stem}_{i}{ext}")
        if not cand.exists():
            return cand
        i += 1


def build_new_name(filename, tag):
    name, ext = os.path.splitext(filename)
    clean = "".join(c if c.isalnum() or c in " ._-" else "_" for c in name).strip()[:50] or "arquivo"
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    prefix = f"[{tag}] " if tag else ""
    return f"{prefix}{stamp}_{clean}{ext.lower()}"


# ---------------------------------------------------------------------------
# Processamento de novos arquivos
# ---------------------------------------------------------------------------

def process_file(path):
    """Categoriza, deduplica, renomeia, move e registra um arquivo."""
    path = Path(path)
    if not path.is_file():
        return
    filename = path.name
    size = path.stat().st_size
    digest = sha256_of(path)

    # Função 1: Dedupe inteligente
    with DB_LOCK, db() as conn:
        dup = conn.execute(
            "SELECT * FROM files WHERE sha256 = ? AND status = 'active' ORDER BY id DESC",
            (digest,)).fetchone()
        if dup and (dup["drive_id"] or Path(dup["file_path"]).exists()):
            if CONFIG["dedupe"] == "keep_new" and not dup["drive_id"]:
                Path(dup["file_path"]).unlink(missing_ok=True)
                conn.execute("UPDATE files SET status='deleted', closed_at=? WHERE id=?",
                             (int(time.time()), dup["id"]))
                add_event(conn, "duplicate", dup["id"], dup["size"] or 0)
                log_action(f"Duplicata: removido antigo {dup['file_path']}, mantendo novo {filename}")
                say(f"♻️  {filename} é cópia de um arquivo antigo — versão antiga removida.")
            else:
                path.unlink()
                add_event(conn, "duplicate", dup["id"], size)
                log_action(f"Duplicata: {filename} = {dup['file_path']} (novo removido)")
                say(f"♻️  {filename} já existe em {dup['file_path']} — cópia removida "
                    f"({human_size(size)} economizados).")
                return

    category = categorize_file(filename)
    tag = detect_tag(filename)
    target_dir = ORGANIZED_DIR / category
    target_dir.mkdir(parents=True, exist_ok=True)
    new_path = unique_path(target_dir / build_new_name(filename, tag))

    try:
        shutil.move(str(path), str(new_path))
    except OSError as e:
        log_action(f"Erro ao mover {path}: {e}")
        say(f"❌ Não consegui mover {filename}: {e}")
        return

    secs = parse_duration(CONFIG["default_expiry"].get(category, "1w"))
    now = int(time.time())
    expiry = now + secs if secs else None

    with DB_LOCK, db() as conn:
        cur = conn.execute("""
            INSERT INTO files (file_path, original_name, new_name, category, tag, sha256,
                               size, mtime, expiry_timestamp, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (str(new_path), filename, new_path.name, category, tag, digest, size,
             new_path.stat().st_mtime, expiry, now))
        file_id = cur.lastrowid
        add_event(conn, "organized", file_id, size)

    snapshot_version(file_id, new_path, category)
    cloud_path = maybe_send_to_cloud(file_id, new_path, category, size)
    if cloud_path is None and category in CONFIG["ai_categories"]:
        enqueue_summary(file_id)
    log_action(f"Organizado #{file_id}: {filename} -> {new_path} (expira {fmt_ts(expiry)})")
    tag_txt = f" [{tag}]" if tag else ""
    say(f"📥 #{file_id} {filename} → {category}{tag_txt} · expira {fmt_ts(expiry)} "
        f"(use 'extend {file_id} 3m' ou 'keep {file_id}')")


# ---------------------------------------------------------------------------
# Função 6: Versionamento
# ---------------------------------------------------------------------------

def should_version(category, size):
    return (category in CONFIG["versioning_categories"]
            and size <= CONFIG["versioning_max_mb"] * 1024 * 1024)


def snapshot_version(file_id, path, category):
    path = Path(path)
    if not path.exists() or not should_version(category, path.stat().st_size):
        return None
    vdir = VERSIONS_DIR / str(file_id)
    vdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest = unique_path(vdir / f"{stamp}{path.suffix}")
    shutil.copy2(path, dest)
    versions = sorted(vdir.iterdir())
    for old in versions[:-CONFIG["max_versions"]]:
        old.unlink(missing_ok=True)
    return dest


def check_versions():
    """Detecta arquivos organizados que foram editados e guarda a nova versão."""
    with DB_LOCK, db() as conn:
        rows = conn.execute("SELECT id, file_path, category, mtime FROM files "
                            "WHERE status='active'").fetchall()
    for r in rows:
        p = Path(r["file_path"])
        if not p.exists():
            continue
        mtime = p.stat().st_mtime
        if r["mtime"] is not None and mtime > r["mtime"] + 1:
            if snapshot_version(r["id"], p, r["category"]):
                log_action(f"Nova versão salva de #{r['id']} ({p.name})")
                with DB_LOCK, db() as conn:
                    add_event(conn, "version", r["id"])
            with DB_LOCK, db() as conn:
                conn.execute("UPDATE files SET mtime=?, size=? WHERE id=?",
                             (mtime, p.stat().st_size, r["id"]))


def list_versions(file_id):
    vdir = VERSIONS_DIR / str(file_id)
    return sorted(vdir.iterdir()) if vdir.exists() else []


def restore_version(file_id, index):
    with DB_LOCK, db() as conn:
        row = conn.execute("SELECT file_path FROM files WHERE id=?", (file_id,)).fetchone()
    versions = list_versions(file_id)
    if not row or not versions:
        raise ValueError("Sem versões para esse arquivo.")
    if not 1 <= index <= len(versions):
        raise ValueError(f"Escolha uma versão entre 1 e {len(versions)}.")
    target = Path(row["file_path"])
    if target.exists():
        snapshot_version(file_id, target, "Documentos")  # guarda o estado atual antes
    shutil.copy2(versions[index - 1], target)
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE files SET mtime=? WHERE id=?", (target.stat().st_mtime, file_id))
    return target


# ---------------------------------------------------------------------------
# Expiração + Função 3: Arquivo Frio
# ---------------------------------------------------------------------------

def archive_file(path, extras=(), cold_dir=None):
    """Compacta em zip no Arquivo Frio. Usa senha AES se `pyzipper` estiver instalado."""
    extras = [Path(e) for e in extras if e and Path(e).exists()]
    month_dir = (cold_dir or COLD_DIR) / datetime.date.today().strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    zip_path = unique_path(month_dir / f"{path.stem}.zip")
    password = None
    try:
        import pyzipper  # opcional
        password = secrets.token_urlsafe(12)
        with pyzipper.AESZipFile(zip_path, "w", compression=pyzipper.ZIP_DEFLATED,
                                 encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(password.encode())
            for f in [path, *extras]:
                zf.write(f, f.name)
    except ImportError:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in [path, *extras]:
                zf.write(f, f.name)
    for f in [path, *extras]:
        f.unlink()
    return zip_path, password


def run_cleanup(verbose=True):
    now = int(time.time())
    with DB_LOCK, db() as conn:
        expired = conn.execute(
            "SELECT * FROM files WHERE status='active' AND expiry_timestamp IS NOT NULL "
            "AND expiry_timestamp <= ?", (now,)).fetchall()
    if not expired:
        if verbose:
            say("🧹 Nenhum arquivo expirado.")
        return 0

    for r in expired:
        if r["drive_id"]:
            expire_drive_file(r, now)
            continue
        p = Path(r["file_path"])
        if not p.exists():
            remove_shortcut(r["shortcut_path"])
            with DB_LOCK, db() as conn:
                conn.execute("UPDATE files SET status='missing', closed_at=? WHERE id=?",
                             (now, r["id"]))
            continue
        size = p.stat().st_size
        remove_shortcut(r["shortcut_path"])
        try:
            if CONFIG["on_expire"] == "archive":
                # Arquivos na nuvem continuam na nuvem (não ocupam o SSD de novo)
                cold = p.parent.parent / "_ArquivoFrio" if r["cloud"] else None
                zip_path, pwd = archive_file(p, [r["summary_path"]], cold)
                with DB_LOCK, db() as conn:
                    conn.execute("UPDATE files SET status='archived', archive_path=?, "
                                 "archive_password=?, closed_at=? WHERE id=?",
                                 (str(zip_path), pwd, now, r["id"]))
                    add_event(conn, "archived", r["id"], max(size - zip_path.stat().st_size, 0))
                log_action(f"Arquivado #{r['id']} {p.name} -> {zip_path}")
                say(f"🧊 #{r['id']} {p.name} expirou → Arquivo Frio ({zip_path.name})")
            else:
                p.unlink()
                if r["summary_path"]:
                    Path(r["summary_path"]).unlink(missing_ok=True)
                with DB_LOCK, db() as conn:
                    conn.execute("UPDATE files SET status='deleted', closed_at=? WHERE id=?",
                                 (now, r["id"]))
                    add_event(conn, "deleted", r["id"], size)
                log_action(f"Deletado #{r['id']} {p}")
                say(f"🗑️  #{r['id']} {p.name} expirou e foi removido")
        except OSError as e:
            log_action(f"Erro ao expirar #{r['id']} {p}: {e}")
            say(f"❌ Erro ao expirar {p.name}: {e}")
    return len(expired)


# ---------------------------------------------------------------------------
# Função 2: Resumo de Conteúdo com IA (Claude)
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = (
    "Resuma este documento em exatamente 3 tópicos curtos, em português do Brasil, "
    "no formato '- texto'. Foque no que importa para quem baixou o arquivo: do que "
    "se trata, os pontos principais e qualquer prazo, valor ou ação necessária. "
    "Responda apenas com os 3 tópicos."
)
TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".xml", ".rtf"}
MAX_TEXT_CHARS = 600_000  # ~150k tokens; acima disso o arquivo é pulado (não truncado)

SUMMARY_QUEUE = queue.Queue()
_ai_warned = set()


def _warn_once(key, msg):
    if key not in _ai_warned:
        _ai_warned.add(key)
        say(msg)


def extract_docx_text(path):
    from xml.etree import ElementTree
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as zf:
        root = ElementTree.fromstring(zf.read("word/document.xml"))
    paragraphs = []
    for p in root.iter(f"{ns}p"):
        text = "".join(t.text or "" for t in p.iter(f"{ns}t"))
        if text.strip():
            paragraphs.append(text)
    return "\n".join(paragraphs)


def build_document_block(path):
    """Converte o arquivo num bloco de conteúdo para a API, ou None se não suportado."""
    import base64
    ext = path.suffix.lower()
    if ext == ".pdf":
        data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        return {"type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    if ext == ".docx":
        text = extract_docx_text(path)
    elif ext in TEXT_EXTS:
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        return None
    if not text.strip():
        return None
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("documento grande demais para resumir de uma vez")
    return {"type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": text},
            "title": path.name}


def summarize_file(file_id):
    """Gera <arquivo>.resumo.md ao lado do arquivo. Retorna o caminho ou None."""
    with DB_LOCK, db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not row or row["status"] != "active":
        return None
    path = Path(row["file_path"])
    if not path.exists() or path.stat().st_size > CONFIG["ai_max_mb"] * 1024 * 1024:
        return None
    try:
        import anthropic
    except ImportError:
        _warn_once("sdk", "💡 Resumo com IA desativado: rode `pip install anthropic` "
                          "e defina ANTHROPIC_API_KEY para ativar.")
        return None
    try:
        block = build_document_block(path)
    except (ValueError, OSError, zipfile.BadZipFile, KeyError) as e:
        log_action(f"Resumo #{file_id} pulado: {e}")
        return None
    if block is None:
        return None

    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=CONFIG["ai_model"],
            max_tokens=16000,
            output_config={"effort": "low"},
            # Se o modelo recusar por política, a API tenta outro modelo automaticamente
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": [block, {"type": "text", "text": SUMMARY_PROMPT}]}],
        )
    except anthropic.AuthenticationError:
        _warn_once("auth", "🔑 Resumo com IA: chave da API inválida ou ausente "
                           "(defina ANTHROPIC_API_KEY).")
        return None
    except anthropic.RateLimitError:
        log_action(f"Resumo #{file_id}: limite de uso atingido, tente 'summary {file_id}' depois")
        return None
    except anthropic.APIStatusError as e:
        log_action(f"Resumo #{file_id}: erro da API {e.status_code}: {e.message}")
        return None
    except anthropic.APIConnectionError:
        log_action(f"Resumo #{file_id}: sem conexão")
        return None
    except anthropic.AnthropicError as e:  # ex.: nenhuma credencial configurada
        _warn_once("auth", f"🔑 Resumo com IA indisponível: {e}")
        return None

    if response.stop_reason == "refusal":
        log_action(f"Resumo #{file_id}: modelo recusou o conteúdo")
        return None
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        return None

    out = path.with_name(f"{path.stem}.resumo.md")
    out.write_text(f"# Resumo: {row['original_name']}\n\n{text}\n", encoding="utf-8")
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE files SET summary_path=? WHERE id=?", (str(out), file_id))
    log_action(f"Resumo gerado #{file_id}: {out}")
    say(f"🧠 #{file_id} resumido:\n{text}")
    return out


def enqueue_summary(file_id):
    if CONFIG["ai_summary"]:
        SUMMARY_QUEUE.put(file_id)


def summary_worker():
    """Roda os resumos numa thread separada para não travar o monitor."""
    while True:
        file_id = SUMMARY_QUEUE.get()
        try:
            summarize_file(file_id)
        except Exception as e:
            log_action(f"Erro no resumo #{file_id}: {e!r}")


# ---------------------------------------------------------------------------
# Função 4: Cloud Bridge
# ---------------------------------------------------------------------------

def _google_drive_candidates():
    home = Path.home()
    out = []
    if sys.platform == "win32":
        # Google Drive para computador monta uma unidade (geralmente G:)
        for letter in "GHIJKLMNOPQRSTUVWXYZDEF":
            out += [Path(f"{letter}:/My Drive"), Path(f"{letter}:/Meu Drive")]
    cloud_storage = home / "Library" / "CloudStorage"  # macOS
    if cloud_storage.is_dir():
        for d in sorted(cloud_storage.iterdir()):
            if d.name.startswith("GoogleDrive"):
                out += [d / "My Drive", d / "Meu Drive"]
    # Modo "espelhar arquivos" ou instalações antigas
    out += [home / "My Drive", home / "Meu Drive",
            home / "Google Drive" / "My Drive", home / "Google Drive" / "Meu Drive",
            home / "Google Drive"]
    return out


def _other_cloud_candidates():
    home = Path.home()
    out = [Path(os.environ[v]) for v in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
           if os.environ.get(v)]
    cloud_storage = home / "Library" / "CloudStorage"
    if cloud_storage.is_dir():
        out += [d for d in sorted(cloud_storage.iterdir())
                if d.name.startswith(("OneDrive", "Dropbox"))]
    return out + [home / "OneDrive", home / "Dropbox"]


def detect_cloud_dir():
    """Procura a pasta da nuvem: Google Drive primeiro, depois OneDrive/Dropbox."""
    if CONFIG.get("cloud_dir"):
        p = Path(CONFIG["cloud_dir"]).expanduser()
        return p if p.is_dir() else None
    candidates = _google_drive_candidates()
    if CONFIG.get("cloud_provider", "google_drive") != "google_drive":
        candidates = _other_cloud_candidates() + candidates
    else:
        candidates += _other_cloud_candidates()
    for c in candidates:
        try:
            if c.is_dir():
                return c
        except OSError:  # unidade de disco inexistente/sem mídia no Windows
            continue
    return None


def create_shortcut(link_path, target):
    """Atalho local apontando para o arquivo na nuvem. Retorna o caminho do atalho."""
    target = Path(target)
    if sys.platform == "win32":
        # .url funciona sem permissão de administrador (symlink no Windows exige)
        link = unique_path(Path(link_path).with_name(Path(link_path).name + ".url"))
        link.write_text(f"[InternetShortcut]\nURL={target.as_uri()}\n", encoding="utf-8")
        return link
    link = unique_path(Path(link_path))
    os.symlink(target, link)
    return link


def remove_shortcut(shortcut_path):
    if shortcut_path:
        p = Path(shortcut_path)
        if p.is_symlink() or p.exists():
            p.unlink()


def send_to_cloud_folder(file_id, path):
    """Modo "pasta": move para a pasta sincronizada e deixa um atalho no lugar."""
    cloud_root = detect_cloud_dir()
    if cloud_root is None:
        raise ValueError("Nenhuma pasta do Google Drive/OneDrive/Dropbox encontrada. "
                         "Defina 'cloud_dir' no config.json.")
    path = Path(path)
    dest_dir = cloud_root / "FileLife" / path.parent.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_path(dest_dir / path.name)
    shutil.move(str(path), str(dest))
    shortcut = create_shortcut(path, dest)
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE files SET file_path=?, shortcut_path=?, cloud=1, mtime=? WHERE id=?",
                     (str(dest), str(shortcut), dest.stat().st_mtime, file_id))
    log_action(f"Cloud Bridge #{file_id}: {path} -> {dest} (atalho {shortcut})")
    return dest


# --- Modo "api": Google Drive direto pela internet --------------------------

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]  # só arquivos criados pelo app
DRIVE_CREDENTIALS = APP_DIR / "google_credentials.json"
DRIVE_TOKEN = APP_DIR / "google_token.json"
FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_QUEUE = queue.Queue()


def _drive_libs():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        raise ValueError("instale as bibliotecas do Google: "
                         "pip install google-api-python-client google-auth-oauthlib")
    return Request, Credentials, InstalledAppFlow, build, MediaFileUpload


def drive_login():
    """Abre o navegador para você autorizar o FileLife no seu Google Drive (uma vez)."""
    _, _, InstalledAppFlow, _, _ = _drive_libs()
    if not DRIVE_CREDENTIALS.exists():
        raise ValueError(f"coloque o arquivo de credenciais do Google em {DRIVE_CREDENTIALS} "
                         "(veja o README, seção Google Drive)")
    flow = InstalledAppFlow.from_client_secrets_file(str(DRIVE_CREDENTIALS), DRIVE_SCOPES)
    creds = flow.run_local_server(port=0)
    DRIVE_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def drive_service():
    Request, Credentials, _, build, _ = _drive_libs()
    if not DRIVE_TOKEN.exists():
        raise ValueError("Google Drive não conectado — digite 'drive login'")
    creds = Credentials.from_authorized_user_file(str(DRIVE_TOKEN), DRIVE_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            DRIVE_TOKEN.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise ValueError("login do Google Drive expirou — digite 'drive login'")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def drive_account():
    """Conta Google conectada: {'name', 'email', 'photo'} ou None."""
    if not DRIVE_TOKEN.exists():
        return None
    service = drive_service()
    user = service.about().get(fields="user(displayName,emailAddress,photoLink)").execute()["user"]
    return {"name": user.get("displayName", ""), "email": user.get("emailAddress", ""),
            "photo": user.get("photoLink", "")}


def drive_logout():
    """Desconecta a conta Google (revoga o acesso e apaga o login salvo)."""
    if not DRIVE_TOKEN.exists():
        return
    try:
        token = json.loads(DRIVE_TOKEN.read_text(encoding="utf-8")).get("token")
        if token:
            import urllib.parse
            import urllib.request
            data = urllib.parse.urlencode({"token": token}).encode()
            urllib.request.urlopen("https://oauth2.googleapis.com/revoke", data=data, timeout=10)
    except Exception as e:
        log_action(f"Falha ao revogar token do Google: {e!r}")
    DRIVE_TOKEN.unlink(missing_ok=True)
    _folder_cache.clear()


_folder_cache = {}


def drive_folder(service, name, parent="root"):
    """Acha (ou cria) uma pasta no Drive e devolve o ID."""
    key = (parent, name)
    if key in _folder_cache:
        return _folder_cache[key]
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    found = service.files().list(
        q=f"name = '{safe}' and mimeType = '{FOLDER_MIME}' and '{parent}' in parents "
          "and trashed = false",
        spaces="drive", fields="files(id)").execute().get("files", [])
    if found:
        folder_id = found[0]["id"]
    else:
        folder_id = service.files().create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
            fields="id").execute()["id"]
    _folder_cache[key] = folder_id
    return folder_id


def create_web_shortcut(link_path, url):
    """Atalho que abre o arquivo no navegador (drive.google.com)."""
    link_path = Path(link_path)
    if sys.platform == "darwin":
        link = unique_path(link_path.with_name(link_path.name + ".webloc"))
        link.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        f'<plist version="1.0"><dict><key>URL</key><string>{url}</string>'
                        '</dict></plist>\n', encoding="utf-8")
    else:
        link = unique_path(link_path.with_name(link_path.name + ".url"))
        link.write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-8")
    return link


def upload_to_drive(file_id, path):
    """Envia o arquivo ao Google Drive, apaga a cópia local e deixa um atalho web."""
    _, _, _, _, MediaFileUpload = _drive_libs()
    path = Path(path)
    service = drive_service()
    root = drive_folder(service, CONFIG["drive_folder"])
    parent = drive_folder(service, path.parent.name, root)
    media = MediaFileUpload(str(path), resumable=True, chunksize=16 * 1024 * 1024)
    request = service.files().create(body={"name": path.name, "parents": [parent]},
                                     media_body=media, fields="id, webViewLink")
    result = None
    while result is None:
        _, result = request.next_chunk()
    media.stream().close()  # libera o arquivo (necessário no Windows antes de apagar)
    size = path.stat().st_size
    path.unlink()
    shortcut = create_web_shortcut(path, result["webViewLink"])
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE files SET file_path=?, shortcut_path=?, cloud=1, drive_id=?, "
                     "drive_link=? WHERE id=?",
                     (f"gdrive://{CONFIG['drive_folder']}/{path.parent.name}/{path.name}",
                      str(shortcut), result["id"], result["webViewLink"], file_id))
    log_action(f"Google Drive #{file_id}: {path.name} -> {result['webViewLink']}")
    say(f"☁️  #{file_id} {path.name} ({human_size(size)}) enviado ao Google Drive — "
        f"abra com 'open {file_id}'")
    return result["webViewLink"]


def expire_drive_file(row, now):
    """Expiração de arquivo que está no Drive: move para _ArquivoFrio ou manda pra lixeira."""
    try:
        service = drive_service()
        if CONFIG["on_expire"] == "archive":
            root = drive_folder(service, CONFIG["drive_folder"])
            cold = drive_folder(service, "_ArquivoFrio", root)
            parents = service.files().get(fileId=row["drive_id"], fields="parents").execute()
            service.files().update(fileId=row["drive_id"], addParents=cold,
                                   removeParents=",".join(parents.get("parents", [])),
                                   fields="id").execute()
            status, kind, msg = "archived", "archived", "movido para _ArquivoFrio no Drive"
        else:
            service.files().update(fileId=row["drive_id"], body={"trashed": True}).execute()
            status, kind, msg = "deleted", "deleted", "enviado para a lixeira do Drive"
    except ValueError as e:
        _warn_once("drive", f"☁️  Google Drive: {e}")
        return
    except Exception as e:  # erro de rede/API: tenta de novo na próxima limpeza
        log_action(f"Expirar #{row['id']} no Drive falhou: {e!r}")
        return
    remove_shortcut(row["shortcut_path"])
    if row["summary_path"]:
        Path(row["summary_path"]).unlink(missing_ok=True)
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE files SET status=?, closed_at=? WHERE id=?", (status, now, row["id"]))
        add_event(conn, kind, row["id"], 0)
    log_action(f"#{row['id']} expirou: {msg}")
    say(f"🧊 #{row['id']} {row['new_name']} expirou → {msg}")


def upload_worker():
    """Uploads rodam numa thread própria (vídeos grandes demoram)."""
    while True:
        file_id, path = UPLOAD_QUEUE.get()
        try:
            upload_to_drive(file_id, path)
        except ValueError as e:
            _warn_once("drive", f"☁️  Google Drive: {e}")
        except Exception as e:
            log_action(f"Upload #{file_id} falhou: {e!r}")
            say(f"❌ Upload de #{file_id} para o Drive falhou (o arquivo continua no PC): {e}")


# --- Escolha do modo ---------------------------------------------------------

def send_to_cloud(file_id, path):
    if CONFIG.get("cloud_mode", "api") == "api":
        return upload_to_drive(file_id, path)
    return send_to_cloud_folder(file_id, path)


def maybe_send_to_cloud(file_id, path, category, size):
    if not CONFIG["cloud_bridge"] or category not in CONFIG["cloud_categories"]:
        return None
    if size < CONFIG["cloud_min_mb"] * 1024 * 1024:
        return None
    if CONFIG.get("cloud_mode", "api") == "api":
        UPLOAD_QUEUE.put((file_id, path))
        say(f"☁️  #{file_id} na fila de envio para o Google Drive...")
        return "queued"
    try:
        dest = send_to_cloud_folder(file_id, path)
    except ValueError as e:
        _warn_once("cloud", f"☁️  Cloud Bridge: {e}")
        return None
    except OSError as e:
        log_action(f"Cloud Bridge #{file_id} falhou: {e}")
        return None
    say(f"☁️  #{file_id} ({human_size(size)}) enviado para {dest.parent} — atalho local criado.")
    return dest


# ---------------------------------------------------------------------------
# Função 7: Relatório de Produtividade
# ---------------------------------------------------------------------------

def generate_report(days=7):
    since = int(time.time()) - days * DAY
    with DB_LOCK, db() as conn:
        ev = {row["kind"]: (row["n"], row["b"] or 0) for row in conn.execute(
            "SELECT kind, COUNT(*) n, SUM(bytes) b FROM events WHERE ts >= ? GROUP BY kind",
            (since,))}
        cats = conn.execute(
            "SELECT category, COUNT(*) n, SUM(size) b FROM files WHERE created_at >= ? "
            "GROUP BY category ORDER BY n DESC", (since,)).fetchall()
        active = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files "
                              "WHERE status='active'").fetchone()

    organized = ev.get("organized", (0, 0))[0]
    dup_n, dup_b = ev.get("duplicate", (0, 0))
    arc_n, arc_b = ev.get("archived", (0, 0))
    del_n, del_b = ev.get("deleted", (0, 0))
    ver_n = ev.get("version", (0, 0))[0]
    freed = dup_b + arc_b + del_b
    minutes = organized * CONFIG["minutes_saved_per_file"]

    start = datetime.date.today() - datetime.timedelta(days=days)
    rows = "".join(f"<tr><td>{c['category']}</td><td>{c['n']}</td>"
                   f"<td>{human_size(c['b'] or 0)}</td></tr>" for c in cats) \
        or "<tr><td colspan=3>Nenhum arquivo no período</td></tr>"

    def card(value, label):
        return f'<div class="card"><b>{value}</b><span>{label}</span></div>'

    html = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Relatório FileLife</title><style>
body{{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#1d1d1f}}
h1{{margin-bottom:0}} .sub{{color:#666;margin-top:4px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:24px 0}}
.card{{border:1px solid #ddd;border-radius:12px;padding:16px}}
.card b{{display:block;font-size:28px}} .card span{{color:#666;font-size:14px}}
table{{width:100%;border-collapse:collapse}} td,th{{text-align:left;padding:8px;border-bottom:1px solid #eee}}
@media print{{body{{margin:0}}}}
</style></head><body>
<h1>Relatório de Produtividade</h1>
<p class="sub">{start:%d/%m/%Y} a {datetime.date.today():%d/%m/%Y} · gerado pelo FileLifecycle Pro</p>
<div class="grid">
{card(organized, "arquivos organizados")}
{card(f"{minutes / 60:.1f} h", "de organização economizadas")}
{card(human_size(freed), "de lixo digital liberado")}
{card(dup_n, "duplicatas eliminadas")}
{card(arc_n + del_n, "arquivos expirados")}
{card(ver_n, "versões salvas")}
</div>
<h2>Por categoria</h2>
<table><tr><th>Categoria</th><th>Arquivos</th><th>Tamanho</th></tr>{rows}</table>
<p class="sub">Monitorando agora: {active[0]} arquivos ativos ({human_size(active[1])}).
Para salvar em PDF: abra no navegador → Imprimir → Salvar como PDF.</p>
</body></html>"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"relatorio_{datetime.date.today()}.html"
    out.write_text(html, encoding="utf-8")
    log_action(f"Relatório gerado: {out}")
    return out


def weekly_report_due():
    today = datetime.date.today()
    return today.weekday() == 6 and not (REPORTS_DIR / f"relatorio_{today}.html").exists()


# ---------------------------------------------------------------------------
# Monitor em segundo plano
# ---------------------------------------------------------------------------

class Watcher(threading.Thread):
    """Varre a pasta periodicamente e só processa arquivos com tamanho estável
    (evita pegar downloads ainda em andamento)."""

    def __init__(self, folder, organize_existing=False):
        super().__init__(daemon=True)
        self.folder = Path(folder)
        self.stop_event = threading.Event()
        self.pending = {}  # path -> (size, mtime)
        self.ignored = set() if organize_existing else set(self._list())
        self.last_cleanup = 0
        self.jobs = queue.Queue()

    def _list(self):
        try:
            return {p for p in self.folder.iterdir()
                    if p.is_file() and not p.name.startswith(".")
                    and not p.name.lower().endswith(TEMP_SUFFIXES)}
        except OSError:
            return set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as e:  # nunca deixa o monitor morrer
                log_action(f"Erro no monitor: {e!r}")
            self.stop_event.wait(CONFIG["scan_interval"])

    def tick(self):
        current = self._list()
        self.ignored &= current
        for p in current - self.ignored:
            try:
                st = p.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime)
            if self.pending.get(p) == sig and st.st_size > 0:
                self.pending.pop(p, None)
                process_file(p)
            else:
                self.pending[p] = sig
        for p in list(self.pending):
            if p not in current:
                self.pending.pop(p)

        now = time.time()
        if now - self.last_cleanup >= CONFIG["cleanup_interval"]:
            self.last_cleanup = now
            run_cleanup(verbose=False)
            check_versions()
            if weekly_report_due():
                out = generate_report()
                say(f"📊 Relatório semanal pronto: {out}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

HELP = """Comandos:
  status                 resumo e próximas expirações
  list [categoria]       lista arquivos ativos
  extend <id> <prazo>    novo prazo a partir de agora (3d, 2w, 1m, 1y)
  keep <id>              nunca expirar
  expire <id>            expira agora
  versions <id>          lista versões salvas
  restore <id> <n>       restaura a versão n
  archive <id>           mostra onde está o zip e a senha
  summary <id>           gera/regera o resumo com IA
  cloud <id>             envia para a nuvem e deixa um atalho
  open <id>              abre o arquivo (no navegador, se estiver no Drive)
  drive login            conecta sua conta do Google Drive (abre o navegador)
  cleanup                processa expirados agora
  report [dias]          gera relatório (padrão 7 dias)
  help | quit"""


def cmd_status():
    with DB_LOCK, db() as conn:
        n, total = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files "
                                "WHERE status='active'").fetchone()
        upcoming = conn.execute(
            "SELECT id, new_name, expiry_timestamp FROM files WHERE status='active' "
            "AND expiry_timestamp IS NOT NULL ORDER BY expiry_timestamp LIMIT 5").fetchall()
    say(f"\n--- STATUS ---\nArquivos ativos: {n} ({human_size(total)})")
    if upcoming:
        say("Próximas expirações:")
        for r in upcoming:
            say(f"  #{r['id']:<4} {fmt_ts(r['expiry_timestamp'])}  {r['new_name']}")
    say("--------------\n")


def cmd_list(category=None):
    sql = "SELECT * FROM files WHERE status='active'"
    args = ()
    if category:
        sql += " AND category LIKE ?"
        args = (f"%{category}%",)
    with DB_LOCK, db() as conn:
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    if not rows:
        say("Nenhum arquivo ativo.")
    for r in rows:
        say(f"  #{r['id']:<4} {r['category']:<20} {human_size(r['size'] or 0):>9}  "
            f"expira {fmt_ts(r['expiry_timestamp']):<16}  {r['new_name']}")


def set_expiry(file_id, seconds):
    expiry = int(time.time()) + seconds if seconds is not None else None
    with DB_LOCK, db() as conn:
        cur = conn.execute("UPDATE files SET expiry_timestamp=? WHERE id=? AND status='active'",
                           (expiry, file_id))
    if cur.rowcount == 0:
        raise ValueError(f"Arquivo #{file_id} não encontrado ou não está ativo.")
    return expiry


def handle_command(line):
    parts = line.split()
    if not parts:
        return True
    cmd, args = parts[0].lower(), parts[1:]
    try:
        if cmd in ("quit", "exit", "sair"):
            return False
        elif cmd == "help":
            say(HELP)
        elif cmd == "status":
            cmd_status()
        elif cmd == "list":
            cmd_list(" ".join(args) or None)
        elif cmd == "extend" and len(args) == 2:
            exp = set_expiry(int(args[0]), parse_duration(args[1]))
            say(f"⏳ #{args[0]} agora expira em {fmt_ts(exp)}")
        elif cmd == "keep" and len(args) == 1:
            set_expiry(int(args[0]), None)
            say(f"📌 #{args[0]} nunca vai expirar")
        elif cmd == "expire" and len(args) == 1:
            set_expiry(int(args[0]), 0)
            run_cleanup()
        elif cmd == "versions" and len(args) == 1:
            vs = list_versions(int(args[0]))
            if not vs:
                say("Sem versões salvas.")
            for i, v in enumerate(vs, 1):
                say(f"  {i}. {v.name}  ({human_size(v.stat().st_size)})")
        elif cmd == "restore" and len(args) == 2:
            target = restore_version(int(args[0]), int(args[1]))
            say(f"⏪ Versão {args[1]} restaurada em {target}")
        elif cmd == "archive" and len(args) == 1:
            with DB_LOCK, db() as conn:
                r = conn.execute("SELECT archive_path, archive_password FROM files WHERE id=?",
                                 (int(args[0]),)).fetchone()
            if not r or not r["archive_path"]:
                say("Esse arquivo não está no Arquivo Frio.")
            else:
                say(f"🧊 {r['archive_path']}\n   senha: {r['archive_password'] or '(sem senha)'}")
        elif cmd == "summary" and len(args) == 1:
            say("🧠 Resumindo...")
            if not summarize_file(int(args[0])):
                say("Não foi possível resumir (formato não suportado, IA desativada ou erro — veja o log).")
        elif cmd == "drive" and args[:1] == ["login"]:
            say("🌐 Abrindo o navegador para você autorizar o FileLife no Google Drive...")
            drive_login()
            say("✅ Google Drive conectado!")
        elif cmd == "open" and len(args) == 1:
            import webbrowser
            with DB_LOCK, db() as conn:
                r = conn.execute("SELECT file_path, drive_link FROM files WHERE id=?",
                                 (int(args[0]),)).fetchone()
            if not r:
                say("Arquivo não encontrado.")
            else:
                webbrowser.open(r["drive_link"] or Path(r["file_path"]).as_uri())
        elif cmd == "cloud" and len(args) == 1:
            with DB_LOCK, db() as conn:
                r = conn.execute("SELECT file_path, cloud FROM files WHERE id=? AND status='active'",
                                 (int(args[0]),)).fetchone()
            if not r:
                say("Arquivo não encontrado.")
            elif r["cloud"]:
                say(f"☁️  Já está na nuvem: {r['file_path']}")
            else:
                say("☁️  Enviando...")
                dest = send_to_cloud(int(args[0]), r["file_path"])
                say(f"☁️  Enviado: {dest}")
        elif cmd == "cleanup":
            run_cleanup()
        elif cmd == "report":
            out = generate_report(int(args[0]) if args else 7)
            say(f"📊 Relatório gerado: {out}")
        else:
            say(HELP)
    except ValueError as e:
        say(f"⚠️  {e}")
    except Exception as e:  # erro de rede/API não derruba o programa
        log_action(f"Erro no comando {line!r}: {e!r}")
        say(f"❌ Erro: {e}")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="FileLifecycle Pro")
    parser.add_argument("--watch", default=None,
                        help="pasta monitorada (padrão: watch_dir do config ou ~/Downloads)")
    parser.add_argument("--organize-existing", action="store_true",
                        help="organiza também os arquivos que já estão na pasta")
    args = parser.parse_args(argv)

    watcher = start_background(args.organize_existing, args.watch)
    folder = watcher.folder
    if CONFIG["cloud_bridge"] and CONFIG.get("cloud_mode", "api") == "api":
        if DRIVE_TOKEN.exists():
            say("☁️  Google Drive conectado: arquivos grandes vão para o Drive.")
        else:
            say("☁️  Para mandar arquivos grandes ao Google Drive, digite 'drive login'.")
    elif CONFIG["cloud_bridge"]:
        cloud = detect_cloud_dir()
        if cloud:
            say(f"☁️  Cloud Bridge ativo: arquivos grandes vão para {cloud / 'FileLife'}")
    say(f"👀 Monitorando {folder} em segundo plano.")
    say(f"   Configurações em {CONFIG_PATH}. Digite 'help' para ver os comandos.\n")

    try:
        while True:
            try:
                line = input()
            except EOFError:  # rodando sem terminal (ex.: serviço) — só monitora
                watcher.stop_event.wait()
                break
            if not handle_command(line.strip()):
                break
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop_event.set()
        say("👋 Encerrando o monitor...")


if __name__ == "__main__":
    sys.exit(main())
