#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import hashlib
import hmac
import time
import uuid
import ssl
import shlex
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cloud_runtime import CloudRuntime, ServerConfig
from self_update import apply_update, check_for_updates


APP_TITLE = "SMTP Рассылка — ПРОМТЕХРЕШЕНИЯ"
CONFIG_PATH = Path(__file__).resolve().parent / "mailer_gui_config.json"
PROFILES_PATH = Path(__file__).resolve().parent / "mailer_profiles.json"
SCRIPT_PATH = Path(__file__).resolve().parent / "send_email.py"
BASE_DIR = Path(__file__).resolve().parent
PROFILE_STATE_DIR = BASE_DIR / "state_profiles"


class MailerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("980x760")
        self.root.minsize(900, 680)

        self.process: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.cloud_runtime: CloudRuntime | None = None
        self.remote_run_id: str | None = None
        self.remote_task_meta: dict | None = None
        self.current_log_file_path: Path | None = None
        self.current_log_handle = None
        self.progress_total = 0
        self.progress_sent = 0
        self.progress_failed = 0
        self.progress_skipped = 0
        self.progress_running = False
        self.settings_dirty = False
        self._suspend_dirty_tracking = True
        self.server_init_in_progress = False
        self.smtp_accounts: list[dict] = []
        self.cloud_sessions: dict[str, dict] = {}
        self._cloud_monitor_running = False

        self._setup_style()
        self._build_ui()
        self._bind_state_traces()
        self._load_config()
        self._refresh_profile_controls()
        if not self.to_file_var.get().strip():
            self._auto_pick_to_file(silent=True)
        if not self.template_var.get().strip():
            self._auto_pick_template(silent=True)
        self._suspend_dirty_tracking = False
        self.settings_dirty = False
        self._poll_logs()
        self.root.after(1200, self._check_for_updates)
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

    def _set_status_async(self, value: str) -> None:
        self.root.after(0, lambda: self.status_var.set(value))

    def _set_cloud_status_async(self, value: str) -> None:
        self.root.after(0, lambda: self.cloud_status_var.set(value))

    def _set_update_status_async(self, value: str) -> None:
        self.root.after(0, lambda: self.update_status_var.set(value))

    def _on_window_close(self) -> None:
        if self.server_init_in_progress:
            should_close = messagebox.askyesno(
                "Инициализация сервера",
                "Инициализация сервера еще идет. Закрыть программу и прервать процесс?",
            )
            if not should_close:
                return
            self.server_init_in_progress = False
            self.cloud_status_var.set("Инициализация прервана, требуется повторный запуск")
        self.root.destroy()

    def _set_settings_dirty(self, value: bool) -> None:
        self.settings_dirty = value
        suffix = " *" if value else ""
        self.root.title(APP_TITLE + suffix)

    def _profile_state_path(self, profile_name: str) -> Path:
        safe_name = self._sanitize_filename_part(profile_name).lower() or "profile"
        PROFILE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        return PROFILE_STATE_DIR / f"{safe_name}.json"

    def _ensure_profile_state_data(self, profile_name: str, data: dict) -> dict:
        profile_data = dict(data)
        current = str(profile_data.get("state_file", "")).strip()
        # Общий дефолтный state недопустим для профиля — у каждого свой файл,
        # иначе счётчики/курсоры сессий перетирают друг друга.
        shared_defaults = {"", ".send_email_state.json",
                           str((BASE_DIR / ".send_email_state.json"))}
        if current in shared_defaults:
            profile_data["state_file"] = str(self._profile_state_path(profile_name))
        return profile_data

    def _sanitize_filename_part(self, text: str) -> str:
        value = re.sub(r"[^\w\-\.]+", "_", text.strip(), flags=re.UNICODE).strip("._")
        return value[:80] if value else "run"

    def _begin_run_log_file(
        self,
        *,
        force_dry_run: bool,
        override_to: list[str] | None,
        use_to_file: bool,
    ) -> Path:
        logs_dir = BASE_DIR / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        mode = "dry-run" if force_dry_run else "send"
        target = "test" if (override_to and not use_to_file) else "campaign"
        execution = "cloud" if self.cloud_enabled_var.get() else "local"
        base_hint = ""
        if use_to_file and self.to_file_var.get().strip():
            base_hint = Path(self.to_file_var.get().strip()).stem
        elif self.template_var.get().strip():
            base_hint = Path(self.template_var.get().strip()).stem
        base_safe = self._sanitize_filename_part(base_hint)
        file_name = f"{timestamp}_{execution}_{mode}_{target}_{base_safe}.log"
        log_path = logs_dir / file_name
        self.current_log_file_path = log_path
        self.current_log_handle = log_path.open("a", encoding="utf-8")
        return log_path

    def _close_run_log_file(self) -> None:
        if self.current_log_handle is not None:
            try:
                self.current_log_handle.close()
            except Exception:
                pass
        self.current_log_handle = None
        self.current_log_file_path = None

    def _persist_cloud_last_task(self) -> None:
        data = {}
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        data["cloud_last_task"] = self.remote_task_meta or {}
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_profiles_store(self) -> dict:
        if not PROFILES_PATH.exists():
            return {"active": "", "profiles": {}}
        try:
            data = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {"active": "", "profiles": {}}
        if not isinstance(data, dict):
            return {"active": "", "profiles": {}}
        profiles = data.get("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
        active = data.get("active", "")
        return {"active": str(active), "profiles": profiles}

    def _write_profiles_store(self, store: dict) -> None:
        PROFILES_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")

    def _refresh_profile_controls(self, selected: str | None = None) -> None:
        store = self._read_profiles_store()
        profiles = store.get("profiles", {})
        changed = False
        if isinstance(profiles, dict):
            for name, payload in list(profiles.items()):
                if isinstance(payload, dict):
                    normalized = self._ensure_profile_state_data(name, payload)
                    if normalized != payload:
                        profiles[name] = normalized
                        changed = True
        if changed:
            self._write_profiles_store(store)
        names = sorted(profiles.keys()) if isinstance(profiles, dict) else []
        self.profile_combo.configure(values=names)
        if hasattr(self, "_cloud_profile_combo"):
            self._cloud_profile_combo.configure(values=names)
        self._refresh_cloud_batch_list(names)
        target = selected if selected is not None else store.get("active", "")
        if target in names:
            self.profile_var.set(target)
        elif names:
            self.profile_var.set(names[0])
        else:
            self.profile_var.set("")

    def _refresh_cloud_batch_list(self, names: list[str] | None = None) -> None:
        if not hasattr(self, "cloud_batch_list"):
            return
        if names is None:
            store = self._read_profiles_store()
            profiles = store.get("profiles", {})
            names = sorted(profiles.keys()) if isinstance(profiles, dict) else []
        current = set(self.cloud_batch_list.get(0, "end"))
        if current == set(names):
            return
        self.cloud_batch_list.delete(0, "end")
        for name in names:
            self.cloud_batch_list.insert("end", name)

    def _save_profile_as(self) -> None:
        name = simpledialog.askstring("Сохранить профиль", "Название профиля:")
        if not name:
            return
        profile_name = name.strip()
        if not profile_name:
            return
        store = self._read_profiles_store()
        profiles = store.setdefault("profiles", {})
        data = self._collect_settings_data(include_runtime=False)
        profiles[profile_name] = self._ensure_profile_state_data(profile_name, data)
        store["active"] = profile_name
        self._write_profiles_store(store)
        self._refresh_profile_controls(selected=profile_name)
        self._append_log(f"\nПрофиль сохранен: {profile_name}\n")

    def _save_profile_overwrite(self) -> None:
        profile_name = self.profile_var.get().strip()
        if not profile_name:
            self._save_profile_as()
            return
        store = self._read_profiles_store()
        profiles = store.setdefault("profiles", {})
        current = profiles.get(profile_name, {})
        data = self._collect_settings_data(include_runtime=False)
        if isinstance(current, dict):
            if "state_file" in current and str(current.get("state_file", "")).strip():
                data["state_file"] = str(current.get("state_file", "")).strip()
        profiles[profile_name] = self._ensure_profile_state_data(profile_name, data)
        store["active"] = profile_name
        self._write_profiles_store(store)
        self._refresh_profile_controls(selected=profile_name)
        self._append_log(f"\nПрофиль обновлен: {profile_name}\n")

    def _load_selected_profile(self) -> None:
        profile_name = self.profile_var.get().strip()
        if not profile_name:
            messagebox.showinfo("Профили", "Выберите профиль.")
            return
        store = self._read_profiles_store()
        profiles = store.get("profiles", {})
        data = profiles.get(profile_name)
        if not isinstance(data, dict):
            messagebox.showerror("Профили", "Профиль не найден.")
            return
        normalized = self._ensure_profile_state_data(profile_name, data)
        profiles[profile_name] = normalized
        self._apply_settings_data(normalized)
        store["active"] = profile_name
        self._write_profiles_store(store)
        self._refresh_profile_controls(selected=profile_name)
        self._set_settings_dirty(False)
        self._append_log(f"\nПрофиль загружен: {profile_name}\n")

    def _delete_selected_profile(self) -> None:
        profile_name = self.profile_var.get().strip()
        if not profile_name:
            messagebox.showinfo("Профили", "Выберите профиль.")
            return
        if not messagebox.askyesno("Профили", f"Удалить профиль '{profile_name}'?"):
            return
        store = self._read_profiles_store()
        profiles = store.get("profiles", {})
        if profile_name in profiles:
            del profiles[profile_name]
        if store.get("active", "") == profile_name:
            store["active"] = ""
        self._write_profiles_store(store)
        self._refresh_profile_controls()
        self._append_log(f"\nПрофиль удален: {profile_name}\n")

    def _on_refresh_trigger(self, *_args) -> None:
        self._refresh_state_info()
        if not self._suspend_dirty_tracking:
            self._set_settings_dirty(True)

    def _on_dirty_trigger(self, *_args) -> None:
        if not self._suspend_dirty_tracking:
            self._set_settings_dirty(True)
        self._refresh_simple_pixel_status()

    def _bind_state_traces(self) -> None:
        refresh_vars = [
            self.template_var,
            self.to_file_var,
            self.email_col_var,
            self.kind_col_var,
            self.kind_filter_var,
            self.fields_var,
            self.start_row_var,
            self.sheet_var,
            self.allow_duplicate_emails_var,
            self.use_kind_template_var,
            self.state_file_var,
        ]
        dirty_only_vars = [
            self.subject_var,
            self.auto_template_var,
            self.extra_to_var,
            self.smtp_host_var,
            self.smtp_port_var,
            self.smtp_user_var,
            self.smtp_pass_var,
            self.remember_password_var,
            self.from_email_var,
            self.limit_min_var,
            self.limit_day_var,
            self.hub_url_var,
            self.hub_connection_id_var,
            self.hub_secret_var,
            self.hub_insecure_ssl_var,
            self.cloud_enabled_var,
            self.server_host_var,
            self.server_port_var,
            self.server_user_var,
            self.server_password_var,
            self.server_key_path_var,
            self.server_key_pass_var,
            self.server_remote_dir_var,
            self.dry_run_var,
            self.test_email_var,
        ]
        for var in refresh_vars:
            var.trace_add("write", self._on_refresh_trigger)
        for var in dirty_only_vars:
            var.trace_add("write", self._on_dirty_trigger)

    def _add_paste_support(self, widget: ttk.Entry) -> None:
        def handle_paste(_event=None):
            try:
                text = self.root.clipboard_get()
            except tk.TclError:
                return "break"
            widget.insert("insert", text)
            return "break"

        widget.bind("<Command-v>", handle_paste, add=True)
        widget.bind("<Control-v>", handle_paste, add=True)
        widget.bind("<<Paste>>", handle_paste, add=True)

    def _setup_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=14)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(4, weight=1)

        ttk.Label(container, text=APP_TITLE, style="Header.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )

        self._build_template_section(container, row=1)
        self._build_excel_section(container, row=2)
        self._build_smtp_section(container, row=3)
        self._build_action_tabs(container, row=4)
        self._build_status_bar(container, row=5)
        self._refresh_simple_pixel_status()

    def _build_template_section(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="Шаблон и тема", padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)

        self.template_var = tk.StringVar()
        self.subject_var = tk.StringVar()
        self.auto_template_var = tk.BooleanVar(value=True)

        ttk.Label(frame, text="HTML шаблон:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.template_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(frame, text="Выбрать", command=self._pick_template).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Button(frame, text="Авто по базе", command=self._auto_pick_template).grid(
            row=0, column=3, padx=(8, 0)
        )

        ttk.Label(frame, text="Тема (опц.):").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.subject_var).grid(
            row=1, column=1, columnspan=3, sticky="ew", pady=(8, 0)
        )
        ttk.Checkbutton(
            frame,
            text="Автоподбор HTML из базы/листа",
            variable=self.auto_template_var,
        ).grid(row=2, column=1, columnspan=3, sticky="w", pady=(8, 0))

    def _build_excel_section(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="Получатели и персонализация", padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        for idx in range(6):
            frame.columnconfigure(idx, weight=1 if idx in (1, 3, 5) else 0)

        self.to_file_var = tk.StringVar()
        self.email_col_var = tk.StringVar(value="G")
        self.kind_col_var = tk.StringVar(value="P")
        self.kind_filter_var = tk.StringVar(value="ALL")
        self.fields_var = tk.StringVar(value="A,B,C,D")
        self.start_row_var = tk.StringVar(value="2")
        self.sheet_var = tk.StringVar(value="active")
        self.extra_to_var = tk.StringVar()
        self.use_kind_template_var = tk.BooleanVar(value=True)
        self.allow_duplicate_emails_var = tk.BooleanVar(value=False)
        self.state_file_var = tk.StringVar(value=".send_email_state.json")

        ttk.Label(frame, text="Файл базы:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.to_file_var).grid(row=0, column=1, columnspan=4, sticky="ew")
        ttk.Button(frame, text="Выбрать", command=self._pick_excel).grid(row=0, column=5, padx=(8, 0))

        ttk.Label(frame, text="Лист:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.sheet_var, width=10).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(frame, text="Колонка Email:").grid(row=1, column=2, sticky="w", padx=(12, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.email_col_var, width=8).grid(row=1, column=3, sticky="ew", pady=(8, 0))
        ttk.Label(frame, text="Колонка вида:").grid(row=1, column=4, sticky="w", padx=(12, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.kind_col_var, width=8).grid(row=1, column=5, sticky="ew", pady=(8, 0))

        ttk.Label(frame, text="Поля (A,B,C,D):").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.fields_var).grid(row=2, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(frame, text="Старт строка:").grid(row=2, column=3, sticky="w", padx=(12, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.start_row_var, width=8).grid(row=2, column=4, sticky="ew", pady=(8, 0))
        ttk.Label(frame, text="Фильтр вида (ALL/ГВС,Море):").grid(
            row=3, column=3, sticky="w", padx=(12, 8), pady=(8, 0)
        )
        ttk.Entry(frame, textvariable=self.kind_filter_var).grid(
            row=3, column=4, columnspan=2, sticky="ew", pady=(8, 0)
        )

        ttk.Label(frame, text="Доп. email (через запятую):").grid(
            row=4, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        ttk.Entry(frame, textvariable=self.extra_to_var).grid(
            row=4, column=1, columnspan=3, sticky="ew", pady=(8, 0)
        )
        ttk.Checkbutton(
            frame,
            text="Шаблон по колонке вида рассылки",
            variable=self.use_kind_template_var,
        ).grid(row=4, column=4, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            frame,
            text="Разрешить повторы email",
            variable=self.allow_duplicate_emails_var,
        ).grid(row=5, column=4, columnspan=2, sticky="w", pady=(4, 0))

    def _build_smtp_section(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="SMTP и лимиты", padding=10)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        for idx in range(8):
            frame.columnconfigure(idx, weight=1 if idx in (1, 3, 5, 7) else 0)

        self.smtp_host_var = tk.StringVar(value="smtp.timeweb.ru")
        self.smtp_port_var = tk.StringVar(value="465")
        self.smtp_user_var = tk.StringVar(value="SZFO@teploobmennik.online")
        self.smtp_pass_var = tk.StringVar(value=os.getenv("SMTP_PASSWORD", ""))
        self.from_email_var = tk.StringVar(value="SZFO@teploobmennik.online")
        self.limit_min_var = tk.StringVar(value="20")
        self.limit_day_var = tk.StringVar(value="300")
        self.dry_run_var = tk.BooleanVar(value=False)
        self.remember_password_var = tk.BooleanVar(value=False)

        ttk.Label(frame, text="SMTP host:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.smtp_host_var).grid(row=0, column=1, sticky="ew")
        ttk.Label(frame, text="Порт:").grid(row=0, column=2, sticky="w", padx=(12, 8))
        ttk.Entry(frame, textvariable=self.smtp_port_var, width=8).grid(row=0, column=3, sticky="ew")
        ttk.Label(frame, text="Лимит/мин:").grid(row=0, column=4, sticky="w", padx=(12, 8))
        ttk.Entry(frame, textvariable=self.limit_min_var, width=8).grid(row=0, column=5, sticky="ew")
        ttk.Label(frame, text="Лимит/сутки:").grid(row=0, column=6, sticky="w", padx=(12, 8))
        ttk.Entry(frame, textvariable=self.limit_day_var, width=8).grid(row=0, column=7, sticky="ew")

        ttk.Label(frame, text="SMTP логин:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.smtp_user_var).grid(row=1, column=1, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(frame, text="Пароль:").grid(row=1, column=4, sticky="w", padx=(12, 8), pady=(8, 0))
        self.smtp_pass_entry = ttk.Entry(frame, textvariable=self.smtp_pass_var, show="*")
        self.smtp_pass_entry.grid(row=1, column=5, columnspan=3, sticky="ew", pady=(8, 0))
        self._add_paste_support(self.smtp_pass_entry)
        ttk.Checkbutton(
            frame,
            text="Запомнить пароль локально",
            variable=self.remember_password_var,
        ).grid(row=2, column=4, columnspan=4, sticky="w", padx=(12, 0), pady=(8, 0))

        ttk.Label(frame, text="From:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.from_email_var).grid(
            row=2, column=1, columnspan=3, sticky="ew", pady=(8, 0)
        )
        ttk.Checkbutton(frame, text="Только проверка (dry-run)", variable=self.dry_run_var).grid(
            row=3, column=4, columnspan=4, sticky="w", padx=(12, 0), pady=(8, 0)
        )
        ttk.Button(frame, text="SMTP аккаунты до 5", command=self._edit_smtp_accounts).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

    def _edit_smtp_accounts(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("SMTP аккаунты")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("1080x340")
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Пул до 5 аккаунтов с авто-релеем: когда аккаунт упирается в свой дневной лимит, "
                 "рассылка автоматически продолжается со следующего аккаунта с той же строки базы. "
                 "Ставь разных провайдеров — бан одного не остановит остальные.",
            wraplength=1040,
        ).grid(row=0, column=0, columnspan=9, sticky="w", pady=(0, 8))
        headers = ["Вкл", "Название/домен", "Host", "Порт", "Логин", "Пароль", "From", "Лимит/день", "Сегодня"]
        for col, title in enumerate(headers):
            ttk.Label(frame, text=title, style="TLabelframe.Label").grid(row=1, column=col, sticky="w", padx=4)
            frame.columnconfigure(col, weight=1 if col in (1, 2, 4, 5, 6) else 0)

        state_data = self._load_state_json()
        today = date.today().isoformat()
        sent_map = state_data.get("account_sent_today", {}) if str(state_data.get("date", "")) == today else {}
        if not isinstance(sent_map, dict):
            sent_map = {}

        rows = []
        current = list(self.smtp_accounts)
        while len(current) < 5:
            current.append({})
        for idx in range(5):
            item = current[idx]
            enabled = tk.BooleanVar(value=bool(item.get("enabled", idx == 0 and not self.smtp_accounts)))
            label = tk.StringVar(value=str(item.get("label", "")))
            host = tk.StringVar(value=str(item.get("host", self.smtp_host_var.get().strip() or "smtp.timeweb.ru")))
            port = tk.StringVar(value=str(item.get("port", self.smtp_port_var.get().strip() or "465")))
            user = tk.StringVar(value=str(item.get("user", "")))
            password = tk.StringVar(value=str(item.get("password", "")))
            from_email = tk.StringVar(value=str(item.get("from_email", "")))
            daily_limit = tk.StringVar(value=str(item.get("daily_limit", self.limit_day_var.get().strip() or "2000")))
            values = [enabled, label, host, port, user, password, from_email, daily_limit]
            grid_row = idx + 2
            ttk.Checkbutton(frame, variable=enabled).grid(row=grid_row, column=0, padx=4, pady=4)
            for col, var in enumerate(values[1:], start=1):
                show = "*" if col == 5 else ""
                entry = ttk.Entry(frame, textvariable=var, show=show, width=12)
                entry.grid(row=grid_row, column=col, sticky="ew", padx=4, pady=4)
                if col == 5:
                    self._add_paste_support(entry)
            key = (user.get() or from_email.get() or label.get()).strip().lower()
            sent_today = int(sent_map.get(key, 0)) if key else 0
            limit_view = daily_limit.get().strip() or "2000"
            ttk.Label(frame, text=f"{sent_today}/{limit_view}").grid(row=grid_row, column=8, sticky="w", padx=4)
            rows.append(values)

        total_capacity = 0
        for _e, _l, _h, _p, _u, _pw, _f, dl in rows:
            if _e.get() and _u.get().strip() and _pw.get().strip():
                total_capacity += self._safe_int(dl.get().strip(), 0)
        ttk.Label(
            frame,
            text=f"Суммарная ёмкость активных аккаунтов: до {total_capacity} писем/сутки. "
                 "«Сегодня» — сколько уже ушло с каждого (сбрасывается в полночь).",
        ).grid(row=7, column=0, columnspan=9, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, columnspan=9, sticky="e", pady=(12, 0))

        def save() -> None:
            accounts = []
            for enabled, label, host, port, user, password, from_email, daily_limit in rows:
                if not enabled.get():
                    continue
                payload = {
                    "enabled": True,
                    "label": label.get().strip(),
                    "host": host.get().strip() or "smtp.timeweb.ru",
                    "port": port.get().strip() or "465",
                    "user": user.get().strip(),
                    "password": password.get().strip(),
                    "from_email": from_email.get().strip() or user.get().strip(),
                    "daily_limit": daily_limit.get().strip() or "2000",
                }
                if payload["user"] and payload["password"]:
                    accounts.append(payload)
            self.smtp_accounts = accounts[:5]
            self._set_settings_dirty(True)
            self._append_log(f"\nSMTP аккаунты обновлены: {len(self.smtp_accounts)} активн.\n")
            dialog.destroy()

        ttk.Button(buttons, text="Сохранить", command=save).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(buttons, text="Отмена", command=dialog.destroy).grid(row=0, column=1)

    def _build_log_section(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Лог выполнения", padding=10)
        frame.grid(row=0, column=0, sticky="nsew", pady=(0, 0))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(frame, wrap="word", height=16, font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.insert("end", "Готово. Заполните поля и нажмите «Проверить» или «Отправить».\n")

    def _build_action_tabs(self, parent: ttk.Frame, row: int) -> None:
        notebook = ttk.Notebook(parent)
        notebook.grid(row=row, column=0, sticky="nsew", pady=(0, 8))

        simple_tab = ttk.Frame(notebook, padding=10)
        send_tab = ttk.Frame(notebook, padding=10)
        test_tab = ttk.Frame(notebook, padding=10)
        validate_tab = ttk.Frame(notebook, padding=10)
        hub_tab = ttk.Frame(notebook, padding=10)
        cloud_tab = ttk.Frame(notebook, padding=10)
        log_tab = ttk.Frame(notebook, padding=10)
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)
        notebook.add(simple_tab, text="Просто")
        notebook.add(send_tab, text="Рассылка")
        notebook.add(test_tab, text="Тест")
        notebook.add(validate_tab, text="Проверка")
        notebook.add(hub_tab, text="Хаб")
        notebook.add(cloud_tab, text="Облако")
        notebook.add(log_tab, text="Логи")

        self._build_simple_controls(simple_tab)
        self._build_send_controls(send_tab)
        self._build_test_controls(test_tab)
        self._build_validate_controls(validate_tab)
        self._build_hub_controls(hub_tab)
        self._build_cloud_controls(cloud_tab)
        self._build_log_section(log_tab)

    def _build_simple_controls(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text="Простой режим: выбери Excel, укажи колонку email, выбери HTML — и отправляй.",
            style="Header.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="1. Excel-база:").grid(row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.to_file_var).grid(row=1, column=1, sticky="ew")
        ttk.Button(frame, text="Выбрать Excel", command=self._pick_excel).grid(
            row=1, column=2, padx=(8, 0)
        )
        ttk.Label(
            frame,
            text="Все вкладки книги объединяются автоматически, дубликаты email убираются.",
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(2, 8))

        ttk.Label(frame, text="2. Колонка email:").grid(row=3, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.email_col_var, width=10).grid(
            row=3, column=1, sticky="w"
        )
        ttk.Button(frame, text="Определить", command=self._simple_detect_email_col).grid(
            row=3, column=2, padx=(8, 0)
        )
        ttk.Label(
            frame,
            text="Буква колонки с адресами (например G).",
        ).grid(row=4, column=1, columnspan=2, sticky="w", pady=(2, 8))

        ttk.Label(frame, text="3. HTML-письмо:").grid(row=5, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.template_var).grid(row=5, column=1, sticky="ew")
        ttk.Button(frame, text="Выбрать HTML", command=self._simple_pick_template).grid(
            row=5, column=2, padx=(8, 0)
        )

        ttk.Label(frame, text="Тема (опц.):").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.subject_var).grid(
            row=6, column=1, columnspan=2, sticky="ew", pady=(8, 0)
        )

        actions = ttk.Frame(frame)
        actions.grid(row=7, column=0, columnspan=3, sticky="w", pady=(14, 0))
        ttk.Button(actions, text="Проверить", command=self._simple_dry_run).grid(row=0, column=0)
        ttk.Button(actions, text="📨 Отправить", command=self._simple_send).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Button(actions, text="Стоп", command=self._stop_process).grid(row=0, column=2, padx=(8, 0))

        self.simple_pixel_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.simple_pixel_var).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        self._refresh_simple_pixel_status()

    def _apply_simple_mode_settings(self) -> None:
        """Зафиксировать простые значения по умолчанию перед запуском."""
        self.sheet_var.set("ALL")
        self.kind_filter_var.set("ALL")
        self.use_kind_template_var.set(False)
        self.auto_template_var.set(False)
        if not self.start_row_var.get().strip():
            self.start_row_var.set("2")

    def _refresh_simple_pixel_status(self) -> None:
        if not hasattr(self, "simple_pixel_var") or not hasattr(self, "hub_url_var"):
            return
        hub_url = self.hub_url_var.get().strip()
        cid = self.hub_connection_id_var.get().strip()
        secret = self.hub_secret_var.get().strip()
        if hub_url and cid and secret:
            self.simple_pixel_var.set("Пиксель открытий: включён (настройки на вкладке «Хаб»).")
        else:
            self.simple_pixel_var.set("Пиксель открытий: выключен. Заполни вкладку «Хаб» один раз.")

    def _simple_detect_email_col(self) -> None:
        to_file = self.to_file_var.get().strip()
        if not to_file:
            messagebox.showinfo("Колонка email", "Сначала выбери Excel-базу.")
            return
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location("send_email_detect", str(SCRIPT_PATH))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            suggestions = module.detect_xlsx_email_columns(
                Path(to_file).expanduser().resolve(), "ALL"
            )
        except Exception as error:
            messagebox.showerror("Колонка email", f"Не удалось прочитать файл: {error}")
            return
        if not suggestions:
            messagebox.showwarning("Колонка email", "Не нашёл колонку с email. Укажи букву вручную.")
            return
        self.email_col_var.set(suggestions[0])
        self._append_log(f"\nАвтоопределение колонки email: {', '.join(suggestions)} → {suggestions[0]}\n")

    def _simple_pick_template(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите HTML письмо",
            filetypes=[("HTML", "*.html *.htm"), ("Все файлы", "*.*")],
        )
        if path:
            self.template_var.set(path)
            self.auto_template_var.set(False)
            self._refresh_state_info()

    def _simple_dry_run(self) -> None:
        if not self._simple_validate():
            return
        self._apply_simple_mode_settings()
        self._start_process(force_dry_run=True)

    def _simple_send(self) -> None:
        if not self._simple_validate():
            return
        self._apply_simple_mode_settings()
        self._start_process(force_dry_run=False)

    def _simple_validate(self) -> bool:
        if not self.to_file_var.get().strip():
            messagebox.showerror("Проверка", "Шаг 1: выбери Excel-базу.")
            return False
        if not self.email_col_var.get().strip():
            messagebox.showerror("Проверка", "Шаг 2: укажи колонку email (например G).")
            return False
        if not self.template_var.get().strip():
            messagebox.showerror("Проверка", "Шаг 3: выбери HTML письмо.")
            return False
        return True

    def _build_send_controls(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        actions = ttk.Frame(frame)
        actions.grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="Сохранить настройки", command=self._save_config).grid(row=0, column=0)
        ttk.Button(actions, text="Проверить", command=self._start_dry_run).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(actions, text="Отправить", command=self._start_send).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(actions, text="Стоп", command=self._stop_process).grid(row=0, column=3, padx=(8, 0))
        ttk.Label(actions, text="Профиль:").grid(row=0, column=4, padx=(14, 4))
        self.profile_var = tk.StringVar(value="")
        self.profile_combo = ttk.Combobox(actions, textvariable=self.profile_var, width=22, state="readonly")
        self.profile_combo.grid(row=0, column=5, padx=(0, 6))
        ttk.Button(actions, text="Сохранить как", command=self._save_profile_as).grid(row=0, column=6, padx=(0, 6))
        ttk.Button(actions, text="Сохранить в профиль", command=self._save_profile_overwrite).grid(row=0, column=7, padx=(0, 6))
        ttk.Button(actions, text="Загрузить профиль", command=self._load_selected_profile).grid(row=0, column=8, padx=(0, 6))
        ttk.Button(actions, text="Удалить профиль", command=self._delete_selected_profile).grid(row=0, column=9)
        self.profile_combo.bind("<<ComboboxSelected>>", lambda _e: self._load_selected_profile())
        self._refresh_profile_controls()

        state_frame = ttk.LabelFrame(frame, text="Состояние отправки (state)", padding=10)
        state_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for idx in range(6):
            state_frame.columnconfigure(idx, weight=1 if idx in (1, 3, 5) else 0)

        self.state_campaign_key_var = tk.StringVar(value="")
        self.state_cursor_var = tk.StringVar(value="0")
        self.state_last_row_var = tk.StringVar(value="0")
        self.state_sent_today_var = tk.StringVar(value="0")
        self.state_date_var = tk.StringVar(value="—")
        self.resume_from_row_var = tk.StringVar(value="2")

        ttk.Label(state_frame, text="State файл:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(state_frame, textvariable=self.state_file_var).grid(row=0, column=1, columnspan=4, sticky="ew")
        ttk.Button(state_frame, text="Выбрать", command=self._pick_state_file).grid(row=0, column=5, padx=(8, 0))

        ttk.Label(state_frame, text="Дата счётчика:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(state_frame, textvariable=self.state_date_var, state="readonly").grid(row=1, column=1, sticky="ew", pady=(8, 0))

        ttk.Label(state_frame, text="Уже отправлено (позиций):").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(state_frame, textvariable=self.state_cursor_var, state="readonly").grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(state_frame, text="Последняя строка Excel:").grid(row=2, column=2, sticky="w", padx=(12, 8), pady=(8, 0))
        ttk.Entry(state_frame, textvariable=self.state_last_row_var, state="readonly").grid(row=2, column=3, sticky="ew", pady=(8, 0))
        ttk.Label(state_frame, text="Отправлено сегодня:").grid(row=2, column=4, sticky="w", padx=(12, 8), pady=(8, 0))
        ttk.Entry(state_frame, textvariable=self.state_sent_today_var, state="readonly").grid(row=2, column=5, sticky="ew", pady=(8, 0))

        ttk.Label(state_frame, text="Начать со строки:").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(state_frame, textvariable=self.resume_from_row_var).grid(row=3, column=1, sticky="ew", pady=(8, 0))
        ttk.Button(state_frame, text="Применить", command=self._apply_resume_from_row).grid(
            row=3, column=2, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        controls = ttk.Frame(state_frame)
        controls.grid(row=4, column=0, columnspan=6, sticky="w", pady=(10, 0))
        ttk.Button(controls, text="Обновить state", command=self._refresh_state_info).grid(row=0, column=0)
        ttk.Button(controls, text="Сбросить прогресс кампании", command=self._reset_campaign_state).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(controls, text="Сбросить счётчик дня", command=self._reset_daily_state).grid(row=0, column=2, padx=(8, 0))

    def _build_test_controls(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        self.test_email_var = tk.StringVar()

        ttk.Label(frame, text="Тестовый email:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.test_email_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(frame, text="Проверить тест", command=self._start_test_dry_run).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Button(frame, text="Отправить тест", command=self._start_test_send).grid(
            row=0, column=3, padx=(8, 0)
        )
        ttk.Button(frame, text="Стоп", command=self._stop_process).grid(row=0, column=4, padx=(8, 0))

        ttk.Label(
            frame,
            text="Тест отправляется только на указанный email, база получателей не используется.",
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))

    def _build_validate_controls(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(7, weight=1)

        ttk.Label(
            frame,
            text="Чистка базы. «Убрать дубликаты» — мгновенно, без интернета (по всей книге). "
                 "«Проверить почту» — дополнительно проверяет синтаксис и MX-запись домена "
                 "(медленно, для больших баз не нужно).",
            wraplength=760,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(frame, text="Файл базы:").grid(row=1, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.to_file_var).grid(row=1, column=1, sticky="ew")
        ttk.Button(frame, text="Выбрать", command=self._pick_excel).grid(row=1, column=2, padx=(8, 0))

        ttk.Label(frame, text="Колонка email:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        row2 = ttk.Frame(frame)
        row2.grid(row=2, column=1, columnspan=2, sticky="w", pady=(8, 0))
        self.validate_start_row_var = tk.StringVar(value="2")
        ttk.Entry(row2, textvariable=self.email_col_var, width=8).grid(row=0, column=0)
        ttk.Label(row2, text="   Данные со строки:").grid(row=0, column=1, padx=(8, 4))
        ttk.Entry(row2, textvariable=self.validate_start_row_var, width=6).grid(row=0, column=2)
        ttk.Label(row2, text="(обычно 2 — чистится вся база, не с позиции докрутки)").grid(row=0, column=3, padx=(8, 0))

        self.validate_dedup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame,
            text="Удалять дубликаты email (по всем листам книги)",
            variable=self.validate_dedup_var,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.validate_overwrite_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame,
            text="Заменить исходный файл (создаётся копия .bak) — тогда рассылка сразу пойдёт по чистой базе",
            variable=self.validate_overwrite_var,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))

        actions = ttk.Frame(frame)
        actions.grid(row=5, column=0, columnspan=3, sticky="w", pady=(14, 0))
        self.validate_dedup_btn = ttk.Button(actions, text="⚡ Убрать дубликаты (быстро)", command=self._start_dedup_only)
        self.validate_dedup_btn.grid(row=0, column=0)
        self.validate_txt_btn = ttk.Button(actions, text="📄 Дубли → .txt (через запятую)", command=self._export_txt_dedup)
        self.validate_txt_btn.grid(row=0, column=1, padx=(8, 0))
        self.validate_start_btn = ttk.Button(actions, text="🩺 Проверить почту + очистить", command=self._start_validation)
        self.validate_start_btn.grid(row=0, column=2, padx=(8, 0))
        self.validate_stop_btn = ttk.Button(actions, text="Стоп", command=self._stop_validation, state="disabled")
        self.validate_stop_btn.grid(row=0, column=3, padx=(8, 0))
        self.validate_cloud_btn = ttk.Button(actions, text="☁ Очистить на облаке (MX+дубли)", command=self._start_cloud_clean)
        self.validate_cloud_btn.grid(row=1, column=0, pady=(8, 0), sticky="w")

        self.validate_progress_var = tk.StringVar(value="Готов к проверке.")
        ttk.Label(
            frame, textvariable=self.validate_progress_var, style="Header.TLabel"
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 4))

        from tkinter.scrolledtext import ScrolledText
        self.validate_log = ScrolledText(frame, height=14, wrap="word")
        self.validate_log.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(4, 0))
        self.validate_log.configure(state="disabled")

        self._validate_cancel = None
        self._validate_thread = None

    def _validate_log_append(self, text: str) -> None:
        self.validate_log.configure(state="normal")
        self.validate_log.insert("end", text + "\n")
        self.validate_log.see("end")
        self.validate_log.configure(state="disabled")

    def _start_validation(self) -> None:
        import threading
        from pathlib import Path as _Path
        from tkinter import messagebox as _mb
        src_raw = self.to_file_var.get().strip()
        if not src_raw:
            _mb.showwarning("Проверка", "Сначала выбери файл базы.")
            return
        src = _Path(src_raw).expanduser().resolve()
        if not src.exists():
            _mb.showerror("Проверка", f"Файл не найден: {src}")
            return
        col = (self.email_col_var.get() or "A").strip()
        try:
            start_row = int(self.validate_start_row_var.get() or "2")
        except ValueError:
            start_row = 2
        overwrite = bool(self.validate_overwrite_var.get())

        self.validate_log.configure(state="normal")
        self.validate_log.delete("1.0", "end")
        self.validate_log.configure(state="disabled")
        self.validate_progress_var.set("Загружаю адреса…")
        self.validate_start_btn.configure(state="disabled")
        self.validate_dedup_btn.configure(state="disabled")
        self.validate_stop_btn.configure(state="normal")

        dedup = bool(self.validate_dedup_var.get())
        self._validate_cancel = threading.Event()
        cancel = self._validate_cancel

        def log(msg: str) -> None:
            self.root.after(0, lambda m=msg: self._validate_log_append(m))

        def status(msg: str) -> None:
            self.root.after(0, lambda m=msg: self.validate_progress_var.set(m))

        def worker():
            import email_validator as ev
            import csv as _csv
            try:
                is_xlsx = src.suffix.lower() in {".xlsx", ".xlsm"}
                status("Загружаю адреса из файла…")
                if is_xlsx:
                    emails, _ = ev._read_xlsx_emails(src, col, start_row)
                else:
                    emails = ev._read_text_emails(src)
                total = len(emails)
                if total == 0:
                    self.root.after(0, lambda: self._finish_validation("В файле нет адресов.", None))
                    return
                uniq = len({e.strip().lower() for e in emails})
                log(f"Загружено {total} адресов ({uniq} уникальных). Проверяю MX/синтаксис…")
                status(f"0/{total}  ·  проверяю…")

                results = [None] * total
                ok_n = bad_n = 0
                from concurrent.futures import ThreadPoolExecutor, as_completed
                with ThreadPoolExecutor(max_workers=16) as pool:
                    futures = {pool.submit(ev.validate_one, e): i for i, e in enumerate(emails)}
                    done = 0
                    last_pct = -1
                    log_step = max(total // 20, 1)  # ~20 строк лога за прогон
                    for fut in as_completed(futures):
                        if cancel.is_set():
                            for f in futures:
                                f.cancel()
                            self.root.after(0, lambda: self._finish_validation("Прервано пользователем.", None))
                            return
                        i = futures[fut]
                        try:
                            r = fut.result()
                        except Exception as exc:
                            r = {"email": emails[i], "ok": False, "reason": f"error: {exc}"}
                        results[i] = r
                        if r["ok"]:
                            ok_n += 1
                        else:
                            bad_n += 1
                        done += 1
                        pct = int(done / total * 100)
                        if pct != last_pct:
                            last_pct = pct
                            status(f"{done}/{total} ({pct}%)  ·  валидных {ok_n}, плохих {bad_n}")
                        if done % log_step == 0:
                            log(f"  …{done}/{total} проверено (валидных {ok_n}, плохих {bad_n})")

                bad = [r for r in results if r and not r["ok"]]
                ok_results = [r for r in results if r and r["ok"]]
                bad_set = {r["email"].strip() for r in bad}

                stem = src.stem
                cleaned = src.with_name(f"{stem}_очищенный{src.suffix}")
                bad_path = src.with_name(f"{stem}_невалидные.txt")
                report_path = src.with_name(f"{stem}_отчёт.csv")

                status("Записываю очищенный файл…")
                bad_path.write_text("\n".join(r["email"] for r in bad), encoding="utf-8")
                with report_path.open("w", encoding="utf-8", newline="") as f:
                    w = _csv.writer(f)
                    w.writerow(["email", "ok", "reason"])
                    for r in results:
                        if r:
                            w.writerow([r["email"], "1" if r["ok"] else "0", r["reason"]])

                removed_dup = 0
                if is_xlsx:
                    removed_bad, removed_dup, _removed = ev._write_xlsx_clean_dedup(
                        src, cleaned, bad_set, dedup, col, start_row
                    )
                else:
                    seen = set()
                    kept = []
                    for r in ok_results:
                        key = r["email"].strip().lower()
                        if dedup and key in seen:
                            removed_dup += 1
                            continue
                        seen.add(key)
                        kept.append(r["email"])
                    cleaned.write_text("\n".join(kept), encoding="utf-8")
                    removed_bad = len(bad)

                by_reason = {}
                for r in bad:
                    by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
                reason_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(by_reason.items(), key=lambda x: -x[1]))
                dup_line = f"Удалено дубликатов: {removed_dup}\n" if dedup else ""

                final_file, extra = self._finalize_cleaned(src, cleaned, overwrite)
                summary = (
                    f"\nГотово. Валидных: {len(ok_results)}, удалено невалидных: {removed_bad}\n"
                    f"{dup_line}"
                    f"{reason_lines}\n\n"
                    f"{extra}"
                    f"Список невалидных: {bad_path}\n"
                    f"Отчёт: {report_path}"
                )
                final_status = (
                    f"Готово. Валидных {len(ok_results)}, "
                    f"невалидных {removed_bad}"
                    + (f", дублей {removed_dup}" if dedup else "")
                    + "."
                )
                self.root.after(0, lambda: self._finish_validation(final_status, summary))
            except BaseException as exc:  # noqa: BLE001 — SystemExit из openpyxl тоже показываем
                self.root.after(0, lambda e=exc: self._finish_validation(f"Ошибка: {e}", None))

        self._validate_thread = threading.Thread(target=worker, daemon=True)
        self._validate_thread.start()

    def _stop_validation(self) -> None:
        if self._validate_cancel:
            self._validate_cancel.set()

    def _finish_validation(self, status_text: str, summary: str | None) -> None:
        self.validate_progress_var.set(status_text)
        if summary:
            self._validate_log_append(summary)
        self.validate_start_btn.configure(state="normal")
        self.validate_dedup_btn.configure(state="normal")
        self.validate_stop_btn.configure(state="disabled")

    def _finalize_cleaned(self, src: Path, cleaned: Path, overwrite: bool) -> tuple[Path, str]:
        """Если overwrite — заменяет исходник очищенным (с бэкапом .bak). Возвращает (итоговый_файл, текст)."""
        if not overwrite:
            return cleaned, f"Очищенный файл: {cleaned}\n"
        backup = src.with_name(f"{src.stem}.bak{src.suffix}")
        try:
            import shutil
            shutil.copy2(src, backup)
            cleaned.replace(src)
            return src, (
                f"Исходный файл заменён очищенной базой: {src}\n"
                f"Резервная копия оригинала: {backup}\n"
            )
        except Exception as error:
            return cleaned, (
                f"Очищенный файл: {cleaned}\n"
                f"(не удалось заменить оригинал: {error})\n"
            )

    def _start_dedup_only(self) -> None:
        """Быстрое удаление дубликатов и синтаксически неправильных адресов (без интернета).

        Спрашивает: куда сохранить очищенную базу и куда выгрузить удалённые
        (дубли + неправильные). Мультилист: чистятся все листы книги.
        """
        import threading
        src_raw = self.to_file_var.get().strip()
        if not src_raw:
            messagebox.showwarning("Дубликаты", "Сначала выбери файл базы.")
            return
        src = Path(src_raw).expanduser().resolve()
        if not src.exists():
            messagebox.showerror("Дубликаты", f"Файл не найден: {src}")
            return
        col = (self.email_col_var.get() or "A").strip()
        try:
            start_row = int(self.validate_start_row_var.get() or "2")
        except ValueError:
            start_row = 2

        is_xlsx = src.suffix.lower() in {".xlsx", ".xlsm"}
        out_ext = src.suffix if is_xlsx else ".txt"
        clean_type = [("Excel", "*.xlsx"), ("Все файлы", "*.*")] if is_xlsx else [("Текст", "*.txt"), ("Все файлы", "*.*")]
        cleaned_raw = filedialog.asksaveasfilename(
            title="Сохранить очищенную базу как…",
            defaultextension=out_ext,
            initialfile=f"{src.stem}_очищенный{out_ext}",
            filetypes=clean_type,
        )
        if not cleaned_raw:
            return
        cleaned = Path(cleaned_raw).expanduser()
        if not cleaned.suffix:
            cleaned = cleaned.with_suffix(out_ext)
        removed_raw = filedialog.asksaveasfilename(
            title="Куда сохранить удалённые (дубли + неправильные)… (можно пропустить)",
            defaultextension=".txt",
            initialfile=f"{src.stem}_удалённые.txt",
            filetypes=[("Текст", "*.txt"), ("Все файлы", "*.*")],
        )
        removed_path = None
        if removed_raw:
            removed_path = Path(removed_raw).expanduser()
            if not removed_path.suffix:
                removed_path = removed_path.with_suffix(".txt")

        self.validate_log.configure(state="normal")
        self.validate_log.delete("1.0", "end")
        self.validate_log.configure(state="disabled")
        self.validate_progress_var.set("Убираю дубликаты и неправильные…")
        self.validate_start_btn.configure(state="disabled")
        self.validate_dedup_btn.configure(state="disabled")

        def worker() -> None:
            import email_validator as ev
            from email_validator import EMAIL_RE
            try:
                if is_xlsx:
                    removed_bad, removed_dup, removed = ev._write_xlsx_clean_dedup(
                        src, cleaned, set(), True, col, start_row, drop_bad_syntax=True
                    )
                    kept_n = -1  # для xlsx точное число оставшихся не считаем построчно
                else:
                    emails = ev._read_text_emails(src)
                    seen: set[str] = set()
                    kept: list[str] = []
                    removed = []
                    removed_bad = 0
                    removed_dup = 0
                    for raw in emails:
                        e = raw.strip()
                        key = e.lower()
                        if not key:
                            continue
                        if not EMAIL_RE.match(e):
                            removed.append(e)
                            removed_bad += 1
                            continue
                        if key in seen:
                            removed.append(e)
                            removed_dup += 1
                            continue
                        seen.add(key)
                        kept.append(e)
                    cleaned.parent.mkdir(parents=True, exist_ok=True)
                    cleaned.write_text("\n".join(kept), encoding="utf-8")
                    kept_n = len(kept)

                if removed_path is not None:
                    removed_path.parent.mkdir(parents=True, exist_ok=True)
                    removed_path.write_text("\n".join(removed), encoding="utf-8")

                removed_line = (
                    f"Файл удалённых: {removed_path}\n" if removed_path is not None else ""
                )
                kept_line = f"Осталось адресов: {kept_n}\n" if kept_n >= 0 else ""
                summary = (
                    f"\nГотово.\n{kept_line}"
                    f"Удалено неправильных: {removed_bad}, дубликатов: {removed_dup}\n"
                    f"Очищенная база: {cleaned}\n"
                    f"{removed_line}"
                )
                self.root.after(0, lambda: self._finish_validation(
                    f"Готово. Убрано неправильных {removed_bad}, дублей {removed_dup}.",
                    summary))
            except BaseException as exc:  # noqa: BLE001
                self.root.after(0, lambda e=exc: self._finish_validation(f"Ошибка: {e}", None))

        threading.Thread(target=worker, daemon=True).start()

    def _export_txt_dedup(self) -> None:
        """Оффлайн-экспорт: из базы (xlsx/txt) в .txt с email через запятую, без дублей."""
        import threading
        src_raw = self.to_file_var.get().strip()
        if not src_raw:
            messagebox.showwarning("Экспорт в .txt", "Сначала выбери файл базы.")
            return
        src = Path(src_raw).expanduser().resolve()
        if not src.exists():
            messagebox.showerror("Экспорт в .txt", f"Файл не найден: {src}")
            return
        col = (self.email_col_var.get() or "A").strip()
        try:
            start_row = int(self.validate_start_row_var.get() or "2")
        except ValueError:
            start_row = 2
        out_path = filedialog.asksaveasfilename(
            title="Сохранить email через запятую как…",
            defaultextension=".txt",
            initialfile=f"{src.stem}_email.txt",
            filetypes=[("Текст", "*.txt"), ("Все файлы", "*.*")],
        )
        if not out_path:
            return
        out = Path(out_path).expanduser()

        self.validate_log.configure(state="normal")
        self.validate_log.delete("1.0", "end")
        self.validate_log.configure(state="disabled")
        self.validate_progress_var.set("Готовлю .txt…")
        self.validate_txt_btn.configure(state="disabled")
        self.validate_dedup_btn.configure(state="disabled")

        def worker() -> None:
            import email_validator as ev
            try:
                if src.suffix.lower() in {".xlsx", ".xlsm"}:
                    emails, _ = ev._read_xlsx_emails(src, col, start_row)
                else:
                    emails = ev._read_text_emails(src)
                seen: set[str] = set()
                kept: list[str] = []
                removed = 0
                for e in emails:
                    key = e.strip().lower()
                    if not key:
                        continue
                    if key in seen:
                        removed += 1
                        continue
                    seen.add(key)
                    kept.append(e.strip())
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(", ".join(kept), encoding="utf-8")
                summary = (
                    f"\nГотово. Уникальных адресов: {len(kept)} (убрано дублей: {removed})\n"
                    f"Файл: {out}"
                )
                self.root.after(0, lambda: self._finish_txt_export(
                    f"Готово. {len(kept)} адресов в .txt (дублей убрано {removed}).", summary))
            except BaseException as exc:  # noqa: BLE001
                self.root.after(0, lambda e=exc: self._finish_txt_export(f"Ошибка: {e}", None))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_txt_export(self, status_text: str, summary: str | None) -> None:
        self.validate_progress_var.set(status_text)
        if summary:
            self._validate_log_append(summary)
        self.validate_txt_btn.configure(state="normal")
        self.validate_dedup_btn.configure(state="normal")

    def _start_cloud_clean(self) -> None:
        """Гоняет чистку базы (MX + дубликаты) на облачном сервере и качает результаты."""
        import threading
        src_raw = self.to_file_var.get().strip()
        if not src_raw:
            messagebox.showwarning("Очистка на облаке", "Сначала выбери файл базы.")
            return
        src = Path(src_raw).expanduser().resolve()
        if not src.exists():
            messagebox.showerror("Очистка на облаке", f"Файл не найден: {src}")
            return
        try:
            server_config = self._build_server_config()
        except Exception as error:
            messagebox.showerror("Очистка на облаке", str(error))
            return
        col = (self.email_col_var.get() or "A").strip()
        try:
            start_row = int(self.validate_start_row_var.get() or "2")
        except ValueError:
            start_row = 2
        dedup = bool(self.validate_dedup_var.get())

        self.validate_progress_var.set("Отправляю базу на сервер…")
        self.validate_cloud_btn.configure(state="disabled")
        self._append_log(f"\n[Cloud-clean] Старт облачной очистки: {src.name}\n")

        def worker() -> None:
            runtime = CloudRuntime(server_config, BASE_DIR)
            try:
                runtime.connect()
                self.log_queue.put("[Cloud-clean] Загрузка кода на сервер...\n")
                runtime.upload_project()
                remote_base = runtime.get_remote_base_dir().rstrip("/")
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                job_dir = f"{remote_base}/.clean_jobs/{stamp}"
                remote_in = f"{job_dir}/{src.name}"
                self.log_queue.put("[Cloud-clean] Заливаю базу на сервер...\n")
                runtime.upload_file(src, remote_in)
                out_ext = src.suffix if src.suffix.lower() in {".xlsx", ".xlsm"} else ".txt"
                remote_out = f"{job_dir}/{src.stem}_очищенный{out_ext}"
                remote_bad = f"{job_dir}/{src.stem}_невалидные.txt"
                remote_report = f"{job_dir}/{src.stem}_отчёт.csv"
                remote_txt = f"{job_dir}/{src.stem}_email.txt"
                argv = [
                    "python3", f"{remote_base}/email_validator.py",
                    "--in", remote_in,
                    "--email-col", col,
                    "--start-row", str(start_row),
                    "--out", remote_out,
                    "--bad", remote_bad,
                    "--report", remote_report,
                    "--txt-out", remote_txt,
                ]
                if dedup:
                    argv.append("--dedup")
                self.log_queue.put("[Cloud-clean] Запуск проверки на сервере (detached)...\n")
                task = runtime.start_remote_process_detached(argv)
                self.root.after(0, lambda: self.validate_progress_var.set("Проверка идёт на сервере…"))
                log_offset = 0
                while True:
                    chunk, log_offset = runtime.read_log_chunk(task["log_file"], log_offset)
                    if chunk:
                        for line in chunk.splitlines():
                            self.log_queue.put(f"[Cloud-clean] {line}\n")
                    if not runtime.is_remote_process_running(task["pid_file"]):
                        chunk, log_offset = runtime.read_log_chunk(task["log_file"], log_offset)
                        if chunk:
                            for line in chunk.splitlines():
                                self.log_queue.put(f"[Cloud-clean] {line}\n")
                        break
                    time.sleep(1.0)
                exit_code = runtime.read_exit_code(task["status_file"])
                self.root.after(0, lambda: self.validate_progress_var.set("Скачиваю результаты с сервера…"))
                downloaded = []
                for remote_path in (remote_out, remote_bad, remote_report, remote_txt):
                    local_dest = src.with_name(Path(remote_path).name)
                    try:
                        if runtime.download_file(remote_path, local_dest):
                            downloaded.append(str(local_dest))
                            self.log_queue.put(f"[Cloud-clean] Скачан: {local_dest}\n")
                    except Exception as error:
                        self.log_queue.put(f"[Cloud-clean] Не скачан {Path(remote_path).name}: {error}\n")
                code_view = "ok" if exit_code in (0, None) else f"код {exit_code}"
                files_txt = "\n".join(f"  • {p}" for p in downloaded) or "  (файлы не скачаны)"
                summary = (
                    f"\nОблачная очистка завершена ({code_view}). Скачано в папку базы:\n{files_txt}"
                )
                self.root.after(0, lambda: self._finish_cloud_clean(
                    f"Облако: готово ({code_view}), файлов скачано {len(downloaded)}.", summary))
            except Exception as error:
                self.root.after(0, lambda e=error: self._finish_cloud_clean(f"Ошибка облака: {e}", None))
            finally:
                try:
                    runtime.close()
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _finish_cloud_clean(self, status_text: str, summary: str | None) -> None:
        self.validate_progress_var.set(status_text)
        if summary:
            self._validate_log_append(summary)
        self.validate_cloud_btn.configure(state="normal")

    def _build_hub_controls(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        self.hub_url_var = tk.StringVar()
        self.hub_connection_id_var = tk.StringVar()
        self.hub_secret_var = tk.StringVar()
        self.hub_insecure_ssl_var = tk.BooleanVar(value=False)

        ttk.Label(frame, text="Hub URL:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(frame, textvariable=self.hub_url_var).grid(row=0, column=1, sticky="ew")
        ttk.Label(frame, text="Hub Conn ID:").grid(row=0, column=2, sticky="w", padx=(12, 8))
        ttk.Entry(frame, textvariable=self.hub_connection_id_var).grid(row=0, column=3, sticky="ew")
        ttk.Label(frame, text="Hub Secret:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.hub_secret_var, show="*").grid(
            row=1, column=1, columnspan=3, sticky="ew", pady=(8, 0)
        )

        ttk.Button(frame, text="Проверить Hub", command=self._check_hub_health).grid(
            row=2, column=0, sticky="w", pady=(10, 0)
        )
        ttk.Button(frame, text="Проверить пиксель", command=self._check_hub_pixel).grid(
            row=2, column=1, sticky="w", pady=(10, 0)
        )
        ttk.Button(frame, text="Сохранить настройки", command=self._save_config).grid(
            row=2, column=2, sticky="w", pady=(10, 0)
        )
        ttk.Checkbutton(
            frame,
            text="Игнорировать SSL ошибки (тест)",
            variable=self.hub_insecure_ssl_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(
            frame,
            text="Пиксель откртия письма работает только если заполнены все три поля.",
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))

    def _make_scrollable(self, parent: ttk.Frame) -> ttk.Frame:
        """Оборачивает содержимое в вертикально прокручиваемую область.

        Нужно вкладке «Облако» — иначе нижние блоки (параллельный запуск)
        не влезают в окно и кнопки уходят за край.
        """
        canvas = tk.Canvas(parent, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        def _on_inner_configure(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event) -> None:
            canvas.itemconfigure(window, width=event.width)

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_wheel(event) -> None:
            delta = event.delta
            if delta == 0:
                return
            canvas.yview_scroll(-1 if delta > 0 else 1, "units")

        inner.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        inner.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _build_cloud_controls(self, outer_frame: ttk.Frame) -> None:
        frame = self._make_scrollable(outer_frame)
        for idx in range(4):
            frame.columnconfigure(idx, weight=1 if idx in (1, 3) else 0)

        self.cloud_enabled_var = tk.BooleanVar(value=False)
        self.server_host_var = tk.StringVar()
        self.server_port_var = tk.StringVar(value="22")
        self.server_user_var = tk.StringVar()
        self.server_password_var = tk.StringVar()
        self.server_key_path_var = tk.StringVar()
        self.server_key_pass_var = tk.StringVar()
        self.server_remote_dir_var = tk.StringVar(value="~/mailinig-soft-cloud")
        self.cloud_status_var = tk.StringVar(value="Сервер не инициализирован")
        self.update_status_var = tk.StringVar(value="Обновления не проверялись")

        ttk.Checkbutton(
            frame,
            text="Выполнение в облаке",
            variable=self.cloud_enabled_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(frame, text="IP / домен:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.server_host_var).grid(row=1, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(frame, text="SSH порт:").grid(row=1, column=2, sticky="w", padx=(12, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.server_port_var).grid(row=1, column=3, sticky="ew", pady=(8, 0))

        ttk.Label(frame, text="SSH логин:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.server_user_var).grid(row=2, column=1, sticky="ew", pady=(8, 0))
        ttk.Label(frame, text="SSH пароль:").grid(row=2, column=2, sticky="w", padx=(12, 8), pady=(8, 0))
        server_pass_entry = ttk.Entry(frame, textvariable=self.server_password_var, show="*")
        server_pass_entry.grid(row=2, column=3, sticky="ew", pady=(8, 0))
        self._add_paste_support(server_pass_entry)

        key_frame = ttk.Frame(frame)
        key_frame.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        key_frame.columnconfigure(1, weight=1)
        ttk.Label(key_frame, text="SSH ключ (вместо пароля):").grid(row=0, column=0, sticky="w", padx=(0, 8))
        key_entry = ttk.Entry(key_frame, textvariable=self.server_key_path_var)
        key_entry.grid(row=0, column=1, sticky="ew")
        self._add_paste_support(key_entry)
        ttk.Button(key_frame, text="Выбрать ключ", command=self._pick_ssh_key).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(key_frame, text="Очистить", command=lambda: self.server_key_path_var.set("")).grid(row=0, column=3, padx=(6, 0))
        ttk.Label(key_frame, text="Пароль ключа:").grid(row=0, column=4, sticky="w", padx=(12, 8))
        key_pass_entry = ttk.Entry(key_frame, textvariable=self.server_key_pass_var, show="*", width=16)
        key_pass_entry.grid(row=0, column=5, sticky="w")
        self._add_paste_support(key_pass_entry)
        ttk.Label(
            key_frame,
            text="Если сервер пускает только по ключу — укажи файл ключа (id_rsa / .pem). Пароль можно оставить пустым.",
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))

        ttk.Label(frame, text="Папка на сервере:").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.server_remote_dir_var).grid(row=4, column=1, columnspan=3, sticky="ew", pady=(8, 0))

        run_frame = ttk.LabelFrame(frame, text="Запуск профиля в облаке", padding=8)
        run_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        run_frame.columnconfigure(1, weight=1)
        ttk.Label(run_frame, text="Профиль:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        cloud_profile_combo = ttk.Combobox(
            run_frame, textvariable=self.profile_var, state="readonly", width=26
        )
        cloud_profile_combo.grid(row=0, column=1, sticky="w")
        self._cloud_profile_combo = cloud_profile_combo
        cloud_profile_combo.bind("<<ComboboxSelected>>", lambda _e: self._load_selected_profile())
        ttk.Button(run_frame, text="Загрузить", command=self._load_selected_profile).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(
            run_frame, text="▶ Запустить в облаке", command=self._run_current_profile_in_cloud
        ).grid(row=0, column=3, padx=(8, 0))
        ttk.Label(
            run_frame,
            text="Выбери сессию → «Запустить в облаке». Счётчик суток и state сами подтянутся с сервера.",
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        actions = ttk.Frame(frame)
        actions.grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Button(actions, text="Инициализация сервера", command=self._initialize_server).grid(row=0, column=0)
        ttk.Button(actions, text="Проверить обновления", command=self._check_for_updates).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(actions, text="Обновить программу", command=self._apply_update).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(actions, text="Статус облачной задачи", command=self._check_cloud_task_status).grid(row=0, column=3, padx=(8, 0))

        ttk.Label(frame, text="Статус облака:").grid(row=7, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        ttk.Label(frame, textvariable=self.cloud_status_var).grid(row=7, column=1, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(frame, text="Статус обновлений:").grid(row=8, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Label(frame, textvariable=self.update_status_var).grid(row=8, column=1, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(
            frame,
            text="При облачном режиме задача продолжает выполняться на сервере даже если GUI закрыт.",
        ).grid(row=9, column=0, columnspan=4, sticky="w", pady=(8, 0))

        batch = ttk.LabelFrame(frame, text="Параллельный запуск нескольких профилей", padding=8)
        batch.grid(row=10, column=0, columnspan=4, sticky="nsew", pady=(12, 0))
        frame.rowconfigure(10, weight=1)
        batch.columnconfigure(0, weight=1)
        batch.columnconfigure(1, weight=2)
        batch.rowconfigure(1, weight=1)

        ttk.Label(
            batch,
            text="Выбери профили (Cmd/Shift-клик) и запусти их в облаке одновременно — "
                 "у каждого свой state, счётчик суток и SMTP-пул. Пароли должны быть сохранены в профиле.",
            wraplength=900,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        list_wrap = ttk.Frame(batch)
        list_wrap.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        list_wrap.rowconfigure(0, weight=1)
        list_wrap.columnconfigure(0, weight=1)
        self.cloud_batch_list = tk.Listbox(list_wrap, selectmode="extended", height=6, exportselection=False)
        self.cloud_batch_list.grid(row=0, column=0, sticky="nsew")
        list_scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.cloud_batch_list.yview)
        list_scroll.grid(row=0, column=1, sticky="ns")
        self.cloud_batch_list.configure(yscrollcommand=list_scroll.set)

        self.cloud_batch_tree = ttk.Treeview(
            batch, columns=("profile", "status", "detail"), show="headings", height=6
        )
        self.cloud_batch_tree.heading("profile", text="Профиль")
        self.cloud_batch_tree.heading("status", text="Статус")
        self.cloud_batch_tree.heading("detail", text="Прогресс")
        self.cloud_batch_tree.column("profile", width=140, anchor="w")
        self.cloud_batch_tree.column("status", width=90, anchor="w")
        self.cloud_batch_tree.column("detail", width=240, anchor="w")
        self.cloud_batch_tree.grid(row=1, column=1, sticky="nsew")

        batch_actions = ttk.Frame(batch)
        batch_actions.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(batch_actions, text="▶ Запустить выбранные параллельно", command=self._start_cloud_batch).grid(row=0, column=0)
        ttk.Button(batch_actions, text="⏹ Остановить все", command=self._stop_cloud_batch).grid(row=0, column=1, padx=(8, 0))

        self._refresh_cloud_batch_list()

    def _pick_ssh_key(self) -> None:
        # Ключи часто без расширения (id_rsa, EU) — фильтр по расширению их прячет,
        # поэтому показываем все файлы. По умолчанию открываем ~/.ssh.
        ssh_dir = Path("~/.ssh").expanduser()
        initial_dir = str(ssh_dir) if ssh_dir.exists() else str(Path.home())
        path = filedialog.askopenfilename(
            title="Выберите файл SSH-ключа (например ~/.ssh/EU, без .pub)",
            initialdir=initial_dir,
            filetypes=[("Все файлы", "*")],
        )
        if path:
            self.server_key_path_var.set(path)

    def _build_server_config(self) -> ServerConfig:
        host = self.server_host_var.get().strip()
        username = self.server_user_var.get().strip()
        password = self.server_password_var.get().strip()
        key_path = self.server_key_path_var.get().strip()
        key_passphrase = self.server_key_pass_var.get().strip()
        remote_dir = self.server_remote_dir_var.get().strip() or "~/mailinig-soft-cloud"
        port = self._safe_int(self.server_port_var.get().strip(), 22)
        if not host or not username:
            raise RuntimeError("Заполните IP/домен и SSH логин.")
        if not password and not key_path:
            raise RuntimeError("Укажите SSH пароль или файл SSH-ключа.")
        if key_path and not Path(key_path).expanduser().exists():
            raise RuntimeError(f"Файл ключа не найден: {key_path}")
        if port <= 0:
            raise RuntimeError("SSH порт должен быть положительным числом.")
        return ServerConfig(
            host=host,
            port=port,
            username=username,
            password=password,
            remote_dir=remote_dir,
            key_path=key_path,
            key_passphrase=key_passphrase,
        )

    def _initialize_server(self) -> None:
        if self.server_init_in_progress:
            messagebox.showwarning("Инициализация сервера", "Инициализация уже выполняется.")
            return
        try:
            server_config = self._build_server_config()
        except Exception as error:
            messagebox.showerror("Инициализация сервера", str(error))
            return

        self.server_init_in_progress = True
        self._append_log("\n[Cloud] Подключение к серверу и инициализация...\n")
        self.cloud_status_var.set("Инициализация...")
        self.status_var.set("Инициализация сервера...")

        def worker() -> None:
            runtime = CloudRuntime(server_config, BASE_DIR)
            try:
                self.log_queue.put("[Cloud] Шаг 1/4: подключение по SSH...\n")
                runtime.connect()
                self.log_queue.put("[Cloud] Шаг 2/4: загрузка файлов на сервер...\n")

                last_reported_index = 0

                def upload_progress(index: int, total: int, relative: str) -> None:
                    nonlocal last_reported_index
                    if index == 1 or index == total or index - last_reported_index >= 15:
                        last_reported_index = index
                        self.log_queue.put(
                            f"[Cloud] Загрузка: {index}/{total} ({relative})\n"
                        )

                uploaded = runtime.upload_project(on_progress=upload_progress)

                self.log_queue.put("[Cloud] Шаг 3/4: установка зависимостей на сервере...\n")

                def step_progress(label: str) -> None:
                    self.log_queue.put(f"[Cloud] {label}...\n")

                init_output = runtime.initialize_server(on_step=step_progress)
                self.cloud_runtime = runtime
                self.log_queue.put(f"[Cloud] Загружено файлов: {len(uploaded)}\n")
                if init_output:
                    trimmed = init_output
                    if len(trimmed) > 3500:
                        trimmed = trimmed[-3500:]
                        self.log_queue.put("[Cloud] Вывод инициализации сокращен (показан хвост).\n")
                    self.log_queue.put(f"[Cloud] Вывод инициализации:\n{trimmed}\n")
                self.log_queue.put("[Cloud] Шаг 4/4: сервер готов.\n")
                self.log_queue.put("[Cloud] Сервер инициализирован.\n")
                self._set_cloud_status_async("Сервер готов к облачному выполнению")
            except Exception as error:
                runtime.close()
                self.log_queue.put(f"[Cloud] Ошибка инициализации: {error}\n")
                self._set_cloud_status_async("Ошибка инициализации")
            finally:
                self.server_init_in_progress = False
                self._set_status_async("Ожидание")

        threading.Thread(target=worker, daemon=True).start()

    def _check_for_updates(self) -> None:
        self.update_status_var.set("Проверка обновлений...")
        self.status_var.set("Проверка обновлений...")

        def worker() -> None:
            try:
                info = check_for_updates(BASE_DIR)
                if info.has_update:
                    message = f"Доступно обновление: {info.remote_commit} (локально: {info.local_commit})"
                else:
                    message = f"Обновление не требуется, текущий коммит: {info.local_commit}"
                self.log_queue.put(f"[Update] {message}\n")
                self._set_update_status_async(message)
            except Exception as error:
                self.log_queue.put(f"[Update] Ошибка проверки: {error}\n")
                self._set_update_status_async("Ошибка проверки обновлений")
            finally:
                self._set_status_async("Ожидание")

        threading.Thread(target=worker, daemon=True).start()

    def _apply_update(self) -> None:
        if self.server_init_in_progress:
            messagebox.showwarning("Обновление", "Дождитесь завершения инициализации сервера.")
            return
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Обновление", "Сначала остановите текущую задачу.")
            return

        self.update_status_var.set("Установка обновления...")
        self.status_var.set("Установка обновления...")
        self._append_log("\n[Update] Запуск обновления программы...\n")

        def worker() -> None:
            try:
                commit = apply_update(BASE_DIR)
                self.log_queue.put(f"[Update] Обновление установлено. Коммит: {commit}\n")
                self._set_update_status_async(f"Установлено обновление: {commit}")
                self.root.after(0, lambda c=commit: self._handle_update_installed(c))
            except Exception as error:
                self.log_queue.put(f"[Update] Ошибка обновления: {error}\n")
                self._set_update_status_async("Ошибка обновления")
            finally:
                self._set_status_async("Ожидание")

        threading.Thread(target=worker, daemon=True).start()

    def _handle_update_installed(self, commit: str) -> None:
        if self.settings_dirty:
            answer = messagebox.askyesnocancel(
                "Сохранить настройки",
                "Есть несохраненные настройки. Сохранить перед перезапуском?",
            )
            if answer is None:
                self._append_log("[Update] Перезапуск отменен пользователем.\n")
                return
            if answer:
                self._save_config()
        should_restart = messagebox.askyesno(
            "Обновление установлено",
            f"Установлен коммит {commit}. Перезапустить программу сейчас?",
        )
        if should_restart:
            self._restart_application()
        else:
            self._append_log("[Update] Перезапуск отложен.\n")

    def _restart_application(self) -> None:
        if self.server_init_in_progress:
            messagebox.showwarning("Перезапуск", "Нельзя перезапускать программу во время инициализации сервера.")
            return
        self._append_log("[Update] Перезапуск приложения...\n")
        python = sys.executable
        args = [python, *sys.argv]
        subprocess.Popen(args, cwd=str(BASE_DIR))
        self.root.after(100, self.root.destroy)

    def _build_status_bar(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, weight=1)

        self.status_var = tk.StringVar(value="Ожидание")
        self.progress_text_var = tk.StringVar(value="Прогресс: 0/0")
        ttk.Label(frame, text="Статус:").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, textvariable=self.status_var).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(frame, textvariable=self.progress_text_var).grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.progress_bar = ttk.Progressbar(frame, mode="indeterminate")
        self.progress_bar.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))

    def _pick_template(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите HTML шаблон",
            filetypes=[("HTML", "*.html *.htm"), ("Все файлы", "*.*")],
        )
        if path:
            self.template_var.set(path)
            self._refresh_state_info()

    def _pick_excel(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите файл базы",
            filetypes=[("Excel/CSV/TXT", "*.xlsx *.csv *.txt"), ("Все файлы", "*.*")],
        )
        if path:
            self.to_file_var.set(path)
            if self.auto_template_var.get() and not self.template_var.get().strip():
                self._auto_pick_template(silent=True)
            self._refresh_state_info()

    def _pick_state_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Выберите state-файл",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")],
        )
        if path:
            self.state_file_var.set(path)
            self._refresh_state_info()

    def _auto_pick_to_file(self, silent: bool = False) -> None:
        preferred = sorted(BASE_DIR.glob("База*.xlsx"))
        candidates = preferred or sorted(BASE_DIR.glob("*.xlsx"))
        if not candidates:
            return
        self.to_file_var.set(str(candidates[0].resolve()))
        if not silent:
            self._append_log(f"\nАвтоподбор базы: {candidates[0].resolve()}\n")

    def _guess_template_path(self) -> Path | None:
        mapping = {
            "нефтегаз": BASE_DIR / "Нефтегаз" / "landing_oil_gas.html",
            "машиностро": BASE_DIR / "Машиностроение" / "landing_factories_cooling.html",
            "торгов": BASE_DIR / "Торговые компании" / "landing_trading_companies.html",
            "пищев": BASE_DIR / "Пищевые компании" / "landing_food_industry_unsubscribe_updated.html",
            "морск": BASE_DIR / "Морские теплообменники" / "landing_food_industry_unsubscribe_2_updated.html",
            "гвс": BASE_DIR / "ГВС и отопление" / "landing_gvs_otoplenie.html",
            "отоплен": BASE_DIR / "ГВС и отопление" / "landing_gvs_otoplenie.html",
            "проект": BASE_DIR / "Проектные организации" / "landing_project_orgs.html",
            "сахар": BASE_DIR / "Сахарные заводы" / "landing_sugar_plants.html",
        }
        context_text = f"{self.to_file_var.get()} {self.sheet_var.get()}"
        context = context_text.lower()
        for key, candidate in mapping.items():
            if key in context and candidate.exists():
                return candidate

        for candidate in mapping.values():
            if candidate.exists():
                return candidate
        return None

    def _auto_pick_template(self, silent: bool = False) -> None:
        guessed = self._guess_template_path()
        if guessed is None:
            if not silent:
                messagebox.showwarning("Автоподбор", "Не нашёл HTML шаблон для автоподбора.")
            return
        self.template_var.set(str(guessed))
        self._refresh_state_info()
        if not silent:
            self._append_log(f"\nАвтоподбор шаблона: {guessed}\n")

    def _safe_int(self, value: str, default: int = 0) -> int:
        try:
            return int(str(value).strip())
        except Exception:
            return default

    def _get_state_file_path(self) -> Path:
        raw = self.state_file_var.get().strip() or ".send_email_state.json"
        return Path(raw).expanduser().resolve()

    def _get_template_for_state(self) -> Path:
        template = self.template_var.get().strip()
        if not template and self.auto_template_var.get():
            guessed = self._guess_template_path()
            if guessed is not None:
                template = str(guessed)
        if not template:
            return Path("").resolve()
        return Path(template).expanduser().resolve()

    def _build_campaign_key_for_ui(self) -> str:
        template_path = self._get_template_for_state()
        raw = "|".join(
            [
                str(Path(self.to_file_var.get().strip()).expanduser().resolve()) if self.to_file_var.get().strip() else "",
                self.sheet_var.get().strip() or "active",
                self.email_col_var.get().strip() or "G",
                (self.kind_col_var.get().strip() or "P") if self.use_kind_template_var.get() else "",
                self.kind_filter_var.get().strip() or "ALL",
                str(self._safe_int(self.start_row_var.get().strip() or "2", 2)),
                self.fields_var.get().strip(),
                str(self.allow_duplicate_emails_var.get()),
                str(template_path),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _load_state_json(self) -> dict:
        path = self._get_state_file_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _refresh_state_info(self) -> None:
        campaign_key = self._build_campaign_key_for_ui()
        data = self._load_state_json()
        self.state_campaign_key_var.set(campaign_key)
        today = date.today().isoformat()
        stored_date = str(data.get("date", "")).strip()
        # Суточный счётчик обнуляется при смене даты (так делает send_email.py),
        # поэтому в новый день показываем 0, а не «зависшее» вчерашнее число.
        if stored_date and stored_date != today:
            self.state_date_var.set(f"{today} (в файле {stored_date} → сброс)")
            self.state_sent_today_var.set("0")
        else:
            self.state_date_var.set(stored_date or "—")
            self.state_sent_today_var.set(str(self._safe_int(str(data.get("sent_today", 0)), 0)))
        campaigns = data.get("campaigns", {})
        campaign = campaigns.get(campaign_key, {}) if isinstance(campaigns, dict) else {}
        cursor_value = self._safe_int(str(campaign.get("cursor_index", 0)), 0)
        last_row_value = self._safe_int(str(campaign.get("last_row", 0)), 0)
        self.state_cursor_var.set(str(cursor_value))
        self.state_last_row_var.set(str(last_row_value))
        if last_row_value > 0:
            self.resume_from_row_var.set(str(last_row_value + 1))
        else:
            self.resume_from_row_var.set(self.start_row_var.get().strip() or "2")

    def _save_state_json(self, data: dict) -> None:
        path = self._get_state_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _reset_campaign_state(self) -> None:
        campaign_key = self._build_campaign_key_for_ui()
        data = self._load_state_json()
        if not isinstance(data, dict):
            data = {}
        campaigns = data.get("campaigns")
        if not isinstance(campaigns, dict):
            campaigns = {}
            data["campaigns"] = campaigns
        campaigns[campaign_key] = {"cursor_index": 0, "last_row": 0}
        if "date" not in data:
            data["date"] = date.today().isoformat()
        if "sent_today" not in data:
            data["sent_today"] = 0
        self._save_state_json(data)
        self._refresh_state_info()
        self._append_log(f"\nState: курсор кампании сброшен ({campaign_key}).\n")

    def _apply_resume_from_row(self) -> None:
        row = self._safe_int(self.resume_from_row_var.get().strip(), 0)
        if row < 1:
            messagebox.showerror("Начать со строки", "Укажите строку >= 1.")
            return
        self.start_row_var.set(str(row))
        campaign_key = self._build_campaign_key_for_ui()
        data = self._load_state_json()
        if not isinstance(data, dict):
            data = {}
        campaigns = data.get("campaigns")
        if not isinstance(campaigns, dict):
            campaigns = {}
            data["campaigns"] = campaigns
        campaigns[campaign_key] = {"cursor_index": 0, "last_row": max(row - 1, 0)}
        if "date" not in data:
            data["date"] = date.today().isoformat()
        if "sent_today" not in data:
            data["sent_today"] = 0
        self._save_state_json(data)
        self._refresh_state_info()
        self._append_log(f"\nState: начало отправки выставлено со строки {row}.\n")

    def _reset_daily_state(self) -> None:
        data = self._load_state_json()
        if not isinstance(data, dict):
            data = {}
        data["date"] = date.today().isoformat()
        data["sent_today"] = 0
        if "campaigns" not in data or not isinstance(data.get("campaigns"), dict):
            data["campaigns"] = {}
        self._save_state_json(data)
        self._refresh_state_info()
        self._append_log("\nState: суточный счётчик сброшен.\n")

    def _append_log(self, text: str) -> None:
        self.log_text.insert("end", text)
        if self.log_text.yview()[1] > 0.98:
            self.log_text.see("end")
        if self.current_log_handle is not None:
            try:
                self.current_log_handle.write(text)
                self.current_log_handle.flush()
            except Exception:
                pass

    def _append_log_batch(self, lines: list[str]) -> None:
        if not lines:
            return
        text = "".join(lines)
        at_bottom = self.log_text.yview()[1] > 0.98
        self.log_text.insert("end", text)
        if at_bottom:
            self.log_text.see("end")
        if self.current_log_handle is not None:
            try:
                self.current_log_handle.write(text)
                self.current_log_handle.flush()
            except Exception:
                pass

    def _poll_logs(self) -> None:
        processed = 0
        max_per_tick = 160
        lines: list[str] = []
        try:
            while processed < max_per_tick:
                line = self.log_queue.get_nowait()
                lines.append(line)
                self._handle_progress_line(line)
                processed += 1
        except queue.Empty:
            pass
        self._append_log_batch(lines)
        next_delay_ms = 20 if processed >= max_per_tick else 120
        self.root.after(next_delay_ms, self._poll_logs)

    def _reset_progress_ui(self) -> None:
        self.progress_total = 0
        self.progress_sent = 0
        self.progress_failed = 0
        self.progress_skipped = 0
        self.progress_running = False
        self.progress_bar.stop()
        self.progress_bar.configure(mode="indeterminate", maximum=100, value=0)
        self.progress_text_var.set("Прогресс: 0/0")

    def _start_progress_ui(self) -> None:
        self._reset_progress_ui()
        self.progress_running = True
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(12)
        self.progress_text_var.set("Прогресс: подготовка...")

    def _apply_progress_ui(self) -> None:
        if self.progress_total > 0:
            current = min(self.progress_sent + self.progress_failed + self.progress_skipped, self.progress_total)
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", maximum=self.progress_total, value=current)
            self.progress_text_var.set(
                f"Прогресс: {current}/{self.progress_total} (ok:{self.progress_sent} err:{self.progress_failed} skip:{self.progress_skipped})"
            )
        else:
            if self.progress_running:
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start(12)
                self.progress_text_var.set(f"Прогресс: ok:{self.progress_sent} err:{self.progress_failed}")

    def _finalize_progress_ui(self) -> None:
        self.progress_running = False
        self.progress_bar.stop()
        if self.progress_total > 0:
            final_value = min(self.progress_sent + self.progress_failed + self.progress_skipped, self.progress_total)
            self.progress_bar.configure(mode="determinate", maximum=self.progress_total, value=final_value)
            self.progress_text_var.set(
                f"Готово: {final_value}/{self.progress_total} (ok:{self.progress_sent} err:{self.progress_failed} skip:{self.progress_skipped})"
            )
        else:
            self.progress_bar.configure(mode="determinate", maximum=1, value=1 if (self.progress_sent or self.progress_failed) else 0)
            self.progress_text_var.set(f"Готово: ok:{self.progress_sent} err:{self.progress_failed}")

    def _handle_progress_line(self, line: str) -> None:
        total_match = re.search(r"Всего получателей \(осталось\):\s*(\d+)", line)
        if total_match:
            self.progress_total = int(total_match.group(1))
            self._apply_progress_ui()
            return

        sent_match = re.search(r"^\[(\d+)\]\s+Отправлено:", line.strip())
        if sent_match:
            self.progress_sent = max(self.progress_sent, int(sent_match.group(1)))
            self._apply_progress_ui()
            return

        if "[ERR] Не отправлено:" in line:
            self.progress_failed += 1
            self._apply_progress_ui()
            return

        final_sent_match = re.search(r"Готово\.\s*Отправлено:\s*(\d+)\.", line)
        if final_sent_match:
            self.progress_sent = max(self.progress_sent, int(final_sent_match.group(1)))
            self._apply_progress_ui()
            return

        skipped_match = re.search(r"Пропущено из-за суточного лимита:\s*(\d+)", line)
        if skipped_match:
            self.progress_skipped = int(skipped_match.group(1))
            self._apply_progress_ui()
            return

        if "✅ Успешно завершено." in line or "❌ Завершено с ошибкой" in line:
            self._finalize_progress_ui()
            return

    def _apply_progress_payload(self, payload: dict) -> None:
        total = self._safe_int(str(payload.get("total", 0)), 0)
        sent = self._safe_int(str(payload.get("sent", 0)), 0)
        failed = self._safe_int(str(payload.get("failed", 0)), 0)
        skipped = self._safe_int(str(payload.get("skipped", 0)), 0)
        processed = self._safe_int(str(payload.get("processed", sent + failed + skipped)), sent + failed + skipped)
        self.progress_total = total
        self.progress_sent = sent
        self.progress_failed = failed
        self.progress_skipped = skipped
        if total > 0:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", maximum=total, value=min(processed, total))
            percent = payload.get("percent")
            percent_view = f"{percent}%" if percent not in (None, "") else f"{round(processed / total * 100, 2)}%"
            account = str(payload.get("current_account", "")).strip()
            suffix = f" | {account}" if account else ""
            self.progress_text_var.set(
                f"Прогресс: {processed}/{total} ({percent_view}) ok:{sent} err:{failed} skip:{skipped}{suffix}"
            )
        else:
            self._apply_progress_ui()
        status = str(payload.get("status", "")).lower()
        if status in {"completed", "stopped", "failed"}:
            self._finalize_progress_ui()

    def _pull_cloud_state(self, runtime: CloudRuntime, cmd: list[str], remote_cmd: list[str]) -> None:
        """Скачивает state-файл (и progress) с сервера обратно в локальные пути.

        Без этого GUI после облачного прогона показывал устаревший локальный state.
        """
        local_state = self._extract_flag_value(cmd, "--state-file")
        remote_state = self._extract_flag_value(remote_cmd, "--state-file")
        if not local_state or not remote_state:
            return
        try:
            local_path = Path(local_state).expanduser().resolve()
            pulled = runtime.download_file(remote_state, local_path)
            if pulled:
                self.log_queue.put(f"[Cloud] State синхронизирован с сервера → {local_path}\n")
                self.root.after(0, self._refresh_state_info)
            else:
                self.log_queue.put("[Cloud] State-файл на сервере не найден (возможно, ещё не создан).\n")
        except Exception as error:
            self.log_queue.put(f"[Cloud] Не удалось скачать state: {error}\n")

    def _read_remote_progress_payload(self, runtime: CloudRuntime, progress_file: str) -> dict | None:
        if not progress_file:
            return None
        try:
            raw = runtime.download_text_file(progress_file)
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _extract_flag_value(self, cmd: list[str], flag: str) -> str:
        try:
            index = cmd.index(flag)
        except ValueError:
            return ""
        if index + 1 >= len(cmd):
            return ""
        return cmd[index + 1]

    def _settings_to_command(self, s: dict, *, force_dry_run: bool, progress_file: str) -> list[str]:
        """Собирает argv send_email.py из словаря настроек профиля (для параллельного облака).

        Не читает живые поля UI — работает с сохранённым профилем, поэтому
        несколько профилей можно запускать одновременно без гонки за self.*_var.
        """
        template = str(s.get("template", "")).strip()
        if not template and bool(s.get("auto_template", True)):
            guessed = self._guess_template_path()
            if guessed is not None:
                template = str(guessed)
        if not template:
            raise RuntimeError("нет HTML-шаблона в профиле")
        to_file = str(s.get("to_file", "")).strip()
        if not to_file:
            raise RuntimeError("не указана база получателей в профиле")

        raw_accounts = s.get("smtp_accounts", [])
        active_accounts = [
            a for a in raw_accounts
            if isinstance(a, dict) and a.get("enabled", True)
            and str(a.get("user", "")).strip() and str(a.get("password", "")).strip()
        ] if isinstance(raw_accounts, list) else []
        single_password = str(s.get("smtp_password", "")).strip()
        if not active_accounts and not single_password:
            raise RuntimeError("нет пароля SMTP (вкл. «Запомнить пароль» и пересохрани профиль)")

        cmd = [
            "python3", str(SCRIPT_PATH),
            "--template", template,
            "--smtp-host", str(s.get("smtp_host", "")).strip() or "smtp.timeweb.ru",
            "--smtp-port", str(s.get("smtp_port", "")).strip() or "465",
            "--smtp-user", str(s.get("smtp_user", "")).strip(),
            "--smtp-password", single_password,
            "--from-email", str(s.get("from_email", "")).strip() or str(s.get("smtp_user", "")).strip(),
            "--xlsx-sheet", str(s.get("sheet", "")).strip() or "active",
            "--xlsx-email-col", str(s.get("email_col", "")).strip() or "G",
            "--xlsx-start-row", str(s.get("start_row", "")).strip() or "2",
        ]
        if bool(s.get("use_kind_template", False)):
            cmd.extend(["--xlsx-kind-col", str(s.get("kind_col", "")).strip() or "P"])
        kind_filter = str(s.get("kind_filter", "")).strip()
        if kind_filter:
            cmd.extend(["--xlsx-kind-filter", kind_filter])
        hub_url = str(s.get("hub_url", "")).strip()
        cid = str(s.get("hub_connection_id", "")).strip()
        secret = str(s.get("hub_secret", "")).strip()
        if hub_url and cid and secret:
            cmd.extend(["--hub-url", hub_url, "--hub-connection-id", cid, "--hub-module-secret", secret])
            if bool(s.get("hub_insecure_ssl", False)):
                cmd.append("--hub-insecure-ssl")
        if bool(s.get("allow_duplicate_emails", False)):
            cmd.append("--allow-duplicate-emails")
        subject = str(s.get("subject", "")).strip()
        if subject:
            cmd.extend(["--subject", subject])
        fields = str(s.get("fields", "")).strip()
        if fields:
            cmd.extend(["--xlsx-fields", fields])
        limit_min = str(s.get("limit_min", "")).strip()
        if limit_min:
            cmd.extend(["--limit-per-minute", limit_min])
        limit_day = str(s.get("limit_day", "")).strip()
        if limit_day:
            cmd.extend(["--limit-per-day", limit_day])
        cmd.extend(["--to-file", to_file])
        state_file = str(s.get("state_file", "")).strip()
        if state_file:
            cmd.extend(["--state-file", state_file])

        if active_accounts:
            cleaned: list[str] = []
            skip_next = False
            single_flags = {"--smtp-host", "--smtp-port", "--smtp-user", "--smtp-password", "--from-email"}
            for token in cmd:
                if skip_next:
                    skip_next = False
                    continue
                if token in single_flags:
                    skip_next = True
                    continue
                cleaned.append(token)
            cmd = cleaned
            for account in active_accounts[:5]:
                payload = {
                    "label": str(account.get("label", "")).strip(),
                    "host": str(account.get("host", "smtp.timeweb.ru")).strip() or "smtp.timeweb.ru",
                    "port": int(self._safe_int(str(account.get("port", "465")), 465)),
                    "user": str(account.get("user", "")).strip(),
                    "password": str(account.get("password", "")).strip(),
                    "from_email": str(account.get("from_email", "")).strip() or str(account.get("user", "")).strip(),
                    "daily_limit": int(self._safe_int(str(account.get("daily_limit", "2000")), 2000)),
                }
                cmd.extend(["--smtp-account", json.dumps(payload, ensure_ascii=False)])

        if progress_file:
            cmd.extend(["--progress-file", progress_file])
        if force_dry_run:
            cmd.append("--dry-run")
        return cmd

    def _collect_command(
        self,
        force_dry_run: bool | None = None,
        override_to: list[str] | None = None,
        use_to_file: bool = True,
    ) -> tuple[list[str], Path | None]:
        if not SCRIPT_PATH.exists():
            raise RuntimeError(f"Не найден скрипт: {SCRIPT_PATH}")

        template = self.template_var.get().strip()
        guessed_template: Path | None = None
        if not template and self.auto_template_var.get():
            guessed_template = self._guess_template_path()
            if guessed_template is not None:
                template = str(guessed_template)
        if not template:
            raise RuntimeError("Не найден HTML шаблон. Проверьте папки с шаблонами.")
        to_file = self.to_file_var.get().strip()
        extra_to = self.extra_to_var.get().strip()
        manual_to = []
        if override_to:
            manual_to.extend([item.strip() for item in override_to if item.strip()])
        if not override_to and extra_to:
            manual_to.extend([item.strip() for item in extra_to.split(",") if item.strip()])

        if not (use_to_file and to_file) and not manual_to:
            raise RuntimeError("Укажите файл базы или доп. email получателей.")

        cmd = [
            "python3",
            str(SCRIPT_PATH),
            "--template",
            template,
            "--smtp-host",
            self.smtp_host_var.get().strip() or "smtp.timeweb.ru",
            "--smtp-port",
            self.smtp_port_var.get().strip() or "465",
            "--smtp-user",
            self.smtp_user_var.get().strip(),
            "--smtp-password",
            self.smtp_pass_var.get().strip(),
            "--from-email",
            self.from_email_var.get().strip() or self.smtp_user_var.get().strip(),
            "--xlsx-sheet",
            self.sheet_var.get().strip() or "active",
            "--xlsx-email-col",
            self.email_col_var.get().strip() or "G",
            "--xlsx-start-row",
            self.start_row_var.get().strip() or "2",
        ]
        if self.use_kind_template_var.get():
            cmd.extend(["--xlsx-kind-col", self.kind_col_var.get().strip() or "P"])
        kind_filter = self.kind_filter_var.get().strip()
        if kind_filter:
            cmd.extend(["--xlsx-kind-filter", kind_filter])
        hub_url = self.hub_url_var.get().strip()
        hub_connection_id = self.hub_connection_id_var.get().strip()
        hub_secret = self.hub_secret_var.get().strip()
        if hub_url and hub_connection_id and hub_secret:
            cmd.extend(["--hub-url", hub_url, "--hub-connection-id", hub_connection_id, "--hub-module-secret", hub_secret])
            if self.hub_insecure_ssl_var.get():
                cmd.append("--hub-insecure-ssl")
        if self.allow_duplicate_emails_var.get():
            cmd.append("--allow-duplicate-emails")

        subject = self.subject_var.get().strip()
        if subject:
            cmd.extend(["--subject", subject])

        fields = self.fields_var.get().strip()
        if fields:
            cmd.extend(["--xlsx-fields", fields])

        limit_min = self.limit_min_var.get().strip()
        if limit_min:
            cmd.extend(["--limit-per-minute", limit_min])

        limit_day = self.limit_day_var.get().strip()
        if limit_day:
            cmd.extend(["--limit-per-day", limit_day])

        if use_to_file and to_file:
            cmd.extend(["--to-file", to_file])
        state_file = self.state_file_var.get().strip()
        if state_file:
            cmd.extend(["--state-file", state_file])

        for email in manual_to:
            cmd.extend(["--to", email])

        active_accounts = [
            item
            for item in self.smtp_accounts
            if item.get("enabled", True) and str(item.get("user", "")).strip() and str(item.get("password", "")).strip()
        ]
        if active_accounts:
            cleaned: list[str] = []
            skip_next = False
            single_flags = {"--smtp-host", "--smtp-port", "--smtp-user", "--smtp-password", "--from-email"}
            for token in cmd:
                if skip_next:
                    skip_next = False
                    continue
                if token in single_flags:
                    skip_next = True
                    continue
                cleaned.append(token)
            cmd = cleaned
            for account in active_accounts[:5]:
                payload = {
                    "label": str(account.get("label", "")).strip(),
                    "host": str(account.get("host", "smtp.timeweb.ru")).strip() or "smtp.timeweb.ru",
                    "port": int(self._safe_int(str(account.get("port", "465")), 465)),
                    "user": str(account.get("user", "")).strip(),
                    "password": str(account.get("password", "")).strip(),
                    "from_email": str(account.get("from_email", "")).strip() or str(account.get("user", "")).strip(),
                    "daily_limit": int(self._safe_int(str(account.get("daily_limit", "2000")), 2000)),
                }
                cmd.extend(["--smtp-account", json.dumps(payload, ensure_ascii=False)])

        dry_run = self.dry_run_var.get() if force_dry_run is None else force_dry_run
        if dry_run:
            cmd.append("--dry-run")

        return cmd, guessed_template

    def _sanitize_cmd_for_log(self, cmd: list[str]) -> str:
        safe = []
        mask_next = False
        for token in cmd:
            if mask_next:
                safe.append("********")
                mask_next = False
                continue
            safe.append(token)
            if token in {"--smtp-password", "--smtp-account"}:
                mask_next = True
        return " ".join(safe)

    def _build_remote_command(self, cmd: list[str], remote_base_dir: str) -> list[str]:
        remote_cmd: list[str] = []
        remote_base = remote_base_dir.rstrip("/")
        path_flags = {"--template", "--to-file", "--state-file", "--progress-file"}
        previous_flag = ""

        def map_local_path(raw: str) -> str:
            path = Path(raw).expanduser()
            resolved = path.resolve()
            if resolved == SCRIPT_PATH:
                return f"{remote_base}/send_email.py"
            if resolved.is_relative_to(BASE_DIR):
                relative = resolved.relative_to(BASE_DIR).as_posix()
                return f"{remote_base}/{relative}"
            return raw

        for index, token in enumerate(cmd):
            if index == 0 and token == "python3":
                remote_cmd.append("python3")
                previous_flag = ""
                continue
            if index == 1:
                remote_cmd.append(map_local_path(token))
                previous_flag = ""
                continue
            if token.startswith("--"):
                remote_cmd.append(token)
                previous_flag = token
                continue
            if previous_flag in path_flags:
                remote_cmd.append(map_local_path(token))
            else:
                remote_cmd.append(token)
            previous_flag = ""
        return remote_cmd

    def _ensure_cloud_runtime(self) -> CloudRuntime:
        if self.cloud_runtime is not None:
            return self.cloud_runtime
        runtime = CloudRuntime(self._build_server_config(), BASE_DIR)
        runtime.connect()
        self.cloud_runtime = runtime
        return runtime

    def _tail_remote_log(self, runtime: CloudRuntime, log_file: str, max_bytes: int = 4000) -> str:
        command = f"if [ -f {shlex.quote(log_file)} ]; then tail -c {max_bytes} {shlex.quote(log_file)}; fi"
        code, out, err = runtime.exec(command, timeout=30)
        if code != 0:
            return (out + "\n" + err).strip()
        return out

    def _check_cloud_task_status(self) -> None:
        task = self.remote_task_meta
        if not task:
            messagebox.showinfo("Облачная задача", "Активная облачная задача не найдена в текущей сессии.")
            return
        self.status_var.set("Проверка статуса облачной задачи...")
        self._append_log("\n[Cloud] Проверка статуса задачи...\n")

        def worker() -> None:
            try:
                runtime = self._ensure_cloud_runtime()
                running = runtime.is_remote_process_running(task["pid_file"])
                exit_code = runtime.read_exit_code(task["status_file"])
                payload = self._read_remote_progress_payload(runtime, task.get("progress_file", ""))
                if payload:
                    self.root.after(0, lambda p=payload: self._apply_progress_payload(p))
                    processed = payload.get("processed", 0)
                    total = payload.get("total", 0)
                    percent = payload.get("percent", 0)
                    account = payload.get("current_account", "")
                    self.log_queue.put(
                        f"[Cloud] Progress: {processed}/{total} ({percent}%) {account}\n"
                    )
                tail_text = self._tail_remote_log(runtime, task["log_file"])
                if tail_text.strip():
                    self.log_queue.put(f"[Cloud] Хвост удаленного лога:\n{tail_text}\n")
                if running:
                    self.log_queue.put("[Cloud] Задача все еще выполняется на сервере.\n")
                    self._set_status_async("Облачная задача выполняется")
                else:
                    code_view = "unknown" if exit_code is None else str(exit_code)
                    self.log_queue.put(f"[Cloud] Задача завершена. Код: {code_view}\n")
                    remote_state = task.get("remote_state_file", "")
                    local_state = task.get("local_state_file", "")
                    if remote_state and local_state:
                        try:
                            local_path = Path(local_state).expanduser().resolve()
                            if runtime.download_file(remote_state, local_path):
                                self.log_queue.put(
                                    f"[Cloud] State синхронизирован с сервера → {local_path}\n"
                                )
                                self.root.after(0, self._refresh_state_info)
                        except Exception as error:
                            self.log_queue.put(f"[Cloud] Не удалось скачать state: {error}\n")
                    self._set_status_async("Облачная задача завершена")
                    self.remote_run_id = None
                    self.remote_task_meta = None
                    self._persist_cloud_last_task()
            except Exception as error:
                self.log_queue.put(f"[Cloud] Ошибка проверки статуса: {error}\n")
                self._set_status_async("Ошибка проверки статуса")
            finally:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # ---------- Параллельный запуск профилей в облаке ----------

    def _cloud_batch_set(self, profile: str, status: str, detail: str) -> None:
        def apply() -> None:
            if not hasattr(self, "cloud_batch_tree"):
                return
            values = (profile, status, detail)
            if self.cloud_batch_tree.exists(profile):
                self.cloud_batch_tree.item(profile, values=values)
            else:
                self.cloud_batch_tree.insert("", "end", iid=profile, values=values)
        self.root.after(0, apply)

    def _start_cloud_batch(self) -> None:
        if not hasattr(self, "cloud_batch_list"):
            return
        selected = [self.cloud_batch_list.get(i) for i in self.cloud_batch_list.curselection()]
        if not selected:
            messagebox.showinfo(
                "Параллельный запуск",
                "Выбери один или несколько профилей в списке (Cmd/Shift-клик для нескольких).",
            )
            return
        try:
            server_config = self._build_server_config()
        except Exception as error:
            messagebox.showerror("Облако", str(error))
            return
        already = {s["profile"] for s in self.cloud_sessions.values() if s.get("status") == "running"}
        clash = [n for n in selected if n in already]
        if clash:
            messagebox.showwarning(
                "Параллельный запуск",
                "Эти профили уже выполняются в облаке: " + ", ".join(clash),
            )
            selected = [n for n in selected if n not in already]
            if not selected:
                return
        self._append_log(f"\n[Cloud] Параллельный запуск {len(selected)} сессий: {', '.join(selected)}\n")
        for name in selected:
            self._cloud_batch_set(name, "ожидание", "постановка в очередь…")
        threading.Thread(
            target=self._run_cloud_batch_worker,
            args=(selected, server_config),
            daemon=True,
        ).start()

    def _run_cloud_batch_worker(self, profiles: list[str], server_config: ServerConfig) -> None:
        store = self._read_profiles_store()
        all_profiles = store.get("profiles", {})
        try:
            up_runtime = CloudRuntime(server_config, BASE_DIR)
            up_runtime.connect()
            self.log_queue.put("[Cloud] Загрузка файлов на сервер (один раз для всех сессий)...\n")
            up_runtime.upload_project()
            remote_base = up_runtime.get_remote_base_dir()
            up_runtime.close()
        except Exception as error:
            self.log_queue.put(f"[Cloud] Ошибка загрузки на сервер: {error}\n")
            for name in profiles:
                self._cloud_batch_set(name, "ошибка", f"загрузка: {error}")
            return

        for name in profiles:
            data = all_profiles.get(name)
            if not isinstance(data, dict):
                self._cloud_batch_set(name, "ошибка", "профиль не найден")
                continue
            data = self._ensure_profile_state_data(name, data)
            try:
                timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                safe = self._sanitize_filename_part(name)
                progress_local = str((BASE_DIR / "logs" / f"cloud_{safe}_{timestamp}.progress.json"))
                cmd = self._settings_to_command(data, force_dry_run=False, progress_file=progress_local)
                remote_cmd = self._build_remote_command(cmd, remote_base)
                runtime = CloudRuntime(server_config, BASE_DIR)
                runtime.connect()
                task = runtime.start_remote_process_detached(remote_cmd)
                task["progress_file"] = self._extract_flag_value(remote_cmd, "--progress-file")
                task["remote_state_file"] = self._extract_flag_value(remote_cmd, "--state-file")
                task["local_state_file"] = self._extract_flag_value(cmd, "--state-file")
                self.cloud_sessions[task["run_id"]] = {
                    "profile": name,
                    "runtime": runtime,
                    "task": task,
                    "log_offset": 0,
                    "status": "running",
                }
                self._cloud_batch_set(name, "запущен", f"task {task['run_id']}")
                self.log_queue.put(f"[Cloud:{name}] запущен на сервере (task {task['run_id']})\n")
            except Exception as error:
                self._cloud_batch_set(name, "ошибка", str(error))
                self.log_queue.put(f"[Cloud:{name}] ошибка запуска: {error}\n")

        self._ensure_cloud_monitor()

    def _ensure_cloud_monitor(self) -> None:
        if self._cloud_monitor_running:
            return
        self._cloud_monitor_running = True
        threading.Thread(target=self._cloud_monitor_worker, daemon=True).start()

    def _cloud_monitor_worker(self) -> None:
        while True:
            active = [
                (rid, s) for rid, s in list(self.cloud_sessions.items())
                if s.get("status") == "running"
            ]
            if not active:
                self._cloud_monitor_running = False
                return
            for _rid, session in active:
                runtime = session["runtime"]
                task = session["task"]
                name = session["profile"]
                try:
                    chunk, session["log_offset"] = runtime.read_log_chunk(task["log_file"], session["log_offset"])
                    if chunk:
                        for line in chunk.splitlines():
                            self.log_queue.put(f"[Cloud:{name}] {line}\n")
                    payload = self._read_remote_progress_payload(runtime, task.get("progress_file", ""))
                    if payload:
                        proc = payload.get("processed", 0)
                        total = payload.get("total", 0)
                        pct = payload.get("percent", 0)
                        acc = str(payload.get("current_account", "")).strip()
                        self._cloud_batch_set(name, "работает", f"{proc}/{total} ({pct}%) {acc}".strip())
                    if not runtime.is_remote_process_running(task["pid_file"]):
                        chunk, session["log_offset"] = runtime.read_log_chunk(task["log_file"], session["log_offset"])
                        if chunk:
                            for line in chunk.splitlines():
                                self.log_queue.put(f"[Cloud:{name}] {line}\n")
                        exit_code = runtime.read_exit_code(task["status_file"])
                        remote_state = task.get("remote_state_file", "")
                        local_state = task.get("local_state_file", "")
                        if remote_state and local_state:
                            try:
                                local_path = Path(local_state).expanduser().resolve()
                                if runtime.download_file(remote_state, local_path):
                                    self.log_queue.put(f"[Cloud:{name}] state синхронизирован с сервера\n")
                            except Exception as error:
                                self.log_queue.put(f"[Cloud:{name}] state не скачан: {error}\n")
                        session["status"] = "done"
                        code_view = "готово" if exit_code in (0, None) else f"код {exit_code}"
                        self._cloud_batch_set(name, "готово", code_view)
                        self.log_queue.put(f"[Cloud:{name}] задача завершена ({code_view})\n")
                        try:
                            runtime.close()
                        except Exception:
                            pass
                        self.root.after(0, self._refresh_state_info)
                except Exception as error:
                    self.log_queue.put(f"[Cloud:{name}] монитор: {error}\n")
            time.sleep(1.5)

    def _stop_cloud_batch(self) -> None:
        running = [(rid, s) for rid, s in list(self.cloud_sessions.items()) if s.get("status") == "running"]
        if not running:
            messagebox.showinfo("Параллельный запуск", "Нет активных облачных сессий.")
            return
        if not messagebox.askyesno("Остановка", f"Остановить все облачные сессии ({len(running)})?"):
            return

        def worker() -> None:
            for _rid, session in running:
                name = session["profile"]
                try:
                    stopped, message = session["runtime"].stop_remote_process(session["task"]["run_id"])
                    self.log_queue.put(f"[Cloud:{name}] остановка: {message}\n")
                    self._cloud_batch_set(name, "остановка", message[:40])
                except Exception as error:
                    self.log_queue.put(f"[Cloud:{name}] ошибка остановки: {error}\n")

        threading.Thread(target=worker, daemon=True).start()

    def _start_dry_run(self) -> None:
        self._start_process(force_dry_run=True)

    def _start_send(self) -> None:
        self._start_process(force_dry_run=False)

    def _run_current_profile_in_cloud(self) -> None:
        if not self.server_host_var.get().strip():
            messagebox.showerror(
                "Облако", "Заполните IP/домен сервера и SSH-доступ на вкладке «Облако»."
            )
            return
        profile = self.profile_var.get().strip()
        if not self.cloud_enabled_var.get():
            self.cloud_enabled_var.set(True)
        note = f" (профиль: {profile})" if profile else ""
        self._append_log(f"\n[Cloud] Запуск текущего профиля в облаке{note}...\n")
        self._start_process(force_dry_run=False)

    def _start_test_dry_run(self) -> None:
        self._start_test_send_internal(force_dry_run=True)

    def _start_test_send(self) -> None:
        self._start_test_send_internal(force_dry_run=False)

    def _start_test_send_internal(self, force_dry_run: bool) -> None:
        test_email = self.test_email_var.get().strip()
        if not test_email:
            messagebox.showerror("Тестовый email", "Укажите email для тестового письма.")
            return
        self._start_process(force_dry_run=force_dry_run, override_to=[test_email], use_to_file=False)

    def _normalized_hub_inputs(self) -> tuple[str, str, str]:
        return (
            self.hub_url_var.get().strip().rstrip("/"),
            self.hub_connection_id_var.get().strip(),
            self.hub_secret_var.get().strip(),
        )

    def _check_hub_health(self) -> None:
        hub_url, _, _ = self._normalized_hub_inputs()
        if not hub_url:
            messagebox.showerror("Проверка Hub", "Укажите Hub URL.")
            return
        try:
            context = ssl._create_unverified_context() if self.hub_insecure_ssl_var.get() else None
            with urlopen(Request(hub_url + "/index.php", method="GET"), timeout=15, context=context) as response:
                code = response.getcode()
            self._append_log(f"\nHub OK: {hub_url} (HTTP {code})\n")
            self.status_var.set("Hub доступен")
        except URLError as error:
            self._append_log(f"\nHub FAIL: {error}\n")
            self.status_var.set("Hub недоступен")
            messagebox.showerror("Проверка Hub", f"Не удалось подключиться: {error}")

    def _check_hub_pixel(self) -> None:
        hub_url, connection_id, secret = self._normalized_hub_inputs()
        if not hub_url or not connection_id or not secret:
            messagebox.showerror("Проверка пикселя", "Заполните Hub URL, Hub Conn ID и Hub Secret.")
            return
        if not connection_id.isdigit():
            messagebox.showerror("Проверка пикселя", "Hub Conn ID должен быть числом.")
            return

        cid = int(connection_id)
        campaign_id = "hubcheck-" + datetime.now().strftime("%Y%m%d")
        message_id = "hubcheck-" + uuid.uuid4().hex[:12]
        recipient_email = "check@example.com"
        recipient_hash = hashlib.sha256(recipient_email.encode("utf-8")).hexdigest()
        mail_kind = "ТЕСТ"
        ts = str(int(time.time()))
        payload = f"{cid}\n{campaign_id}\n{message_id}\n{recipient_hash}\n{recipient_email}\n{ts}"
        sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        url = hub_url + "/index.php/api/v1/public/mail-open.gif?" + urlencode(
            {
                "cid": cid,
                "cmp": campaign_id,
                "mid": message_id,
                "rh": recipient_hash,
                "re": recipient_email,
                "k": mail_kind,
                "ts": ts,
                "sig": sig,
            }
        )
        try:
            context = ssl._create_unverified_context() if self.hub_insecure_ssl_var.get() else None
            with urlopen(Request(url, method="GET"), timeout=20, context=context) as response:
                code = response.getcode()
                content_type = response.headers.get("Content-Type", "")
            self._append_log(f"\nPixel OK: HTTP {code}, {content_type}\n{url}\n")
            self.status_var.set("Пиксель Hub OK")
        except URLError as error:
            self._append_log(f"\nPixel FAIL: {error}\n{url}\n")
            self.status_var.set("Пиксель Hub FAIL")
            messagebox.showerror("Проверка пикселя", f"Ошибка запроса пикселя: {error}")

    def _start_process(
        self,
        force_dry_run: bool,
        override_to: list[str] | None = None,
        use_to_file: bool = True,
    ) -> None:
        if self.remote_run_id or (self.worker_thread and self.worker_thread.is_alive()) or (
            self.process and self.process.poll() is None
        ):
            messagebox.showwarning("Процесс уже запущен", "Остановите текущий процесс перед новым запуском.")
            return
        try:
            cmd, guessed_template = self._collect_command(
                force_dry_run=force_dry_run,
                override_to=override_to,
                use_to_file=use_to_file,
            )
        except Exception as error:
            messagebox.showerror("Ошибка настроек", str(error))
            return

        self._append_log("\n" + "=" * 72 + "\n")
        try:
            log_file_path = self._begin_run_log_file(
                force_dry_run=force_dry_run,
                override_to=override_to,
                use_to_file=use_to_file,
            )
        except Exception as error:
            messagebox.showerror("Лог-файл", f"Не удалось открыть лог-файл: {error}")
            return
        self._append_log(f"Лог-файл: {log_file_path}\n")
        progress_file_path = log_file_path.with_suffix(".progress.json")
        cmd.extend(["--progress-file", str(progress_file_path)])
        self._append_log(f"Файл прогресса: {progress_file_path}\n")
        if guessed_template is not None:
            self._append_log(f"Автоподбор шаблона для запуска: {guessed_template}\n")
        self._append_log("Команда:\n")
        self._append_log(self._sanitize_cmd_for_log(cmd) + "\n\n")
        self.status_var.set("Выполняется...")
        self._start_progress_ui()
        self._refresh_state_info()

        def worker() -> None:
            try:
                if self.cloud_enabled_var.get():
                    runtime = self._ensure_cloud_runtime()
                    self.log_queue.put("[Cloud] Синхронизация файлов проекта...\n")
                    runtime.upload_project()
                    remote_cmd = self._build_remote_command(cmd, runtime.get_remote_base_dir())
                    self.log_queue.put("[Cloud] Запуск задачи на сервере (detached)...\n")
                    task = runtime.start_remote_process_detached(remote_cmd)
                    progress_file = self._extract_flag_value(remote_cmd, "--progress-file")
                    if progress_file:
                        task["progress_file"] = progress_file
                    remote_state = self._extract_flag_value(remote_cmd, "--state-file")
                    local_state = self._extract_flag_value(cmd, "--state-file")
                    if remote_state:
                        task["remote_state_file"] = remote_state
                    if local_state:
                        task["local_state_file"] = local_state
                    self.remote_run_id = task["run_id"]
                    self.remote_task_meta = task
                    self._persist_cloud_last_task()
                    self.log_queue.put(f"[Cloud] Task ID: {task['run_id']}\n")
                    self.log_queue.put(f"[Cloud] Remote log: {task['log_file']}\n")
                    log_offset = 0
                    code: int | None = None
                    while True:
                        chunk, log_offset = runtime.read_log_chunk(task["log_file"], log_offset)
                        if chunk:
                            self.log_queue.put(chunk)
                        payload = self._read_remote_progress_payload(runtime, task.get("progress_file", ""))
                        if payload:
                            self.root.after(0, lambda p=payload: self._apply_progress_payload(p))
                        running = runtime.is_remote_process_running(task["pid_file"])
                        if not running:
                            chunk, log_offset = runtime.read_log_chunk(task["log_file"], log_offset)
                            if chunk:
                                self.log_queue.put(chunk)
                            exit_code = runtime.read_exit_code(task["status_file"])
                            code = 0 if exit_code is None else exit_code
                            break
                        time.sleep(0.35)
                    self._pull_cloud_state(runtime, cmd, remote_cmd)
                    self.remote_run_id = None
                    self.remote_task_meta = None
                    self._persist_cloud_last_task()
                else:
                    self.process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                    )
                    assert self.process.stdout is not None
                    for line in self.process.stdout:
                        self.log_queue.put(line)
                    code = self.process.wait()
                if code == 0:
                    self.log_queue.put("\n✅ Успешно завершено.\n")
                else:
                    self.log_queue.put(f"\n❌ Завершено с ошибкой, код {code}.\n")
            except Exception as error:
                self.log_queue.put(f"\n❌ Ошибка запуска: {error}\n")
            finally:
                self.root.after(0, self._refresh_state_info)
                self._set_status_async("Ожидание")
                self.process = None
                self._close_run_log_file()

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _stop_process(self) -> None:
        if self.remote_run_id:
            try:
                runtime = self._ensure_cloud_runtime()
                stopped, message = runtime.stop_remote_process(self.remote_run_id)
                if stopped:
                    self._append_log("\n⏹ Запрошена остановка облачной задачи.\n")
                else:
                    self._append_log(f"\n⏹ Не удалось подтвердить остановку: {message}\n")
                self.status_var.set("Остановка облачной задачи...")
            except Exception as error:
                self._append_log(f"\n⏹ Ошибка остановки облачной задачи: {error}\n")
            return

        if self.process and self.process.poll() is None:
            self.process.terminate()
            self._append_log("\n⏹ Запрошена остановка процесса.\n")
            self.status_var.set("Остановка...")
            return

        self._append_log("\nНет активного процесса.\n")

    def _collect_settings_data(self, include_runtime: bool) -> dict:
        data = {
            "template": self.template_var.get().strip(),
            "subject": self.subject_var.get().strip(),
            "auto_template": self.auto_template_var.get(),
            "to_file": self.to_file_var.get().strip(),
            "email_col": self.email_col_var.get().strip(),
            "kind_col": self.kind_col_var.get().strip(),
            "kind_filter": self.kind_filter_var.get().strip(),
            "fields": self.fields_var.get().strip(),
            "start_row": self.start_row_var.get().strip(),
            "sheet": self.sheet_var.get().strip(),
            "extra_to": self.extra_to_var.get().strip(),
            "use_kind_template": self.use_kind_template_var.get(),
            "allow_duplicate_emails": self.allow_duplicate_emails_var.get(),
            "state_file": self.state_file_var.get().strip(),
            "smtp_host": self.smtp_host_var.get().strip(),
            "smtp_port": self.smtp_port_var.get().strip(),
            "smtp_user": self.smtp_user_var.get().strip(),
            "smtp_password": self.smtp_pass_var.get().strip() if self.remember_password_var.get() else "",
            "smtp_accounts": self.smtp_accounts if self.remember_password_var.get() else [
                {**item, "password": ""} for item in self.smtp_accounts
            ],
            "remember_password": self.remember_password_var.get(),
            "from_email": self.from_email_var.get().strip(),
            "limit_min": self.limit_min_var.get().strip(),
            "limit_day": self.limit_day_var.get().strip(),
            "hub_url": self.hub_url_var.get().strip(),
            "hub_connection_id": self.hub_connection_id_var.get().strip(),
            "hub_secret": self.hub_secret_var.get().strip(),
            "hub_insecure_ssl": self.hub_insecure_ssl_var.get(),
            "cloud_enabled": self.cloud_enabled_var.get(),
            "server_host": self.server_host_var.get().strip(),
            "server_port": self.server_port_var.get().strip(),
            "server_user": self.server_user_var.get().strip(),
            "server_password": self.server_password_var.get().strip(),
            "server_key_path": self.server_key_path_var.get().strip(),
            "server_key_passphrase": self.server_key_pass_var.get().strip(),
            "server_remote_dir": self.server_remote_dir_var.get().strip(),
            "dry_run": self.dry_run_var.get(),
            "test_email": self.test_email_var.get().strip(),
        }
        if include_runtime:
            data["cloud_last_task"] = self.remote_task_meta or {}
        return data

    def _apply_settings_data(self, data: dict) -> None:
        previous_suspend = self._suspend_dirty_tracking
        self._suspend_dirty_tracking = True
        try:
            self.template_var.set(data.get("template", ""))
            self.subject_var.set(data.get("subject", ""))
            self.auto_template_var.set(bool(data.get("auto_template", True)))
            self.to_file_var.set(data.get("to_file", ""))
            self.email_col_var.set(data.get("email_col", "G"))
            self.kind_col_var.set(data.get("kind_col", "P"))
            self.kind_filter_var.set(data.get("kind_filter", "ALL"))
            self.fields_var.set(data.get("fields", "A,B,C,D"))
            self.start_row_var.set(data.get("start_row", "2"))
            self.sheet_var.set(data.get("sheet", "active"))
            self.extra_to_var.set(data.get("extra_to", ""))
            self.use_kind_template_var.set(bool(data.get("use_kind_template", True)))
            self.allow_duplicate_emails_var.set(bool(data.get("allow_duplicate_emails", False)))
            self.state_file_var.set(data.get("state_file", ".send_email_state.json"))
            self.smtp_host_var.set(data.get("smtp_host", "smtp.timeweb.ru"))
            self.smtp_port_var.set(data.get("smtp_port", "465"))
            self.smtp_user_var.set(data.get("smtp_user", "SZFO@teploobmennik.online"))
            remember_password = bool(data.get("remember_password", False))
            self.remember_password_var.set(remember_password)
            if remember_password:
                self.smtp_pass_var.set(data.get("smtp_password", ""))
            elif not self.smtp_pass_var.get().strip():
                self.smtp_pass_var.set(os.getenv("SMTP_PASSWORD", ""))
            raw_accounts = data.get("smtp_accounts", [])
            self.smtp_accounts = raw_accounts if isinstance(raw_accounts, list) else []
            self.from_email_var.set(data.get("from_email", self.smtp_user_var.get()))
            self.limit_min_var.set(data.get("limit_min", "20"))
            self.limit_day_var.set(data.get("limit_day", "300"))
            self.hub_url_var.set(data.get("hub_url", ""))
            self.hub_connection_id_var.set(data.get("hub_connection_id", ""))
            self.hub_secret_var.set(data.get("hub_secret", ""))
            self.hub_insecure_ssl_var.set(bool(data.get("hub_insecure_ssl", False)))
            self.cloud_enabled_var.set(bool(data.get("cloud_enabled", False)))
            self.server_host_var.set(data.get("server_host", ""))
            self.server_port_var.set(data.get("server_port", "22"))
            self.server_user_var.set(data.get("server_user", ""))
            self.server_password_var.set(data.get("server_password", ""))
            self.server_key_path_var.set(data.get("server_key_path", ""))
            self.server_key_pass_var.set(data.get("server_key_passphrase", ""))
            self.server_remote_dir_var.set(data.get("server_remote_dir", "~/mailinig-soft-cloud"))
            last_task = data.get("cloud_last_task", {})
            self.remote_task_meta = last_task if isinstance(last_task, dict) and last_task else None
            if self.remote_task_meta and self.remote_task_meta.get("run_id"):
                self.remote_run_id = str(self.remote_task_meta.get("run_id"))
                self.cloud_status_var.set(f"Найдена задача: {self.remote_run_id}")
            else:
                self.remote_run_id = None
            self.dry_run_var.set(bool(data.get("dry_run", False)))
            self.test_email_var.set(data.get("test_email", ""))
            self._refresh_state_info()
        finally:
            self._suspend_dirty_tracking = previous_suspend

    def _save_config(self) -> None:
        data = self._collect_settings_data(include_runtime=True)
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._append_log(f"\nНастройки сохранены: {CONFIG_PATH}\n")
        self._refresh_state_info()
        self._set_settings_dirty(False)

    def _load_config(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data, dict):
            self._apply_settings_data(data)


def main() -> None:
    root = tk.Tk()
    MailerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
