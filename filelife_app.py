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
TOKEN = secrets.token_urlsafe(32)  # impede que outros sites chamem a API local
MAX_BODY = 1024 * 1024
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",                 # impede embutir o painel em outro site
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data: https://*.googleusercontent.com; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
}
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

STATE = {"watcher": None, "login": "idle", "login_error": "", "account": None,
         "account_checked": 0}
STATE_LOCK = threading.Lock()

def log(msg):
    """print que não quebra no .exe sem janela de terminal."""
    try:
        print(msg, flush=True)
    except (OSError, ValueError, AttributeError):
        pass


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


def require_row(file_id):
    r = file_row(file_id)
    if r is None or r["status"] != "active":
        raise ValueError("Arquivo não encontrado.")
    return r


def do_action(data):
    action, fid = data.get("action"), data.get("id")
    fid = int(fid) if fid is not None else None
    if action == "extend":
        exp = fl.set_expiry(fid, fl.parse_duration(data["value"]))
        return f"Novo prazo: {fl.fmt_ts(exp)}"
    if action == "expire":
        require_row(fid)
        fl.set_expiry(fid, 0)
        fl.run_cleanup()
        return "Arquivo expirado"
    if action == "open":
        r = require_row(fid)
        if r["drive_link"]:
            webbrowser.open(r["drive_link"])
            return "Abrindo no Google Drive..."
        p = Path(r["file_path"])
        if not p.exists():
            raise ValueError("O arquivo não está mais nesse lugar.")
        if p.suffix.lower() in fl.RISKY_EXTS:
            # Abrir um programa baixado o executaria: mostra a pasta e deixa o usuário decidir
            webbrowser.open(p.parent.as_uri())
            return "Por segurança, programas não são executados daqui: abrindo a pasta."
        webbrowser.open(p.as_uri())
        return "Abrindo..."
    if action == "summary":
        r = require_row(fid)
        if r["summary_path"] and Path(r["summary_path"]).exists():
            return Path(r["summary_path"]).read_text(encoding="utf-8")
        if not fl.CONFIG["ai_summary"]:
            raise ValueError("Ative o resumo com IA em Configurações primeiro.")
        fl.SUMMARY_QUEUE.put(fid)
        return "Gerando resumo... ele aparece na atividade em instantes."
    if action == "cloud":
        r = require_row(fid)
        if r["cloud"]:
            return "Esse arquivo já está na nuvem."
        if fl.CONFIG.get("cloud_mode", "api") == "api":
            if not fl.DRIVE_TOKEN.exists():
                raise ValueError("Entre com sua conta Google primeiro.")
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
    if action == "quit":
        threading.Timer(0.5, SHUTDOWN.set).start()
        return "FileLife encerrado. Pode fechar esta aba."
    raise ValueError("Ação desconhecida")


def update_config(data):
    if not isinstance(data, dict):
        raise ValueError("Dados inválidos")
    changes = {}
    for key, typ in EDITABLE_CONFIG.items():
        if key not in data:
            continue
        value = data[key]
        if typ is int:
            if isinstance(value, bool):
                raise ValueError(f"Valor inválido para {key}")
            value = int(value)
        elif not isinstance(value, typ):
            raise ValueError(f"Valor inválido para {key}")
        changes[key] = value
    if changes.get("watch_dir"):
        folder = Path(changes["watch_dir"]).expanduser()
        reason = fl.unsafe_watch_reason(folder)
        if reason:
            raise ValueError(f"Pasta recusada: {reason}.")
        if not folder.is_dir():
            raise ValueError("Essa pasta não existe.")
    old_watch = fl.watch_folder()
    fl.save_config(changes)  # valida tudo e recusa valores inválidos
    new_watch = fl.watch_folder()
    if new_watch != old_watch:
        STATE["watcher"].stop()
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
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _host_ok(self):
        # Bloqueia "DNS rebinding": só aceita quem acessa pelo endereço local
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.server.server_address[1]}",
                                                f"localhost:{self.server.server_address[1]}")

    def _authorized(self):
        return self._host_ok() and \
            secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        url = urlparse(self.path)
        if not self._host_ok():
            return self._json(403, {"error": "proibido"})
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
            try:
                fid = int(parse_qs(url.query)["id"][0])
            except (KeyError, ValueError, IndexError):
                return self._json(400, {"error": "id inválido"})
            return self._json(200, [v.name for v in fl.list_versions(fid)])
        self._json(404, {"error": "não encontrado"})

    def do_POST(self):
        if not self._authorized():
            return self._json(403, {"error": "proibido"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_BODY:
                return self._json(413, {"ok": False, "message": "pedido grande demais"})
            if "application/json" not in self.headers.get("Content-Type", ""):
                return self._json(415, {"ok": False, "message": "tipo inválido"})
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
        except (ValueError, KeyError, TypeError) as e:
            self._json(400, {"ok": False, "message": str(e) or "Pedido inválido"})
        except Exception as e:
            fl.log_action(f"Erro na interface ({self.path}): {e!r}")
            self._json(500, {"ok": False, "message": "Algo deu errado — veja o log em ~/.filelife/logs"})


SHUTDOWN = threading.Event()


def main():
    url_file = fl.APP_DIR / "ui_url.txt"
    if not fl.acquire_single_instance():
        # Já está aberto: só mostra a janela que existe
        try:
            webbrowser.open(url_file.read_text(encoding="utf-8").strip())
        except OSError:
            log("O FileLife já está aberto.")
        return
    STATE["watcher"] = fl.start_background()
    server = None
    for attempt in range(20):
        try:
            server = ThreadingHTTPServer((HOST, PORT + attempt), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("Nenhuma porta livre para a interface.")
    url = f"http://{HOST}:{server.server_address[1]}/"
    fl.write_private(url_file, url)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fl.say(f"👀 Monitorando {STATE['watcher'].folder}")
    log(f"FileLife aberto em {url}  (feche esta janela para sair)")

    try:
        import webview  # opcional: `pip install pywebview` abre numa janela própria
    except ImportError:
        webview = None
    try:
        if webview and "--browser" not in sys.argv:
            webview.create_window("FileLife", url, width=1200, height=800, min_size=(380, 600))
            threading.Thread(target=lambda: (SHUTDOWN.wait(), [w.destroy() for w in webview.windows]),
                             daemon=True).start()
            webview.start()
        else:
            if "--no-open" not in sys.argv:
                webbrowser.open(url)
            while not SHUTDOWN.wait(1):
                pass
    except KeyboardInterrupt:
        pass
    finally:
        STATE["watcher"].stop()
        server.shutdown()


if __name__ == "__main__":
    main()
