from __future__ import annotations

import json
import posixpath
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None


EXCLUDED_NAMES = {
    ".git",
    "__pycache__",
    ".DS_Store",
    ".venv",
    "venv",
    ".idea",
    ".pytest_cache",
    ".mypy_cache",
    "state_profiles",
    ".cloud_runs",
    "logs",
    "tests",
    "module&hub",
    "SMTP_Рассылка_macOS",
}
EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".zip",
    ".log",
    ".pem",
    ".key",
}
EXCLUDED_FILE_NAMES = {
    "mailer_gui_config.json",
    "mailer_profiles.json",
    ".send_email_state.json",
    ".env",
}


@dataclass
class ServerConfig:
    host: str
    port: int
    username: str
    password: str
    remote_dir: str
    key_path: str = ""
    key_passphrase: str = ""


class CloudRuntimeError(RuntimeError):
    pass


def _non_negative_int(value: object) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _merge_campaign_items(left_item: object, right_item: object) -> dict:
    left = left_item if isinstance(left_item, dict) else {}
    right = right_item if isinstance(right_item, dict) else {}
    left_override = str(left.get("override_at", ""))
    right_override = str(right.get("override_at", ""))
    if left_override != right_override:
        merged = dict(left if left_override > right_override else right)
    else:
        merged = {
            **right,
            **left,
            "cursor_index": max(
                _non_negative_int(left.get("cursor_index")),
                _non_negative_int(right.get("cursor_index")),
            ),
            "last_row": max(
                _non_negative_int(left.get("last_row")),
                _non_negative_int(right.get("last_row")),
            ),
        }
    merged["override_at"] = max(left_override, right_override)
    return merged


def merge_sending_states(local: dict | None, remote: dict | None) -> dict:
    """Merge mailer state without moving any counter/cursor backwards."""
    left = local if isinstance(local, dict) else {}
    right = remote if isinstance(remote, dict) else {}
    merged = {**right, **left}

    left_date = str(left.get("date", ""))
    right_date = str(right.get("date", ""))
    chosen_date = max(left_date, right_date)
    merged["date"] = chosen_date

    if left_date == right_date:
        left_accounts = left.get("account_sent_today", {})
        right_accounts = right.get("account_sent_today", {})
        if not isinstance(left_accounts, dict):
            left_accounts = {}
        if not isinstance(right_accounts, dict):
            right_accounts = {}
        left_override = str(left.get("daily_override_at", ""))
        right_override = str(right.get("daily_override_at", ""))
        if left_override != right_override:
            source = left if left_override > right_override else right
            merged["sent_today"] = _non_negative_int(source.get("sent_today"))
            source_accounts = source.get("account_sent_today", {})
            merged["account_sent_today"] = (
                dict(source_accounts) if isinstance(source_accounts, dict) else {}
            )
        else:
            merged["sent_today"] = max(
                _non_negative_int(left.get("sent_today")),
                _non_negative_int(right.get("sent_today")),
            )
            account_keys = set(left_accounts) | set(right_accounts)
            merged["account_sent_today"] = {
                str(key): max(
                    _non_negative_int(left_accounts.get(key)),
                    _non_negative_int(right_accounts.get(key)),
                )
                for key in account_keys if str(key).strip()
            }
        merged["daily_override_at"] = max(left_override, right_override)
    else:
        source = left if left_date == chosen_date else right
        merged["sent_today"] = _non_negative_int(source.get("sent_today"))
        accounts = source.get("account_sent_today", {})
        merged["account_sent_today"] = dict(accounts) if isinstance(accounts, dict) else {}

    left_campaigns = left.get("campaigns", {})
    right_campaigns = right.get("campaigns", {})
    if not isinstance(left_campaigns, dict):
        left_campaigns = {}
    if not isinstance(right_campaigns, dict):
        right_campaigns = {}
    campaigns: dict[str, dict] = {}
    for key in set(left_campaigns) | set(right_campaigns):
        campaigns[str(key)] = _merge_campaign_items(
            left_campaigns.get(key), right_campaigns.get(key)
        )
    merged["campaigns"] = campaigns
    return merged


def ensure_paramiko() -> None:
    if paramiko is None:
        raise CloudRuntimeError(
            "Для облачного режима нужен модуль paramiko. Установите: pip install paramiko"
        )


def iter_project_files(base_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in base_dir.rglob("*"):
        if any(part in EXCLUDED_NAMES for part in path.parts):
            continue
        if path.name in EXCLUDED_FILE_NAMES:
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        if path.is_file():
            files.append(path)
    return files


class CloudRuntime:
    def __init__(self, config: ServerConfig, base_dir: Path):
        ensure_paramiko()
        self.config = config
        self.base_dir = base_dir
        self.client: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None
        self.current_run_id: str | None = None
        self.remote_home: str | None = None

    def connect(self) -> None:
        if self.client is not None:
            transport = self.client.get_transport()
            if transport is not None and transport.is_active() and self.sftp is not None:
                return
            self.close()
        connect_kwargs: dict = {
            "hostname": self.config.host,
            "port": self.config.port,
            "username": self.config.username,
            # РФ→зарубежный сервер: баннер SSH может приходить с задержкой (DPI/троттлинг),
            # поэтому таймауты щедрые, плюс ниже — ретраи.
            "timeout": 30,
            "banner_timeout": 60,
            "auth_timeout": 45,
        }
        key_path = (self.config.key_path or "").strip()
        password = self.config.password or ""
        if key_path:
            expanded = str(Path(key_path).expanduser())
            connect_kwargs["key_filename"] = expanded
            passphrase = (self.config.key_passphrase or "").strip() or password.strip()
            if passphrase:
                connect_kwargs["passphrase"] = passphrase
            # ключ задан — не мешаем автоподбору ключей из агента/~/.ssh
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True
            if password:
                connect_kwargs["password"] = password
        else:
            connect_kwargs["password"] = password
            connect_kwargs["look_for_keys"] = True
            connect_kwargs["allow_agent"] = True

        last_error: Exception | None = None
        for attempt in range(3):
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(**connect_kwargs)
                self.client = client
                self.sftp = client.open_sftp()
                self.remote_home = self.sftp.normalize(".")
                return
            except paramiko.AuthenticationException:
                # неверные логин/ключ/пароль — ретраи не помогут
                try:
                    client.close()
                except Exception:
                    pass
                raise
            except Exception as error:  # banner/timeout/сеть — пробуем ещё раз
                last_error = error
                try:
                    client.close()
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep(2 + attempt * 2)
        raise CloudRuntimeError(
            "Не удалось подключиться к серверу по SSH после 3 попыток: "
            f"{last_error}. Проверьте IP/порт, доступность сервера и что вход разрешён "
            "(ключ или пароль). Если сервер за границей — возможен троттлинг сети."
        )

    def close(self) -> None:
        if self.sftp is not None:
            try:
                self.sftp.close()
            except Exception:
                pass
            self.sftp = None
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        self.remote_home = None

    def _require_client(self) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
        transport = self.client.get_transport() if self.client is not None else None
        if (
            self.client is None
            or self.sftp is None
            or transport is None
            or not transport.is_active()
        ):
            self.close()
            self.connect()
        if self.client is None or self.sftp is None:  # pragma: no cover - connect raises first
            raise CloudRuntimeError("Нет активного подключения к серверу.")
        return self.client, self.sftp

    def _resolve_remote_dir(self) -> str:
        self._require_client()
        if not self.remote_home:
            raise CloudRuntimeError("Не удалось определить home директорию на сервере.")
        raw = (self.config.remote_dir or "").strip() or "~/mailinig-soft-cloud"
        if raw == "~":
            return self.remote_home
        if raw.startswith("~/"):
            return posixpath.join(self.remote_home, raw[2:])
        if raw.startswith("/"):
            return raw
        return posixpath.join(self.remote_home, raw)

    def get_remote_base_dir(self) -> str:
        return self._resolve_remote_dir()

    def exec(self, command: str, timeout: int = 120) -> tuple[int, str, str]:
        client, _ = self._require_client()
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def _remote_mkdirs(self, remote_path: str) -> None:
        _, sftp = self._require_client()
        parts = []
        current = remote_path
        while current not in ("", "/"):
            parts.append(current)
            current = posixpath.dirname(current)
        for item in reversed(parts):
            try:
                sftp.stat(item)
            except OSError:
                sftp.mkdir(item)

    def upload_project(self, on_progress=None) -> list[str]:
        _, sftp = self._require_client()
        remote_root = self._resolve_remote_dir().rstrip("/")
        self._remote_mkdirs(remote_root)
        uploaded: list[str] = []
        files = iter_project_files(self.base_dir)
        total = len(files)
        for index, local_path in enumerate(files, start=1):
            relative = local_path.relative_to(self.base_dir).as_posix()
            remote_path = f"{remote_root}/{relative}"
            remote_parent = posixpath.dirname(remote_path)
            self._remote_mkdirs(remote_parent)
            sftp.put(str(local_path), remote_path)
            uploaded.append(relative)
            if on_progress:
                on_progress(index, total, relative)
        return uploaded

    def initialize_server(self, on_step=None) -> str:
        remote_dir_abs = self._resolve_remote_dir()
        remote_dir = shlex.quote(remote_dir_abs)
        password = shlex.quote(self.config.password)
        steps = [
            (
                "Создание рабочей папки",
                f"mkdir -p {remote_dir}",
                60,
            ),
            (
                "Проверка доступа sudo",
                f"printf '%s\\n' {password} | sudo -S -v",
                60,
            ),
            (
                "Обновление пакетов apt",
                f"if command -v apt-get >/dev/null 2>&1; then printf '%s\\n' {password} | sudo -S apt-get update -y; fi",
                900,
            ),
            (
                "Установка системных зависимостей",
                (
                    "if command -v apt-get >/dev/null 2>&1; then "
                    f"printf '%s\\n' {password} | sudo -S apt-get install -y python3 python3-venv python3-pip curl unzip git; "
                    "fi"
                ),
                900,
            ),
            (
                "Создание виртуального окружения",
                f"cd {remote_dir} && python3 -m venv .venv",
                120,
            ),
            (
                "Обновление pip",
                f"cd {remote_dir} && . .venv/bin/activate && python -m pip install --upgrade pip",
                300,
            ),
            (
                "Установка python-зависимостей",
                f"cd {remote_dir} && . .venv/bin/activate && python -m pip install openpyxl paramiko certifi",
                600,
            ),
        ]
        full_output: list[str] = []
        for label, command, timeout in steps:
            if on_step:
                on_step(label)
            code, out, err = self.exec(command, timeout=timeout)
            if out.strip():
                full_output.append(out.strip())
            if err.strip():
                full_output.append(err.strip())
            if code != 0:
                joined = "\n".join(full_output).strip()
                raise CloudRuntimeError(joined or f"Ошибка шага: {label}")
        return "\n".join(full_output).strip() or "Инициализация завершена."

    def start_remote_process_detached(self, argv: list[str]) -> dict:
        _, _ = self._require_client()
        run_id = uuid.uuid4().hex[:12]
        self.current_run_id = run_id
        remote_dir = self._resolve_remote_dir().rstrip("/")
        runs_dir = f"{remote_dir}/.cloud_runs"
        pid_file = f"{runs_dir}/task_{run_id}.pid"
        log_file = f"{runs_dir}/task_{run_id}.log"
        status_file = f"{runs_dir}/task_{run_id}.exit"
        argv_quoted = " ".join(shlex.quote(token) for token in argv)
        inner_script = (
            f"{argv_quoted}; "
            "rc=$?; "
            f"echo $rc > {shlex.quote(status_file)}; "
            f"rm -f {shlex.quote(pid_file)}; "
            "exit $rc"
        )
        launch_script = (
            f"set -e; mkdir -p {shlex.quote(runs_dir)}; cd {shlex.quote(remote_dir)}; "
            "export PYTHONUNBUFFERED=1; "
            "if [ -f .venv/bin/activate ]; then . .venv/bin/activate; fi; "
            f"nohup sh -c {shlex.quote(inner_script)} "
            f"> {shlex.quote(log_file)} 2>&1 < /dev/null & "
            "pid=$!; "
            f"echo $pid > {shlex.quote(pid_file)}; "
            "echo $pid"
        )
        code, out, err = self.exec(launch_script, timeout=60)
        if code != 0:
            raise CloudRuntimeError((out + "\n" + err).strip() or "Не удалось запустить задачу в облаке.")
        pid = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if not pid.isdigit():
            raise CloudRuntimeError(f"Не удалось получить PID процесса. Ответ: {(out + err).strip()}")
        return {
            "run_id": run_id,
            "pid": pid,
            "pid_file": pid_file,
            "log_file": log_file,
            "status_file": status_file,
        }

    def is_remote_process_running(self, pid_file: str) -> bool:
        command = (
            f"if [ -f {shlex.quote(pid_file)} ]; then "
            f"pid=$(cat {shlex.quote(pid_file)}); "
            "kill -0 \"$pid\" >/dev/null 2>&1 && echo RUNNING || echo STOPPED; "
            "else echo STOPPED; fi"
        )
        code, out, _err = self.exec(command, timeout=20)
        return code == 0 and "RUNNING" in out

    def stop_remote_process(self, run_id: str) -> tuple[bool, str]:
        runs_dir = f"{self._resolve_remote_dir().rstrip('/')}/.cloud_runs"
        pid_file = f"{runs_dir}/task_{run_id}.pid"
        command = (
            f"if [ -f {shlex.quote(pid_file)} ]; then "
            f"kill -TERM $(cat {shlex.quote(pid_file)}) && echo STOP_SENT; "
            "else echo NO_PID_FILE; fi"
        )
        code, out, err = self.exec(command, timeout=30)
        message = (out + "\n" + err).strip()
        return code == 0 and "STOP_SENT" in message, message or "Команда остановки отправлена."

    def download_text_file(self, remote_path: str) -> str:
        _, sftp = self._require_client()
        with sftp.open(remote_path, "r") as handle:
            data = handle.read()
            if isinstance(data, (bytes, bytearray)):
                return data.decode("utf-8", errors="replace")
            return str(data)

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        """Заливает произвольный локальный файл на сервер (создаёт папки)."""
        _, sftp = self._require_client()
        self._remote_mkdirs(posixpath.dirname(remote_path))
        sftp.put(str(local_path), remote_path)

    def download_file(self, remote_path: str, local_path: Path) -> bool:
        """Скачивает файл с сервера в local_path. True если файл существовал."""
        _, sftp = self._require_client()
        try:
            sftp.stat(remote_path)
        except OSError:
            return False
        local_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = local_path.with_suffix(local_path.suffix + f".{uuid.uuid4().hex}.tmp")
        try:
            sftp.get(remote_path, str(temp_path))
            temp_path.replace(local_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return True

    def upload_text_file(self, remote_path: str, content: str) -> None:
        _, sftp = self._require_client()
        self._remote_mkdirs(posixpath.dirname(remote_path))
        with sftp.open(remote_path, "w") as handle:
            handle.write(content)

    def _read_remote_json(self, remote_path: str) -> dict | None:
        try:
            data = json.loads(self.download_text_file(remote_path))
        except (OSError, ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _read_local_json(local_path: Path) -> dict | None:
        try:
            data = json.loads(local_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write_local_json_atomic(local_path: Path, data: dict) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = local_path.with_suffix(local_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(local_path)

    def _write_remote_text_atomic(self, remote_path: str, content: str) -> None:
        _, sftp = self._require_client()
        self._remote_mkdirs(posixpath.dirname(remote_path))
        temp_path = f"{remote_path}.{uuid.uuid4().hex}.tmp"
        try:
            with sftp.open(temp_path, "w") as handle:
                handle.write(content)
            try:
                sftp.posix_rename(temp_path, remote_path)
            except (AttributeError, OSError):
                try:
                    sftp.remove(remote_path)
                except OSError:
                    pass
                sftp.rename(temp_path, remote_path)
        finally:
            try:
                sftp.remove(temp_path)
            except OSError:
                pass

    def sync_state_file(self, local_path: Path, remote_path: str) -> bool:
        """Merge local/remote state, then atomically write the result to both."""
        local = self._read_local_json(local_path)
        remote = self._read_remote_json(remote_path)
        if local is None and remote is None:
            return False
        merged = merge_sending_states(local, remote)
        self._write_local_json_atomic(local_path, merged)
        self._write_remote_text_atomic(
            remote_path, json.dumps(merged, ensure_ascii=False, indent=2)
        )
        return True

    def pull_state_file(self, remote_path: str, local_path: Path) -> bool:
        """Pull remote state and merge it locally without losing newer counters."""
        remote = self._read_remote_json(remote_path)
        if remote is None:
            return False
        merged = merge_sending_states(self._read_local_json(local_path), remote)
        self._write_local_json_atomic(local_path, merged)
        return True

    def migrate_campaign_key(
        self,
        local_path: Path,
        remote_path: str,
        target_key: str,
        legacy_key: str,
    ) -> bool:
        """Copy progress from the old path-derived cloud key to the stable key."""
        if not target_key or not legacy_key or target_key == legacy_key:
            return False
        state = self._read_local_json(local_path)
        if not isinstance(state, dict):
            return False
        campaigns = state.get("campaigns", {})
        if not isinstance(campaigns, dict) or legacy_key not in campaigns:
            return False
        campaigns[target_key] = _merge_campaign_items(
            campaigns.get(target_key), campaigns.get(legacy_key)
        )
        state["campaigns"] = campaigns
        self._write_local_json_atomic(local_path, state)
        self._write_remote_text_atomic(
            remote_path, json.dumps(state, ensure_ascii=False, indent=2)
        )
        return True

    def read_log_chunk(self, remote_path: str, offset: int) -> tuple[str, int]:
        _, sftp = self._require_client()
        try:
            stats = sftp.stat(remote_path)
        except OSError:
            return "", offset
        size = int(stats.st_size)
        if size <= offset:
            return "", offset
        with sftp.open(remote_path, "r") as handle:
            handle.seek(offset)
            data = handle.read(size - offset)
        text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
        return text, size

    def read_exit_code(self, status_file: str) -> int | None:
        command = f"if [ -f {shlex.quote(status_file)} ]; then cat {shlex.quote(status_file)}; fi"
        code, out, _err = self.exec(command, timeout=20)
        if code != 0:
            return None
        raw = out.strip()
        if not raw:
            return None
        try:
            return int(raw.splitlines()[-1].strip())
        except Exception:
            return None


def stream_channel_lines(channel, stdout, stderr, on_line) -> int:
    del stdout, stderr
    out_buffer = ""
    err_buffer = ""
    while True:
        had_data = False
        if channel.recv_ready():
            out_buffer += channel.recv(4096).decode("utf-8", errors="replace")
            while "\n" in out_buffer:
                line, out_buffer = out_buffer.split("\n", 1)
                on_line(line + "\n")
            had_data = True
        if channel.recv_stderr_ready():
            err_buffer += channel.recv_stderr(4096).decode("utf-8", errors="replace")
            while "\n" in err_buffer:
                line, err_buffer = err_buffer.split("\n", 1)
                on_line(line + "\n")
            had_data = True
        if channel.exit_status_ready():
            while channel.recv_ready():
                out_buffer += channel.recv(4096).decode("utf-8", errors="replace")
            while channel.recv_stderr_ready():
                err_buffer += channel.recv_stderr(4096).decode("utf-8", errors="replace")
            if out_buffer:
                on_line(out_buffer)
            if err_buffer:
                on_line(err_buffer)
            return channel.recv_exit_status()
        if not had_data:
            time.sleep(0.1)
