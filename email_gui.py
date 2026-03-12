#!/usr/bin/env python3
from __future__ import annotations
import json
import os
import queue
import re
import subprocess
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
from tkinter import filedialog, messagebox, ttk
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cloud_runtime import CloudRuntime, ServerConfig
from self_update import apply_update, check_for_updates


APP_TITLE = "SMTP Рассылка — ПРОМТЕХРЕШЕНИЯ"
CONFIG_PATH = Path(__file__).resolve().parent / "mailer_gui_config.json"
SCRIPT_PATH = Path(__file__).resolve().parent / "send_email.py"
BASE_DIR = Path(__file__).resolve().parent


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

        self._setup_style()
        self._build_ui()
        self._bind_state_traces()
        self._load_config()
        if not self.to_file_var.get().strip():
            self._auto_pick_to_file(silent=True)
        if not self.template_var.get().strip():
            self._auto_pick_template(silent=True)
        self._poll_logs()
        self.root.after(1200, self._check_for_updates)

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

    def _bind_state_traces(self) -> None:
        variables = [
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
        for var in variables:
            var.trace_add("write", lambda *_args: self._refresh_state_info())

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
        container.rowconfigure(6, weight=1)

        ttk.Label(container, text=APP_TITLE, style="Header.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 10)
        )

        self._build_template_section(container, row=1)
        self._build_excel_section(container, row=2)
        self._build_smtp_section(container, row=3)
        self._build_action_tabs(container, row=4)
        self._build_status_bar(container, row=5)
        self._build_log_section(container, row=6)

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

    def _build_log_section(self, parent: ttk.Frame, row: int) -> None:
        frame = ttk.LabelFrame(parent, text="Лог выполнения", padding=10)
        frame.grid(row=row, column=0, sticky="nsew", pady=(0, 8))
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
        notebook.grid(row=row, column=0, sticky="ew", pady=(0, 8))

        send_tab = ttk.Frame(notebook, padding=10)
        test_tab = ttk.Frame(notebook, padding=10)
        hub_tab = ttk.Frame(notebook, padding=10)
        cloud_tab = ttk.Frame(notebook, padding=10)
        notebook.add(send_tab, text="Рассылка")
        notebook.add(test_tab, text="Тест")
        notebook.add(hub_tab, text="Хаб")
        notebook.add(cloud_tab, text="Облако")

        self._build_send_controls(send_tab)
        self._build_test_controls(test_tab)
        self._build_hub_controls(hub_tab)
        self._build_cloud_controls(cloud_tab)

    def _build_send_controls(self, frame: ttk.Frame) -> None:
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        actions = ttk.Frame(frame)
        actions.grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="Сохранить настройки", command=self._save_config).grid(row=0, column=0)
        ttk.Button(actions, text="Проверить", command=self._start_dry_run).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(actions, text="Отправить", command=self._start_send).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(actions, text="Стоп", command=self._stop_process).grid(row=0, column=3, padx=(8, 0))

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

    def _build_cloud_controls(self, frame: ttk.Frame) -> None:
        for idx in range(4):
            frame.columnconfigure(idx, weight=1 if idx in (1, 3) else 0)

        self.cloud_enabled_var = tk.BooleanVar(value=False)
        self.server_host_var = tk.StringVar()
        self.server_port_var = tk.StringVar(value="22")
        self.server_user_var = tk.StringVar()
        self.server_password_var = tk.StringVar()
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

        ttk.Label(frame, text="Папка на сервере:").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(frame, textvariable=self.server_remote_dir_var).grid(row=3, column=1, columnspan=3, sticky="ew", pady=(8, 0))

        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Button(actions, text="Инициализация сервера", command=self._initialize_server).grid(row=0, column=0)
        ttk.Button(actions, text="Проверить обновления", command=self._check_for_updates).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(actions, text="Обновить программу", command=self._apply_update).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(actions, text="Статус облачной задачи", command=self._check_cloud_task_status).grid(row=0, column=3, padx=(8, 0))

        ttk.Label(frame, text="Статус облака:").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        ttk.Label(frame, textvariable=self.cloud_status_var).grid(row=5, column=1, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(frame, text="Статус обновлений:").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Label(frame, textvariable=self.update_status_var).grid(row=6, column=1, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(
            frame,
            text="При облачном режиме задача продолжает выполняться на сервере даже если GUI закрыт.",
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(8, 0))

    def _build_server_config(self) -> ServerConfig:
        host = self.server_host_var.get().strip()
        username = self.server_user_var.get().strip()
        password = self.server_password_var.get().strip()
        remote_dir = self.server_remote_dir_var.get().strip() or "~/mailinig-soft-cloud"
        port = self._safe_int(self.server_port_var.get().strip(), 22)
        if not host or not username or not password:
            raise RuntimeError("Заполните IP/домен, SSH логин и SSH пароль.")
        if port <= 0:
            raise RuntimeError("SSH порт должен быть положительным числом.")
        return ServerConfig(
            host=host,
            port=port,
            username=username,
            password=password,
            remote_dir=remote_dir,
        )

    def _initialize_server(self) -> None:
        try:
            server_config = self._build_server_config()
        except Exception as error:
            messagebox.showerror("Инициализация сервера", str(error))
            return

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
                self.cloud_status_var.set("Сервер готов к облачному выполнению")
            except Exception as error:
                runtime.close()
                self.log_queue.put(f"[Cloud] Ошибка инициализации: {error}\n")
                self.cloud_status_var.set("Ошибка инициализации")
            finally:
                self.status_var.set("Ожидание")

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
                self.update_status_var.set(message)
            except Exception as error:
                self.log_queue.put(f"[Update] Ошибка проверки: {error}\n")
                self.update_status_var.set("Ошибка проверки обновлений")
            finally:
                self.status_var.set("Ожидание")

        threading.Thread(target=worker, daemon=True).start()

    def _apply_update(self) -> None:
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
                self.update_status_var.set(f"Установлено обновление: {commit}")
            except Exception as error:
                self.log_queue.put(f"[Update] Ошибка обновления: {error}\n")
                self.update_status_var.set("Ошибка обновления")
            finally:
                self.status_var.set("Ожидание")

        threading.Thread(target=worker, daemon=True).start()

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
        self.state_date_var.set(str(data.get("date", "—")))
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
        self.log_text.see("end")
        if self.current_log_handle is not None:
            try:
                self.current_log_handle.write(text)
                self.current_log_handle.flush()
            except Exception:
                pass

    def _poll_logs(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self._append_log(line)
                self._handle_progress_line(line)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_logs)

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

    def _collect_command(
        self,
        force_dry_run: bool | None = None,
        override_to: list[str] | None = None,
        use_to_file: bool = True,
    ) -> list[str]:
        if not SCRIPT_PATH.exists():
            raise RuntimeError(f"Не найден скрипт: {SCRIPT_PATH}")

        if self.auto_template_var.get():
            self._auto_pick_template(silent=True)

        template = self.template_var.get().strip()
        if not template:
            self._auto_pick_template(silent=True)
            template = self.template_var.get().strip()
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

        dry_run = self.dry_run_var.get() if force_dry_run is None else force_dry_run
        if dry_run:
            cmd.append("--dry-run")

        return cmd

    def _sanitize_cmd_for_log(self, cmd: list[str]) -> str:
        safe = []
        mask_next = False
        for token in cmd:
            if mask_next:
                safe.append("********")
                mask_next = False
                continue
            safe.append(token)
            if token == "--smtp-password":
                mask_next = True
        return " ".join(safe)

    def _build_remote_command(self, cmd: list[str]) -> list[str]:
        remote_cmd: list[str] = []
        for token in cmd:
            if token == "python3":
                remote_cmd.append("python3")
                continue
            try:
                path = Path(token).expanduser().resolve()
            except Exception:
                remote_cmd.append(token)
                continue
            if path == SCRIPT_PATH:
                remote_cmd.append(f"{self.server_remote_dir_var.get().strip().rstrip('/')}/send_email.py")
            elif path.is_relative_to(BASE_DIR):
                relative = path.relative_to(BASE_DIR).as_posix()
                remote_cmd.append(f"{self.server_remote_dir_var.get().strip().rstrip('/')}/{relative}")
            else:
                remote_cmd.append(token)
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
                tail_text = self._tail_remote_log(runtime, task["log_file"])
                if tail_text.strip():
                    self.log_queue.put(f"[Cloud] Хвост удаленного лога:\n{tail_text}\n")
                if running:
                    self.log_queue.put("[Cloud] Задача все еще выполняется на сервере.\n")
                    self.status_var.set("Облачная задача выполняется")
                else:
                    code_view = "unknown" if exit_code is None else str(exit_code)
                    self.log_queue.put(f"[Cloud] Задача завершена. Код: {code_view}\n")
                    self.status_var.set("Облачная задача завершена")
                    self.remote_run_id = None
                    self.remote_task_meta = None
                    self._persist_cloud_last_task()
            except Exception as error:
                self.log_queue.put(f"[Cloud] Ошибка проверки статуса: {error}\n")
                self.status_var.set("Ошибка проверки статуса")
            finally:
                if self.status_var.get() == "Проверка статуса облачной задачи...":
                    self.status_var.set("Ожидание")

        threading.Thread(target=worker, daemon=True).start()

    def _start_dry_run(self) -> None:
        self._start_process(force_dry_run=True)

    def _start_send(self) -> None:
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
            cmd = self._collect_command(
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
                    remote_cmd = self._build_remote_command(cmd)
                    self.log_queue.put("[Cloud] Запуск задачи на сервере (detached)...\n")
                    task = runtime.start_remote_process_detached(remote_cmd)
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
                        running = runtime.is_remote_process_running(task["pid_file"])
                        if not running:
                            chunk, log_offset = runtime.read_log_chunk(task["log_file"], log_offset)
                            if chunk:
                                self.log_queue.put(chunk)
                            exit_code = runtime.read_exit_code(task["status_file"])
                            code = 0 if exit_code is None else exit_code
                            break
                        time.sleep(0.35)
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
                self._refresh_state_info()
                self.status_var.set("Ожидание")
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

    def _save_config(self) -> None:
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
            "server_remote_dir": self.server_remote_dir_var.get().strip(),
            "cloud_last_task": self.remote_task_meta or {},
            "dry_run": self.dry_run_var.get(),
            "test_email": self.test_email_var.get().strip(),
        }
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._append_log(f"\nНастройки сохранены: {CONFIG_PATH}\n")
        self._refresh_state_info()

    def _load_config(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return

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
        self.server_remote_dir_var.set(data.get("server_remote_dir", "~/mailinig-soft-cloud"))
        last_task = data.get("cloud_last_task", {})
        self.remote_task_meta = last_task if isinstance(last_task, dict) and last_task else None
        if self.remote_task_meta and self.remote_task_meta.get("run_id"):
            self.remote_run_id = str(self.remote_task_meta.get("run_id"))
            self.cloud_status_var.set(f"Найдена задача: {self.remote_run_id}")
        self.dry_run_var.set(bool(data.get("dry_run", False)))
        self.test_email_var.set(data.get("test_email", ""))
        self._refresh_state_info()


def main() -> None:
    root = tk.Tk()
    MailerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
