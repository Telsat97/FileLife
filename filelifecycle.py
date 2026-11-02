"""
FileLifecycle Pro — Sistema de Gestão de Ciclo de Vida de Arquivos.

Monitora a pasta Downloads em segundo plano, categoriza, renomeia, organiza,
deduplica, versiona e expira arquivos automaticamente.

Uso:
    python filelifecycle.py                     # monitora ~/Downloads
    python filelifecycle.py --organize-existing # também organiza o que já está lá
    python filelifecycle.py --watch PASTA       # monitora outra pasta
"""

import argparse
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
    "cleanup_interval": 3600,
    # Minutos que o "minuto" economizado vale por arquivo organizado (relatório)
    "minutes_saved_per_file": 1.5,
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


def say(msg):
    with PRINT_LOCK:
        print(msg, flush=True)


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
        if dup and Path(dup["file_path"]).exists():
            if CONFIG["dedupe"] == "keep_new":
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

def archive_file(path):
    """Compacta em zip no Arquivo Frio. Usa senha AES se `pyzipper` estiver instalado."""
    COLD_DIR.mkdir(parents=True, exist_ok=True)
    month_dir = COLD_DIR / datetime.date.today().strftime("%Y-%m")
    month_dir.mkdir(exist_ok=True)
    zip_path = unique_path(month_dir / f"{path.stem}.zip")
    password = None
    try:
        import pyzipper  # opcional
        password = secrets.token_urlsafe(12)
        with pyzipper.AESZipFile(zip_path, "w", compression=pyzipper.ZIP_DEFLATED,
                                 encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(password.encode())
            zf.write(path, path.name)
    except ImportError:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(path, path.name)
    path.unlink()
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
        p = Path(r["file_path"])
        if not p.exists():
            with DB_LOCK, db() as conn:
                conn.execute("UPDATE files SET status='missing', closed_at=? WHERE id=?",
                             (now, r["id"]))
            continue
        size = p.stat().st_size
        try:
            if CONFIG["on_expire"] == "archive":
                zip_path, pwd = archive_file(p)
                with DB_LOCK, db() as conn:
                    conn.execute("UPDATE files SET status='archived', archive_path=?, "
                                 "archive_password=?, closed_at=? WHERE id=?",
                                 (str(zip_path), pwd, now, r["id"]))
                    add_event(conn, "archived", r["id"], max(size - zip_path.stat().st_size, 0))
                log_action(f"Arquivado #{r['id']} {p.name} -> {zip_path}")
                say(f"🧊 #{r['id']} {p.name} expirou → Arquivo Frio ({zip_path.name})")
            else:
                p.unlink()
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
        elif cmd == "cleanup":
            run_cleanup()
        elif cmd == "report":
            out = generate_report(int(args[0]) if args else 7)
            say(f"📊 Relatório gerado: {out}")
        else:
            say(HELP)
    except ValueError as e:
        say(f"⚠️  {e}")
    return True


def main(argv=None):
    global CONFIG
    parser = argparse.ArgumentParser(description="FileLifecycle Pro")
    parser.add_argument("--watch", default=str(Path.home() / "Downloads"),
                        help="pasta monitorada (padrão: ~/Downloads)")
    parser.add_argument("--organize-existing", action="store_true",
                        help="organiza também os arquivos que já estão na pasta")
    args = parser.parse_args(argv)

    CONFIG = load_config()
    init_db()
    folder = Path(args.watch).expanduser()
    folder.mkdir(parents=True, exist_ok=True)

    watcher = Watcher(folder, organize_existing=args.organize_existing)
    watcher.start()
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
