"""
FileLife — interface gráfica.

Abre um painel no navegador (só no seu computador, em 127.0.0.1) para ver e
controlar os arquivos, entrar com a conta Google e mudar as configurações.

    python filelife_app.py
"""

import json
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import filelifecycle as fl

HOST = "127.0.0.1"
PORT = 8787
TOKEN = secrets.token_urlsafe(24)  # impede que outros sites chamem a API local
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

STATE = {"watcher": None, "login": "idle", "login_error": "", "account": None,
         "account_checked": 0}
STATE_LOCK = threading.Lock()

EDITABLE_CONFIG = {
    "watch_dir": str, "on_expire": str, "dedupe": str, "default_expiry": dict,
    "ai_summary": bool, "cloud_bridge": bool, "cloud_mode": str,
    "cloud_min_mb": int, "cloud_categories": list, "ai_categories": list,
}


# ---------------------------------------------------------------------------
# Dados para a tela
# ---------------------------------------------------------------------------

def account_info(force=False):
    """Conta Google conectada (com cache para não consultar o Google a cada 2 s)."""
    with STATE_LOCK:
        fresh = time.time() - STATE["account_checked"] < 300
        if fresh and not force:
            return STATE["account"]
    try:
        acc = fl.drive_account()
    except Exception as e:
        fl.log_action(f"Conta Google indisponível: {e!r}")
        acc = {"name": "", "email": "", "photo": "", "error": str(e)} \
            if fl.DRIVE_TOKEN.exists() else None
    with STATE_LOCK:
        STATE["account"], STATE["account_checked"] = acc, time.time()
    return acc


def stats():
    with fl.DB_LOCK, fl.db() as conn:
        active, active_bytes = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE status='active'").fetchone()
        in_drive = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE status='active' AND cloud=1"
        ).fetchone()
        ev = {r["kind"]: (r["n"], r["b"] or 0) for r in conn.execute(
            "SELECT kind, COUNT(*) n, SUM(bytes) b FROM events GROUP BY kind")}
    freed = sum(ev.get(k, (0, 0))[1] for k in ("duplicate", "archived", "deleted"))
    organized = ev.get("organized", (0, 0))[0]
    return {
        "active": active, "active_size": fl.human_size(active_bytes),
        "organized": organized,
        "hours_saved": round(organized * fl.CONFIG["minutes_saved_per_file"] / 60, 1),
        "freed": fl.human_size(freed + in_drive[1]),
        "duplicates": ev.get("duplicate", (0, 0))[0],
        "in_drive": in_drive[0],
    }


def files_list():
    with fl.DB_LOCK, fl.db() as conn:
        rows = conn.execute("SELECT * FROM files WHERE status='active' "
                            "ORDER BY COALESCE(expiry_timestamp, 9e18), id DESC").fetchall()
    now = time.time()
    out = []
    for r in rows:
        exp = r["expiry_timestamp"]
        out.append({
            "id": r["id"], "name": r["new_name"], "original": r["original_name"],
            "category": r["category"], "tag": r["tag"], "size": fl.human_size(r["size"] or 0),
            "expiry": fl.fmt_ts(exp), "days_left": None if exp is None else round((exp - now) / 86400, 1),
            "where": "drive" if r["drive_id"] else ("nuvem" if r["cloud"] else "pc"),
            "has_summary": bool(r["summary_path"] and Path(r["summary_path"]).exists()),
            "versions": len(fl.list_versions(r["id"])),
        })
    return out


def full_state():
    acc = account_info()
    with STATE_LOCK:
        login = {"status": STATE["login"], "error": STATE["login_error"]}
    watcher = STATE["watcher"]
    return {
        "account": acc, "login": login,
        "has_credentials": fl.DRIVE_CREDENTIALS.exists(),
        "stats": stats(), "files": files_list(),
        "activity": list(fl.ACTIVITY)[-40:][::-1],
        "watching": str(watcher.folder) if watcher else "",
        "config": {k: fl.CONFIG.get(k) for k in EDITABLE_CONFIG},
        "categories": list(fl.CATEGORIES) + ["Outros"],
    }


# ---------------------------------------------------------------------------
# Ações
# ---------------------------------------------------------------------------

def start_login():
    def run():
        try:
            fl.drive_login()
            account_info(force=True)
            fl.say("✅ Conta Google conectada! Arquivos grandes agora vão para o seu Drive.")
            with STATE_LOCK:
                STATE["login"], STATE["login_error"] = "idle", ""
        except Exception as e:
            fl.log_action(f"Login Google falhou: {e!r}")
            with STATE_LOCK:
                STATE["login"], STATE["login_error"] = "error", str(e)
    with STATE_LOCK:
        if STATE["login"] == "running":
            return
        STATE["login"], STATE["login_error"] = "running", ""
    threading.Thread(target=run, daemon=True).start()


def do_logout():
    fl.drive_logout()
    with STATE_LOCK:
        STATE["account"], STATE["account_checked"] = None, time.time()
    fl.say("👋 Conta Google desconectada.")


def file_row(file_id):
    with fl.DB_LOCK, fl.db() as conn:
        return conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()


def do_action(data):
    action, fid = data.get("action"), data.get("id")
    fid = int(fid) if fid is not None else None
    if action == "extend":
        exp = fl.set_expiry(fid, fl.parse_duration(data["value"]))
        return f"Novo prazo: {fl.fmt_ts(exp)}"
    if action == "expire":
        fl.set_expiry(fid, 0)
        fl.run_cleanup()
        return "Arquivo expirado"
    if action == "open":
        r = file_row(fid)
        target = r["drive_link"] or Path(r["file_path"]).as_uri()
        webbrowser.open(target)
        return "Abrindo..."
    if action == "folder":
        r = file_row(fid)
        p = Path(r["shortcut_path"] or r["file_path"])
        webbrowser.open(p.parent.as_uri())
        return "Abrindo pasta..."
    if action == "summary":
        r = file_row(fid)
        if r["summary_path"] and Path(r["summary_path"]).exists():
            return Path(r["summary_path"]).read_text(encoding="utf-8")
        threading.Thread(target=fl.summarize_file, args=(fid,), daemon=True).start()
        return "Gerando resumo... ele aparece na atividade em instantes."
    if action == "cloud":
        r = file_row(fid)
        if r["cloud"]:
            return "Esse arquivo já está na nuvem."
        if fl.CONFIG.get("cloud_mode", "api") == "api":
            fl.UPLOAD_QUEUE.put((fid, r["file_path"]))
            return "Enviando para o Google Drive..."
        fl.send_to_cloud(fid, r["file_path"])
        return "Enviado para a nuvem."
    if action == "report":
        out = fl.generate_report()
        webbrowser.open(out.as_uri())
        return "Relatório aberto no navegador."
    if action == "cleanup":
        n = fl.run_cleanup(verbose=False)
        return f"{n} arquivo(s) expirado(s) processado(s)."
    if action == "open_organized":
        fl.ORGANIZED_DIR.mkdir(parents=True, exist_ok=True)
        webbrowser.open(fl.ORGANIZED_DIR.as_uri())
        return "Abrindo pasta..."
    if action == "open_drive":
        webbrowser.open("https://drive.google.com/drive/my-drive")
        return "Abrindo Google Drive..."
    raise ValueError("Ação desconhecida")


def update_config(data):
    changes = {}
    for key, typ in EDITABLE_CONFIG.items():
        if key not in data:
            continue
        value = data[key]
        if typ is int:
            value = int(value)
        elif not isinstance(value, typ):
            raise ValueError(f"Valor inválido para {key}")
        changes[key] = value
    for dur in changes.get("default_expiry", {}).values():
        fl.parse_duration(dur)  # valida
    old_watch = fl.watch_folder()
    fl.save_config(changes)
    new_watch = fl.watch_folder()
    if new_watch != old_watch:
        new_watch.mkdir(parents=True, exist_ok=True)
        STATE["watcher"].stop_event.set()
        STATE["watcher"] = fl.Watcher(new_watch)
        STATE["watcher"].start()
        fl.say(f"👀 Agora monitorando {new_watch}")
    return "Configurações salvas."


# ---------------------------------------------------------------------------
# Servidor HTTP local
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _authorized(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost") and \
            secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            html = (BASE_DIR / "ui.html").read_text(encoding="utf-8")
            return self._send(200, html.replace("__TOKEN__", TOKEN), "text/html; charset=utf-8")
        if url.path == "/api/state":
            if not self._authorized():
                return self._json(403, {"error": "proibido"})
            return self._json(200, full_state())
        if url.path == "/api/versions":
            if not self._authorized():
                return self._json(403, {"error": "proibido"})
            fid = int(parse_qs(url.query)["id"][0])
            return self._json(200, [v.name for v in fl.list_versions(fid)])
        self._json(404, {"error": "não encontrado"})

    def do_POST(self):
        if not self._authorized():
            return self._json(403, {"error": "proibido"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/login":
                start_login()
                msg = "Abrindo o navegador para você entrar com o Google..."
            elif self.path == "/api/logout":
                do_logout()
                msg = "Conta desconectada."
            elif self.path == "/api/action":
                msg = do_action(data)
            elif self.path == "/api/restore":
                fl.restore_version(int(data["id"]), int(data["index"]))
                msg = "Versão restaurada."
            elif self.path == "/api/config":
                msg = update_config(data)
            else:
                return self._json(404, {"error": "não encontrado"})
            self._json(200, {"ok": True, "message": msg})
        except ValueError as e:
            self._json(400, {"ok": False, "message": str(e)})
        except Exception as e:
            fl.log_action(f"Erro na interface ({self.path}): {e!r}")
            self._json(500, {"ok": False, "message": f"Erro: {e}"})


def main():
    STATE["watcher"] = fl.start_background()
    port = PORT
    for attempt in range(20):
        try:
            server = ThreadingHTTPServer((HOST, port + attempt), Handler)
            break
        except OSError:
            continue
    else:
        raise SystemExit("Nenhuma porta livre para a interface.")
    url = f"http://{HOST}:{server.server_address[1]}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fl.say(f"👀 Monitorando {STATE['watcher'].folder}")
    print(f"FileLife aberto em {url}  (feche esta janela para sair)")

    try:
        import webview  # opcional: `pip install pywebview` abre numa janela própria
    except ImportError:
        webview = None
    if webview and "--browser" not in sys.argv:
        webview.create_window("FileLife", url, width=1200, height=800, min_size=(380, 600))
        webview.start()
    else:
        if "--no-open" not in sys.argv:
            webbrowser.open(url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    STATE["watcher"].stop_event.set()
    server.shutdown()


if __name__ == "__main__":
    main()
