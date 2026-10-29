import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from threading import RLock

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

BASE = Path(__file__).resolve().parent
DATA = Path(os.getenv("BOT_RUN_DATA_DIR") or (BASE / "data")).resolve()
PROJECTS = DATA / "projects"
BACKUPS = DATA / "backups"
LOG_DIR = DATA / "logs"
UPLOADS = DATA / "uploads"
META = DATA / "bots.json"

for p in (PROJECTS, BACKUPS, LOG_DIR, UPLOADS):
    p.mkdir(parents=True, exist_ok=True)

_LOCK = RLock()


def load_meta():
    if not META.exists():
        return {}
    try:
        value = json.loads(META.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_meta(data):
    META.parent.mkdir(parents=True, exist_ok=True)
    tmp = META.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(META)


class Runner:
    """Persistent multi-user Python process runner.

    Each hosted bot gets its own directory and virtual environment. Metadata is
    stored separately from the application code so replacing main.py/config.py
    does not remove hosted projects or their ownership records.
    """

    def __init__(self):
        self.meta = load_meta()
        self._refresh_status()

    def _refresh_status(self):
        changed = False
        with _LOCK:
            for b in self.meta.values():
                pid = b.get("pid")
                alive = bool(pid and self._alive(pid))
                new_status = "running" if alive else "stopped"
                if b.get("status") != new_status or (not alive and b.get("pid") is not None):
                    b["status"] = new_status
                    if not alive:
                        b["pid"] = None
                    changed = True
            if changed:
                save_meta(self.meta)

    @staticmethod
    def _alive(pid):
        try:
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False

    @staticmethod
    def _safe_id(value):
        value = re.sub(r"[^a-zA-Z0-9_-]", "_", str(value))[:60]
        return value or "bot"

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _new_bot_id(self, preferred):
        base = self._safe_id(preferred)
        bot_id = base
        n = 2
        while bot_id in self.meta or (PROJECTS / bot_id).exists():
            bot_id = f"{base}_{n}"
            n += 1
        return bot_id

    def _normalise_project_root(self, project: Path):
        entries = [p for p in project.iterdir() if p.name != "__MACOSX"]
        if len(entries) == 1 and entries[0].is_dir():
            source = entries[0]
            temp = PROJECTS / f".{project.name}_root"
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)
            source.rename(temp)
            shutil.rmtree(project, ignore_errors=True)
            temp.rename(project)

    def _find_entry(self, project: Path):
        direct = project / "main.py"
        if direct.is_file():
            return direct
        candidates = [p for p in project.rglob("main.py") if p.is_file() and ".venv" not in p.parts]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValueError("main.py was not found in the uploaded project.")
        raise ValueError("Multiple main.py files were found. Keep only one main.py in the project.")

    def _extract_zip(self, zip_path: Path, project: Path):
        with zipfile.ZipFile(zip_path) as z:
            for member in z.infolist():
                name = Path(member.filename)
                if name.is_absolute() or ".." in name.parts:
                    raise ValueError("Unsafe ZIP path detected.")
            z.extractall(project)
        self._normalise_project_root(project)

    def import_zip(self, zip_path, owner_id, display_name=None):
        zip_path = Path(zip_path).resolve()
        if not zip_path.is_file():
            raise ValueError("Uploaded ZIP file is missing.")
        with _LOCK:
            bot_id = self._new_bot_id(zip_path.stem)
            project = PROJECTS / bot_id
            project.mkdir(parents=True, exist_ok=False)
            try:
                self._extract_zip(zip_path, project)
                entry = self._find_entry(project)
            except Exception:
                shutil.rmtree(project, ignore_errors=True)
                raise

            self.meta[bot_id] = {
                "id": bot_id,
                "owner_id": int(owner_id),
                "name": display_name or zip_path.stem,
                "project": str(project),
                "entry": str(entry.relative_to(project)),
                "pid": None,
                "status": "stopped",
                "desired_running": False,
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
                "source_type": "zip",
            }
            save_meta(self.meta)
            return bot_id

    def import_main_py(self, main_path, owner_id, display_name=None):
        main_path = Path(main_path).resolve()
        if not main_path.is_file() or main_path.name.lower() != "main.py":
            raise ValueError("Please upload a file named main.py.")
        with _LOCK:
            base_name = display_name or main_path.stem or "Bot"
            bot_id = self._new_bot_id(base_name)
            project = PROJECTS / bot_id
            project.mkdir(parents=True, exist_ok=False)
            try:
                shutil.copy2(main_path, project / "main.py")
            except Exception:
                shutil.rmtree(project, ignore_errors=True)
                raise
            self.meta[bot_id] = {
                "id": bot_id,
                "owner_id": int(owner_id),
                "name": display_name or "main.py",
                "project": str(project),
                "entry": "main.py",
                "pid": None,
                "status": "stopped",
                "desired_running": False,
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
                "source_type": "main.py",
            }
            save_meta(self.meta)
            return bot_id

    def _get(self, bot_id):
        if bot_id not in self.meta:
            raise ValueError("Bot not found.")
        return self.meta[bot_id]

    def _venv_paths(self, project):
        venv = project / ".venv"
        if os.name == "nt":
            return venv / "Scripts" / "python.exe", venv / "Scripts" / "pip.exe"
        return venv / "bin" / "python", venv / "bin" / "pip"

    def _prepare_venv(self, project):
        venv = project / ".venv"
        if not venv.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], cwd=project, check=True, timeout=180)
        python, pip = self._venv_paths(project)
        if not python.exists():
            raise RuntimeError("Project virtual environment is incomplete.")
        req = project / "requirements.txt"
        if req.is_file() and req.stat().st_size > 0:
            subprocess.run([str(pip), "install", "-r", "requirements.txt"], cwd=project, check=True, timeout=900)
        return python

    def start(self, bot_id, mark_desired=True):
        with _LOCK:
            b = self._get(bot_id)
            if b.get("pid") and self._alive(b["pid"]):
                b["status"] = "running"
                if mark_desired:
                    b["desired_running"] = True
                save_meta(self.meta)
                return "🟢 Bot is already running."
            project = Path(b["project"]).resolve()
            if not project.is_dir() or not self._is_within(project, PROJECTS):
                raise ValueError("Bot project directory is missing or invalid.")
            entry = (project / b["entry"]).resolve()
            if not entry.is_file() or not self._is_within(entry, project):
                entry = self._find_entry(project)
                b["entry"] = str(entry.relative_to(project))
            python = self._prepare_venv(project)
            log_file = LOG_DIR / f"{bot_id}.log"
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["BOT_RUN_ID"] = str(bot_id)
            env["BOT_RUN_OWNER_ID"] = str(b.get("owner_id", ""))
            with open(log_file, "ab") as lf:
                lf.write((f"\n--- BOT RUN START {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n").encode())
                process = subprocess.Popen(
                    [str(python), str(entry)],
                    cwd=project,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                )
            b["pid"] = process.pid
            b["status"] = "running"
            if mark_desired:
                b["desired_running"] = True
            b["updated_at"] = int(time.time())
            save_meta(self.meta)
            return f"🟢 Bot started successfully.\n\nPID: `{process.pid}`"

    def stop(self, bot_id, mark_desired=True):
        with _LOCK:
            b = self._get(bot_id)
            pid = b.get("pid")
            if not pid or not self._alive(pid):
                b["pid"] = None
                b["status"] = "stopped"
                if mark_desired:
                    b["desired_running"] = False
                save_meta(self.meta)
                return "🔴 Bot is already stopped."
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
            except Exception:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except Exception:
                    pass
            # Give the process group a moment, then force-kill if necessary.
            deadline = time.time() + 3
            while time.time() < deadline and self._alive(pid):
                time.sleep(0.1)
            if self._alive(pid):
                try:
                    os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
                except Exception:
                    pass
            b["pid"] = None
            b["status"] = "stopped"
            if mark_desired:
                b["desired_running"] = False
            b["updated_at"] = int(time.time())
            save_meta(self.meta)
            return "⏹ Bot stopped successfully."

    def restart(self, bot_id):
        self.stop(bot_id, mark_desired=False)
        time.sleep(0.8)
        return self.start(bot_id, mark_desired=True)

    def logs(self, bot_id, lines=45):
        b = self._get(bot_id)
        path = LOG_DIR / f"{bot_id}.log"
        if not path.exists():
            body = "No logs yet."
        else:
            body = "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
        if len(body) > 3500:
            body = body[-3500:]
        safe = body.replace("`", "'`'" )
        return f"📋 *{b['name']} Logs*\n\n```text\n{safe}\n```"

    def backup(self, bot_id):
        with _LOCK:
            b = self._get(bot_id)
            source = Path(b["project"]).resolve()
            if not source.is_dir():
                raise ValueError("Bot project directory is missing.")
            stamp = time.strftime("%Y%m%d_%H%M%S")
            target = BACKUPS / f"{bot_id}_{stamp}"
            shutil.make_archive(str(target), "zip", root_dir=source)
            return str(target.with_suffix(".zip"))

    def delete(self, bot_id):
        with _LOCK:
            b = self._get(bot_id)
            if b.get("pid") and self._alive(b["pid"]):
                self.stop(bot_id)
            shutil.rmtree(b["project"], ignore_errors=True)
            log = LOG_DIR / f"{bot_id}.log"
            if log.exists():
                log.unlink()
            self.meta.pop(bot_id, None)
            save_meta(self.meta)
            return "🗑 Bot deleted successfully."

    def list_bots(self, owner_id=None):
        self._refresh_status()
        values = list(self.meta.values())
        if owner_id is not None:
            values = [b for b in values if int(b.get("owner_id", 0)) == int(owner_id)]
        return sorted(values, key=lambda x: x.get("created_at", 0), reverse=True)

    def list_backups(self, owner_id=None):
        if owner_id is None:
            return sorted([x.name for x in BACKUPS.glob("*.zip")], reverse=True)
        owned = {b["id"] for b in self.list_bots(owner_id)}
        return sorted([x.name for x in BACKUPS.glob("*.zip") if x.name.split("_")[0] in owned], reverse=True)

    def bot_markup(self, bot_id, include_delete=True):
        b = self._get(bot_id)
        running = b.get("status") == "running" and b.get("pid")
        rows = [[InlineKeyboardButton("⏹ STOP" if running else "▶️ START", callback_data=f"stop:{bot_id}" if running else f"run:{bot_id}")]]
        rows.append([
            InlineKeyboardButton("🔄 RESTART", callback_data=f"restart:{bot_id}"),
            InlineKeyboardButton("📋 LOGS", callback_data=f"logs:{bot_id}"),
        ])
        rows.append([
            InlineKeyboardButton("💾 BACKUP", callback_data=f"backup:{bot_id}"),
            InlineKeyboardButton("🗑 DELETE", callback_data=f"delete:{bot_id}") if include_delete else InlineKeyboardButton("⬅️ BACK", callback_data="my_bots"),
        ])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="my_bots")])
        return InlineKeyboardMarkup(rows)

    def recover_after_master_start(self, subscription_active):
        """Restart only bots that were intentionally running before master restart.

        Returns a list of recovery results. The master waits briefly before calling
        this so Telegram polling is already online.
        """
        results = []
        self._refresh_status()
        for b in self.list_bots():
            if not b.get("desired_running"):
                continue
            if not subscription_active(int(b.get("owner_id", 0))):
                # Keep the intent but don't run expired hosting.
                if b.get("status") == "running":
                    try:
                        self.stop(b["id"], mark_desired=False)
                    except Exception:
                        pass
                results.append((b["id"], False, "subscription expired"))
                continue
            try:
                if b.get("status") != "running":
                    self.start(b["id"], mark_desired=False)
                results.append((b["id"], True, "running"))
            except Exception as exc:
                results.append((b["id"], False, str(exc)[:180]))
        return results
