"""
FileLife para PC.

Quando um download termina, pergunta:
  1. Manter no PC ou mandar para o Google Drive?
  2. Por quanto tempo esse arquivo vai existir?
Quando o prazo acaba, o arquivo é apagado sozinho: no PC vai para a Lixeira,
no Drive vai para a lixeira do Drive (as duas permitem recuperar).

    python filelife_desktop.py
"""

import contextlib
import json
import os
import queue
import sqlite3
import sys
import threading
import time
from pathlib import Path

import filelifecycle as fl

fl.ENTRY_SCRIPT = "filelife_desktop.py"

DB_PATH = fl.APP_DIR / "desktop.db"
SETTINGS_PATH = fl.APP_DIR / "desktop.json"
DRIVE_ROOT = "FileLife"
SCAN_SECONDS = 2
STABLE_SECONDS = 3          # arquivo parado há este tempo = download terminou
EXPIRY_CHECK_SECONDS = 300  # confere prazos a cada 5 minutos

DURATIONS = [("1 dia", "1d"), ("3 dias", "3d"), ("1 semana", "1w"), ("1 mês", "1m"),
             ("3 meses", "3m"), ("1 ano", "1y"), ("Para sempre", "never")]

DEFAULT_SETTINGS = {"watch_dir": "", "min_size_mb": 0}


# ---------------------------------------------------------------------------
# Configurações e banco local
# ---------------------------------------------------------------------------

def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            if isinstance(raw.get("watch_dir"), str):
                s["watch_dir"] = raw["watch_dir"]
            if isinstance(raw.get("min_size_mb"), (int, float)) and 0 <= raw["min_size_mb"] <= 100_000:
                s["min_size_mb"] = raw["min_size_mb"]
    except (OSError, ValueError):
        pass
    if s["watch_dir"] and fl.unsafe_watch_reason(Path(s["watch_dir"])):
        s["watch_dir"] = ""
    return s


def save_settings(s):
    fl.write_private(SETTINGS_PATH, json.dumps(s, ensure_ascii=False, indent=2))


def watch_dir(settings):
    return Path(settings["watch_dir"] or Path.home() / "Downloads").expanduser()


@contextlib.contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


DB_LOCK = threading.RLock()


def init_db():
    fl.make_private_dir(fl.APP_DIR)
    with DB_LOCK, db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT,                 -- caminho no PC (se location = 'pc')
                location TEXT NOT NULL,    -- 'pc' ou 'drive'
                drive_id TEXT,
                drive_link TEXT,
                size INTEGER DEFAULT 0,
                sha256 TEXT,
                category TEXT,
                expiry INTEGER,            -- segundos (epoch); NULL = para sempre
                status TEXT DEFAULT 'active',  -- active | deleted | missing
                created INTEGER,
                closed INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
            CREATE TABLE IF NOT EXISTS seen (sig TEXT PRIMARY KEY, ts INTEGER);
        """)
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE items ADD COLUMN sig TEXT")
    if os.name != "nt":
        os.chmod(DB_PATH, 0o600)


def find_renamed(row):
    """Se o usuário renomeou o arquivo na mesma pasta, acha pelo tamanho + data exata."""
    folder = Path(row["path"]).parent
    if not row["sig"] or not folder.is_dir():
        return None
    for p in folder.iterdir():
        with contextlib.suppress(OSError):
            if p.is_file() and not p.is_symlink() and file_signature(p) == row["sig"]:
                return p
    return None


def file_signature(p):
    """Identifica o arquivo mesmo se ele for renomeado (tamanho + data exata)."""
    st = p.stat()
    return f"{st.st_size}|{st.st_mtime_ns}"


def mark_seen(sig):
    with DB_LOCK, db() as conn:
        conn.execute("INSERT OR REPLACE INTO seen (sig, ts) VALUES (?, ?)", (sig, int(time.time())))
        conn.execute("DELETE FROM seen WHERE ts < ?", (int(time.time()) - 90 * 86400,))


def was_seen(sig):
    with DB_LOCK, db() as conn:
        return conn.execute("SELECT 1 FROM seen WHERE sig=?", (sig,)).fetchone() is not None


def expiry_from(code):
    secs = fl.parse_duration(code)
    return None if secs is None else int(time.time()) + secs


def describe_expiry(expiry):
    return "nunca" if expiry is None else fl.fmt_ts(expiry)


# ---------------------------------------------------------------------------
# Ações (sem interface: dá para testar sozinhas)
# ---------------------------------------------------------------------------

def keep_on_pc(path, code):
    """Deixa o arquivo onde está e agenda a remoção."""
    path = Path(path)
    if not path.is_file():
        raise ValueError("O arquivo não existe mais.")
    expiry = expiry_from(code)
    sig = file_signature(path)
    with DB_LOCK, db() as conn:
        cur = conn.execute(
            "INSERT INTO items (name, path, location, size, category, expiry, created, sig) "
            "VALUES (?, ?, 'pc', ?, ?, ?, ?, ?)",
            (path.name, str(path), path.stat().st_size, fl.categorize_file(path.name), expiry,
             int(time.time()), sig))
    mark_seen(sig)
    fl.log_action(f"[PC] {path} mantido; apaga em {describe_expiry(expiry)}")
    return cur.lastrowid, expiry


def send_to_drive(path, code, progress=None):
    """Envia ao Drive, confere a impressão digital e só então apaga do PC."""
    _, _, _, _, MediaFileUpload = fl._drive_libs()
    path = Path(path)
    if not path.is_file():
        raise ValueError("O arquivo não existe mais.")
    st = path.stat()
    size, mtime = st.st_size, st.st_mtime
    digest = fl.sha256_of(path)
    expiry = expiry_from(code)
    category = fl.categorize_file(path.name)
    tag = fl.detect_tag(path.name) if fl.CONFIG.get("tags") else None

    service = fl.drive_service()
    root = fl.drive_folder(service, DRIVE_ROOT)
    parent = fl.drive_folder(service, category, root)
    now_ms = int(time.time() * 1000)
    props = {
        "fl_app": "1", "fl_status": "active", "fl_cat": category, "fl_tag": tag or "",
        "fl_exp": "never" if expiry is None else str(expiry * 1000), "fl_sha": digest,
        "fl_created": str(now_ms), "fl_orig": _fit("fl_orig", path.name), "fl_src": "pc",
    }
    media = MediaFileUpload(str(path), resumable=True, chunksize=8 * 1024 * 1024)
    try:
        request = service.files().create(
            body={"name": path.name, "parents": [parent], "appProperties": props},
            media_body=media, fields="id, webViewLink, sha256Checksum, size")
        result = None
        while result is None:
            status, result = request.next_chunk()
            if status and progress:
                progress(status.progress())
    finally:
        media.stream().close()  # libera o arquivo (Windows) antes de apagar

    remote_sha = result.get("sha256Checksum")
    for _ in range(3):
        if remote_sha:
            break
        time.sleep(1.5)
        remote_sha = service.files().get(fileId=result["id"], fields="sha256Checksum").execute().get("sha256Checksum")
    if remote_sha != digest or int(result.get("size", -1)) != size:
        with contextlib.suppress(Exception):
            service.files().update(fileId=result["id"], body={"trashed": True}).execute()
        raise OSError("O arquivo chegou diferente no Drive. A cópia do PC foi mantida; tente de novo.")

    sig = file_signature(path)
    st2 = path.stat()
    removed = st2.st_size == size and st2.st_mtime == mtime
    if removed:
        path.unlink()  # seguro: o Drive confirmou o mesmo conteúdo
    with DB_LOCK, db() as conn:
        cur = conn.execute(
            "INSERT INTO items (name, path, location, drive_id, drive_link, size, sha256, category, "
            "expiry, created) VALUES (?, ?, 'drive', ?, ?, ?, ?, ?, ?, ?)",
            (path.name, None if removed else str(path), result["id"], result.get("webViewLink"),
             size, digest, category, expiry, int(time.time())))
    mark_seen(sig)
    fl.log_action(f"[Drive] {path.name} enviado ({result['id']}); apaga em {describe_expiry(expiry)}")
    return cur.lastrowid, expiry, removed


def _fit(key, value):
    v = str(value)
    while len((key + v).encode("utf-8")) > 120:
        v = v[:-1]
    return v


def set_expiry(item_id, code):
    expiry = expiry_from(code)
    with DB_LOCK, db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id=? AND status='active'", (item_id,)).fetchone()
        if not row:
            raise ValueError("Arquivo não encontrado.")
        conn.execute("UPDATE items SET expiry=? WHERE id=?", (expiry, item_id))
    if row["location"] == "drive" and row["drive_id"]:
        with contextlib.suppress(Exception):  # o prazo vale mesmo se o Drive estiver fora do ar
            fl.drive_service().files().update(
                fileId=row["drive_id"],
                body={"appProperties": {"fl_exp": "never" if expiry is None else str(expiry * 1000)}}).execute()
    return expiry


def delete_item(row):
    """Apaga um item: PC → Lixeira; Drive → lixeira do Drive. Retorna o novo status."""
    if row["location"] == "pc":
        p = Path(row["path"])
        if not p.exists():
            p = find_renamed(row)
            if p is None:
                return "missing"  # o usuário moveu ou apagou: nada a fazer
        fl.safe_delete(p)
        return "deleted"
    try:
        fl.drive_service().files().update(fileId=row["drive_id"], body={"trashed": True}).execute()
        return "deleted"
    except Exception as e:
        if getattr(getattr(e, "resp", None), "status", None) == 404:
            return "missing"  # o usuário já apagou no Drive
        raise


def delete_now(item_id):
    with DB_LOCK, db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id=? AND status='active'", (item_id,)).fetchone()
    if not row:
        raise ValueError("Arquivo não encontrado.")
    status = delete_item(row)
    with DB_LOCK, db() as conn:
        conn.execute("UPDATE items SET status=?, closed=? WHERE id=?", (status, int(time.time()), item_id))
    return status


def process_expired():
    """Apaga o que venceu. Retorna (apagados, falhas)."""
    now = int(time.time())
    with DB_LOCK, db() as conn:
        rows = conn.execute("SELECT * FROM items WHERE status='active' AND expiry IS NOT NULL "
                            "AND expiry <= ?", (now,)).fetchall()
    deleted, failed = [], []
    for row in rows:
        try:
            status = delete_item(row)
        except Exception as e:  # sem internet, sem login: tenta de novo depois
            fl.log_action(f"Falha ao expirar #{row['id']} {row['name']}: {e!r}")
            failed.append(row["name"])
            continue
        with DB_LOCK, db() as conn:
            conn.execute("UPDATE items SET status=?, closed=? WHERE id=?", (status, now, row["id"]))
        fl.log_action(f"Expirou #{row['id']} {row['name']} ({row['location']}) → {status}")
        if status == "deleted":
            deleted.append(row)
    return deleted, failed


def active_items():
    with DB_LOCK, db() as conn:
        return conn.execute("SELECT * FROM items WHERE status='active' "
                            "ORDER BY COALESCE(expiry, 9e18), id DESC").fetchall()


# ---------------------------------------------------------------------------
# Vigia de downloads
# ---------------------------------------------------------------------------

class DownloadWatcher(threading.Thread):
    """Detecta downloads que terminaram (arquivo parado há alguns segundos)."""

    def __init__(self, folder, on_new, min_size=0):
        super().__init__(daemon=True)
        self.folder = Path(folder)
        self.on_new = on_new
        self.min_size = min_size
        self.stop_event = threading.Event()
        self.pending = {}
        self.announced = set()
        state = fl.load_state().get("desktop_last_run", {})
        last = state.get(str(self.folder))
        # Primeira vez: ignora o que já estava na pasta. Depois: pergunta também
        # sobre o que chegou enquanto o programa estava fechado.
        self.baseline = {p for p in self._list() if last is None or p.stat().st_mtime < last}
        self._mark()

    def _mark(self):
        state = fl.load_state().get("desktop_last_run", {})
        state[str(self.folder)] = time.time()
        with contextlib.suppress(OSError):
            fl.save_state(desktop_last_run=state)

    def _list(self):
        try:
            return {p for p in self.folder.iterdir() if not fl.should_ignore(p)}
        except OSError:
            return set()

    def run(self):
        last_mark = time.time()
        while not self.stop_event.wait(SCAN_SECONDS):
            try:
                self.tick()
            except Exception as e:
                fl.log_action(f"Erro no vigia de downloads: {e!r}")
            if time.time() - last_mark > 60:
                self._mark()
                last_mark = time.time()

    def tick(self):
        now = time.time()
        current = self._list()
        self.baseline &= current
        for p in current - self.baseline:
            try:
                st = p.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime)
            if self.pending.get(p) != sig or now - st.st_mtime < STABLE_SECONDS or st.st_size == 0:
                self.pending[p] = sig
                continue
            key = file_signature(p)
            if key in self.announced or was_seen(key) or st.st_size < self.min_size:
                continue
            self.announced.add(key)
            self.on_new(p)
        for p in list(self.pending):
            if p not in current:
                self.pending.pop(p)

    def stop(self):
        self._mark()
        self.stop_event.set()


# ---------------------------------------------------------------------------
# Interface (Tkinter: já vem com o Python, sem instalar nada)
# ---------------------------------------------------------------------------

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    if not fl.acquire_single_instance():
        _ping_running_instance()
        return

    fl.CONFIG = fl.load_config()
    init_db()
    settings = load_settings()
    ui_queue = queue.Queue()
    root = tk.Tk()
    root.withdraw()
    root.title("FileLife")
    with contextlib.suppress(Exception):
        root.iconphoto(True, tk.PhotoImage(data=ICON_PNG))

    style = ttk.Style(root)
    with contextlib.suppress(tk.TclError):
        style.theme_use("vista" if os.name == "nt" else "clam")
    BG, FG, MUTED, ACCENT = "#ffffff", "#16181d", "#667085", "#3b5bfd"
    FONT = ("Segoe UI", 10) if os.name == "nt" else ("Helvetica", 10)
    BOLD = (FONT[0], 11, "bold")

    windows = []  # janelas de pergunta abertas (para empilhar)
    state = {"watcher": None, "list_win": None, "paused": False}

    def drive_connected():
        return fl.DRIVE_TOKEN.exists()

    def place(win, index):
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = sw - w - 24
        y = max(10, sh - h - 70 - index * 40)
        win.geometry(f"+{x}+{y}")

    def toast(text, seconds=4):
        t = tk.Toplevel(root, bg=FG)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        tk.Label(t, text=text, bg=FG, fg="white", font=FONT, padx=14, pady=10, justify="left",
                 wraplength=360).pack()
        place(t, len(windows))
        t.after(seconds * 1000, t.destroy)

    # ---------------- Janela de pergunta ----------------
    def ask(path):
        if state["paused"] or not Path(path).exists():
            return
        win = tk.Toplevel(root, bg=BG, padx=18, pady=14)
        win.title("FileLife — novo download")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        windows.append(win)
        size = fl.human_size(Path(path).stat().st_size)
        icon = {"Documentos": "📄", "Imagens": "🖼️", "Vídeos": "🎬", "Áudio": "🎵",
                "Arquivos Compactados": "🗜️", "Instaladores": "💿"}.get(fl.categorize_file(path.name), "📦")

        def close():
            if win in windows:
                windows.remove(win)
            win.destroy()

        def ignore():
            mark_seen(file_signature(path)) if path.exists() else None
            close()

        win.protocol("WM_DELETE_WINDOW", ignore)
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both")

        def clear():
            for w in body.winfo_children():
                w.destroy()

        def header():
            tk.Label(body, text=f"{icon}  Download concluído", bg=BG, fg=MUTED, font=FONT).pack(anchor="w")
            tk.Label(body, text=path.name, bg=BG, fg=FG, font=BOLD, wraplength=340,
                     justify="left").pack(anchor="w", pady=(2, 0))
            tk.Label(body, text=size, bg=BG, fg=MUTED, font=FONT).pack(anchor="w", pady=(0, 10))

        def step_where():
            clear(); header()
            tk.Label(body, text="Onde guardar?", bg=BG, fg=FG, font=BOLD).pack(anchor="w", pady=(0, 6))
            row = tk.Frame(body, bg=BG); row.pack(fill="x")
            ttk.Button(row, text="💻  Manter no PC", command=lambda: step_time("pc")).pack(side="left", expand=True, fill="x", padx=(0, 4))
            b = ttk.Button(row, text="☁️  Mandar pro Drive", command=lambda: step_time("drive"))
            b.pack(side="left", expand=True, fill="x", padx=(4, 0))
            if not drive_connected():
                b.state(["disabled"])
                link = tk.Label(body, text="Conectar Google Drive", bg=BG, fg=ACCENT, cursor="hand2", font=FONT)
                link.pack(anchor="w", pady=(6, 0))
                link.bind("<Button-1>", lambda e: connect_drive(after=step_where))
            ign = tk.Label(body, text="Ignorar este arquivo", bg=BG, fg=MUTED, cursor="hand2", font=(FONT[0], 9))
            ign.pack(anchor="e", pady=(8, 0))
            ign.bind("<Button-1>", lambda e: ignore())
            place(win, windows.index(win))

        def step_time(where):
            clear(); header()
            dest = "no PC" if where == "pc" else "no Google Drive"
            tk.Label(body, text=f"Por quanto tempo ele fica {dest}?", bg=BG, fg=FG, font=BOLD).pack(anchor="w")
            tk.Label(body, text="Depois disso ele é apagado sozinho (vai para a lixeira).",
                     bg=BG, fg=MUTED, font=(FONT[0], 9)).pack(anchor="w", pady=(0, 8))
            grid = tk.Frame(body, bg=BG); grid.pack(fill="x")
            for i, (label, code) in enumerate(DURATIONS):
                ttk.Button(grid, text=label, command=lambda c=code: finish(where, c)).grid(
                    row=i // 3, column=i % 3, sticky="ew", padx=2, pady=2)
            for c in range(3):
                grid.columnconfigure(c, weight=1)
            back = tk.Label(body, text="← Voltar", bg=BG, fg=ACCENT, cursor="hand2", font=FONT)
            back.pack(anchor="w", pady=(8, 0))
            back.bind("<Button-1>", lambda e: step_where())
            place(win, windows.index(win))

        def finish(where, code):
            if not path.exists():
                clear(); tk.Label(body, text="O arquivo não existe mais.", bg=BG, fg=FG, font=FONT).pack()
                win.after(2500, close); return
            if where == "pc":
                try:
                    _, expiry = keep_on_pc(path, code)
                except Exception as e:
                    messagebox.showerror("FileLife", str(e), parent=win); return
                close()
                toast(f"💻 {path.name}\nFica no PC até {describe_expiry(expiry)}." if expiry
                      else f"💻 {path.name}\nFica no PC para sempre.")
                refresh_list()
                return
            clear(); header()
            msg = tk.Label(body, text="Enviando para o Google Drive…", bg=BG, fg=FG, font=FONT)
            msg.pack(anchor="w")
            bar = ttk.Progressbar(body, length=340, maximum=100)
            bar.pack(fill="x", pady=8)
            win.protocol("WM_DELETE_WINDOW", lambda: None)  # não fecha no meio do envio

            def work():
                try:
                    _, expiry, removed = send_to_drive(
                        path, code, progress=lambda p: ui_queue.put(lambda: bar.configure(value=p * 100)))
                    def ok():
                        close()
                        extra = "" if removed else "\n(o arquivo mudou durante o envio, então a cópia do PC foi mantida)"
                        toast(f"☁️ {path.name} está no Drive.\n"
                              + (f"Será apagado em {describe_expiry(expiry)}." if expiry else "Fica lá para sempre.")
                              + extra)
                        refresh_list()
                    ui_queue.put(ok)
                except Exception as e:
                    fl.log_action(f"Envio de {path} falhou: {e!r}")
                    def err(e=e):
                        win.protocol("WM_DELETE_WINDOW", close)
                        clear(); header()
                        text = str(e) if isinstance(e, (ValueError, OSError)) else "Falha ao enviar. Verifique a internet."
                        tk.Label(body, text=f"⚠️ {text}\nO arquivo continua no PC.", bg=BG, fg=FG,
                                 font=FONT, wraplength=340, justify="left").pack(anchor="w")
                        row = tk.Frame(body, bg=BG); row.pack(fill="x", pady=(8, 0))
                        ttk.Button(row, text="Tentar de novo", command=lambda: finish(where, code)).pack(side="left")
                        ttk.Button(row, text="Escolher de novo", command=step_where).pack(side="left", padx=6)
                    ui_queue.put(err)
            threading.Thread(target=work, daemon=True).start()

        step_where()
        win.lift()
        win.focus_force()

    # ---------------- Lista de arquivos ----------------
    def open_list():
        if state["list_win"] and state["list_win"].winfo_exists():
            state["list_win"].deiconify(); state["list_win"].lift(); refresh_list(); return
        win = tk.Toplevel(root, padx=12, pady=12)
        win.title("FileLife — meus arquivos")
        win.geometry("760x460")
        state["list_win"] = win
        top = ttk.Frame(win); top.pack(fill="x", pady=(0, 8))
        state["status_lbl"] = ttk.Label(top, text="")
        state["status_lbl"].pack(side="left")
        cols = ("nome", "onde", "expira", "tamanho")
        tree = ttk.Treeview(win, columns=cols, show="headings", selectmode="browse")
        for c, label, w in (("nome", "Arquivo", 340), ("onde", "Onde", 80), ("expira", "Apaga em", 150),
                            ("tamanho", "Tamanho", 90)):
            tree.heading(c, text=label); tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True)
        state["tree"] = tree
        bar = ttk.Frame(win); bar.pack(fill="x", pady=(8, 0))

        def selected():
            sel = tree.selection()
            return int(sel[0]) if sel else None

        def do_open():
            item_id = selected()
            if item_id is None:
                return
            with DB_LOCK, db() as conn:
                r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            import webbrowser
            if r["location"] == "drive" and r["drive_link"] and r["drive_link"].startswith("https://"):
                webbrowser.open(r["drive_link"])
            elif r["path"] and Path(r["path"]).exists():
                p = Path(r["path"])
                # Programas baixados não são executados daqui: mostra a pasta
                webbrowser.open((p.parent if p.suffix.lower() in fl.RISKY_EXTS else p).as_uri())

        def do_extend(code):
            item_id = selected()
            if item_id is None:
                return
            try:
                expiry = set_expiry(item_id, code)
                toast(f"Novo prazo: {describe_expiry(expiry)}")
            except Exception as e:
                messagebox.showerror("FileLife", str(e), parent=win)
            refresh_list()

        def do_delete():
            item_id = selected()
            if item_id is None:
                return
            if not messagebox.askyesno("FileLife", "Apagar agora? Ele vai para a lixeira (dá para recuperar).", parent=win):
                return
            try:
                delete_now(item_id)
            except Exception as e:
                messagebox.showerror("FileLife", f"Não consegui apagar: {e}", parent=win)
            refresh_list()

        ttk.Button(bar, text="Abrir", command=do_open).pack(side="left")
        mb = ttk.Menubutton(bar, text="Mudar prazo ▾")
        menu = tk.Menu(mb, tearoff=False)
        for label, code in DURATIONS:
            menu.add_command(label=label, command=lambda c=code: do_extend(c))
        mb["menu"] = menu
        mb.pack(side="left", padx=6)
        ttk.Button(bar, text="Apagar agora", command=do_delete).pack(side="left")
        ttk.Button(bar, text="Opções…", command=open_options).pack(side="right")
        tree.bind("<Double-1>", lambda e: do_open())
        refresh_list()

    def refresh_list():
        tree = state.get("tree")
        if not tree or not tree.winfo_exists():
            return
        tree.delete(*tree.get_children())
        rows = active_items()
        for r in rows:
            tree.insert("", "end", iid=str(r["id"]), values=(
                r["name"], "☁️ Drive" if r["location"] == "drive" else "💻 PC",
                describe_expiry(r["expiry"]), fl.human_size(r["size"] or 0)))
        drive = "conectado" if drive_connected() else "não conectado"
        state["status_lbl"].configure(
            text=f"{len(rows)} arquivo(s) · vigiando {watch_dir(settings)} · Google Drive {drive}"
                 + (" · PAUSADO" if state["paused"] else ""))

    # ---------------- Opções ----------------
    def open_options():
        win = tk.Toplevel(root, padx=16, pady=14)
        win.title("FileLife — opções")
        win.resizable(False, False)
        ttk.Label(win, text="Pasta vigiada:").grid(row=0, column=0, sticky="w")
        folder_lbl = ttk.Label(win, text=str(watch_dir(settings)))
        folder_lbl.grid(row=0, column=1, sticky="w", padx=6)

        def choose():
            d = filedialog.askdirectory(parent=win, initialdir=str(watch_dir(settings)))
            if not d:
                return
            reason = fl.unsafe_watch_reason(Path(d))
            if reason:
                messagebox.showerror("FileLife", f"Pasta recusada: {reason}.", parent=win); return
            settings["watch_dir"] = d; save_settings(settings)
            restart_watcher(); folder_lbl.configure(text=d); refresh_list()

        ttk.Button(win, text="Trocar…", command=choose).grid(row=0, column=2)
        ttk.Label(win, text="Só perguntar para arquivos acima de (MB):").grid(row=1, column=0, sticky="w", pady=8)
        size_var = tk.StringVar(value=str(settings["min_size_mb"]))
        ttk.Entry(win, textvariable=size_var, width=8).grid(row=1, column=1, sticky="w", padx=6)

        drive_var = tk.StringVar()
        def drive_text():
            drive_var.set("Google Drive: conectado ✅" if drive_connected() else "Google Drive: não conectado")
        drive_text()
        ttk.Label(win, textvariable=drive_var).grid(row=2, column=0, sticky="w")
        dbtns = ttk.Frame(win); dbtns.grid(row=2, column=1, columnspan=2, sticky="w")
        ttk.Button(dbtns, text="Conectar", command=lambda: connect_drive(after=drive_text)).pack(side="left", padx=6)
        ttk.Button(dbtns, text="Desconectar", command=lambda: (fl.drive_logout(), drive_text(), refresh_list())).pack(side="left")
        ttk.Button(dbtns, text="Importar credenciais…", command=lambda: import_credentials(win)).pack(side="left", padx=6)

        auto_var = tk.BooleanVar(value=_safe(fl.autostart_enabled, False))
        def toggle_auto():
            try:
                fl.set_autostart(auto_var.get())
            except Exception as e:
                messagebox.showerror("FileLife", str(e), parent=win)
        ttk.Checkbutton(win, text="Abrir junto com o computador", variable=auto_var,
                        command=toggle_auto).grid(row=3, column=0, columnspan=3, sticky="w", pady=8)

        def save():
            try:
                v = float(size_var.get().replace(",", "."))
                if not 0 <= v <= 100_000:
                    raise ValueError
            except ValueError:
                messagebox.showerror("FileLife", "Tamanho inválido.", parent=win); return
            settings["min_size_mb"] = v; save_settings(settings); restart_watcher(); win.destroy()

        btns = ttk.Frame(win); btns.grid(row=4, column=0, columnspan=3, sticky="e")
        ttk.Button(btns, text="Pausar / retomar", command=toggle_pause).pack(side="left", padx=6)
        ttk.Button(btns, text="Sair do FileLife", command=quit_app).pack(side="left", padx=6)
        ttk.Button(btns, text="Salvar", command=save).pack(side="left")

    def import_credentials(parent):
        f = filedialog.askopenfilename(parent=parent, filetypes=[("JSON", "*.json")])
        if not f:
            return
        try:
            if os.path.getsize(f) > 64 * 1024:
                raise ValueError("Arquivo grande demais para ser de credenciais.")
            fl.save_google_credentials(Path(f).read_text(encoding="utf-8"))
            messagebox.showinfo("FileLife", "Credenciais salvas. Agora clique em Conectar.", parent=parent)
        except Exception as e:
            messagebox.showerror("FileLife", str(e), parent=parent)

    def connect_drive(after=None):
        toast("🌐 Abrindo o navegador para você entrar com o Google…", 6)
        def work():
            try:
                fl.drive_login()
                ui_queue.put(lambda: (toast("✅ Google Drive conectado!"), after and after(), refresh_list()))
            except Exception as e:
                fl.log_action(f"Login Google falhou: {e!r}")
                ui_queue.put(lambda e=e: messagebox.showerror("FileLife", f"Não foi possível conectar: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def toggle_pause():
        state["paused"] = not state["paused"]
        toast("⏸ FileLife pausado: downloads não serão perguntados." if state["paused"] else "▶ FileLife ativo de novo.")
        refresh_list()

    def restart_watcher():
        if state["watcher"]:
            state["watcher"].stop()
        folder = watch_dir(settings)
        folder.mkdir(parents=True, exist_ok=True)
        w = DownloadWatcher(folder, lambda p: ui_queue.put(lambda: ask(p)),
                            min_size=int(settings["min_size_mb"] * 1024 * 1024))
        w.start()
        state["watcher"] = w

    def expiry_loop():
        while True:
            try:
                deleted, failed = process_expired()
                if deleted:
                    names = ", ".join(r["name"] for r in deleted[:3]) + ("…" if len(deleted) > 3 else "")
                    ui_queue.put(lambda n=len(deleted), names=names: (
                        toast(f"🗑️ {n} arquivo(s) venceram e foram para a lixeira:\n{names}", 6), refresh_list()))
            except Exception as e:
                fl.log_action(f"Erro ao processar prazos: {e!r}")
            fl.purge_old_files()
            time.sleep(EXPIRY_CHECK_SECONDS)

    def quit_app():
        if state["watcher"]:
            state["watcher"].stop()
        if state.get("tray"):
            with contextlib.suppress(Exception):
                state["tray"].stop()
        root.quit()

    def pump():
        while True:
            try:
                fn = ui_queue.get_nowait()
            except queue.Empty:
                break
            with contextlib.suppress(Exception):
                fn()
        root.after(150, pump)

    # Outra cópia aberta pede para mostrar a lista
    def instance_listener():
        sock = fl._INSTANCE_SOCKET
        while True:
            try:
                conn, _ = sock.accept()
                conn.close()
                ui_queue.put(open_list)
            except OSError:
                return

    threading.Thread(target=instance_listener, daemon=True).start()
    _start_tray(state, ui_queue, open_list, open_options, toggle_pause, quit_app)
    restart_watcher()
    threading.Thread(target=expiry_loop, daemon=True).start()
    root.after(150, pump)
    if "--no-open" not in sys.argv:
        open_list()
    toast("👀 FileLife ativo: vou perguntar o que fazer com cada download.")
    root.mainloop()


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _ping_running_instance():
    import socket
    with contextlib.suppress(OSError):
        s = socket.create_connection(("127.0.0.1", fl.INSTANCE_PORT), timeout=2)
        s.close()


def _start_tray(state, ui_queue, open_list, open_options, toggle_pause, quit_app):
    """Ícone perto do relógio (se a biblioteca pystray estiver disponível)."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:  # sem bandeja disponível: o programa funciona sem o ícone
        return
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, 63, 63), radius=16, fill=(59, 91, 253))
    d.polygon([(22, 18), (44, 18), (44, 25), (30, 25), (30, 31), (42, 31), (42, 38), (30, 38), (30, 50), (22, 50)],
              fill="white")
    menu = pystray.Menu(
        pystray.MenuItem("Meus arquivos", lambda: ui_queue.put(open_list), default=True),
        pystray.MenuItem("Opções", lambda: ui_queue.put(open_options)),
        pystray.MenuItem("Pausar / retomar", lambda: ui_queue.put(toggle_pause)),
        pystray.MenuItem("Sair", lambda: ui_queue.put(quit_app)),
    )
    icon = pystray.Icon("FileLife", img, "FileLife", menu)
    state["tray"] = icon
    with contextlib.suppress(Exception):
        icon.run_detached()


ICON_PNG = "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAADQklEQVR4nMWXv2vbVxTFP/feJ9mS5bShjf+CLlkKHu0SDF3bGLqaQknH/BNqhi6d21A6FAquaLsUYhe6tGACVreYLlkydfRSSNJA5Pfu7fDVN4kVSZYlmxz4IqQn3jn3nvN9P4QXCAGJ9z95srba7txG+FDVb3gOR0SZBxGuSdRd7xP8+eTZ07t//7p6XHMBCIRsbWEHB5JvfBYfEf69qK6Fgxefi3cUaooohPsxop/f/0F+29qKdHBAkW439M4d8c2dfK/RtJulgJeSBXTuykcR4QGuZskMTgZl77CXtrvdUAHY2ImPG032ygkFioLIhRCPUQLm1sBOBtzs92RfNj6NNY3ySNRWwgtwQVVPFuGiRnj5z8XeU7zcSk3ruBe/fHIAUffiqWkdvNxKAtteQgiUMxp/HmMipg2iXkIEtpMl2/TsyAyBywUIQIafY1VWYzolSSKingNLtplKLj4LuQCr7WrSWsP44qqxwUn1TOyaQMnF01nkIpAzrHbgx6+U9jJTFZQCZrC7F3zdC955a9i5sXOLpmnkI4JZbcPy0vT/eYAKNBtn5GCIc6U+l2pS99kmnwUzdwAqO2pPReAkD33mZSZrC6b6P6+AGqVASvDL78Hdn1732R2eD+DKymT/FxJQ49/H8OgfeHz1NNHyEqy0LrEDOkzOB+vCF7ehvVyFz70ae/AQDo+C9lL1+4ULqCtbvw7r118vc6kZ/PEXdFrgl2lBLtUaUQvKBZLB02fVqzgLFhKQrHpq1EtEp3126xcS4F69ag8eBv2jcRkIWkvV90sRUC9C/SP48rvg2uhb0ITW8mxdWMiCdguuXYV33x5ZB2K26hcW4D4MYjl7wZkoICJm2o6hWgFfkMnsVU5CRHiyZOrZJ2/wQ4jAlc5Q9TD57dYCm1KAJdNUcjm0pJsl+9hORFTkzwfw7c9BszFf2k/PGW5JteRymALuqclGKTgTtmfVanf7phenTmLnSfspCK4mkjP3znUsNzvt1HnS/hIjx/L+rhx72I4aChbTXH01hLnMRR5goYZ62E5/V4612w3t92Q/D8peamCiJhGRibiYi2HF6xGRRU1SA8uDstfvyf7wavaGL6evyHwj1/P/AbQxzxlLZvzxAAAAAElFTkSuQmCC"  # ícone 32x32 da janela (PNG em base64)


if __name__ == "__main__":
    run_gui()
