from __future__ import annotations

import os
import posixpath
import shlex
import stat
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
}
EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".zip",
}
EXCLUDED_FILE_NAMES = {
    "mailer_gui_config.json",
    ".send_email_state.json",
}


@dataclass
class ServerConfig:
    host: str
    port: int
    username: str
    password: str
    remote_dir: str


class CloudRuntimeError(RuntimeError):
    pass


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

    def connect(self) -> None:
        if self.client is not None:
            return
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.config.host,
            port=self.config.port,
            username=self.config.username,
            password=self.config.password,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
        )
        self.client = client
        self.sftp = client.open_sftp()

    def close(self) -> None:
        if self.sftp is not None:
            self.sftp.close()
            self.sftp = None
        if self.client is not None:
            self.client.close()
            self.client = None

    def _require_client(self) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
        if self.client is None or self.sftp is None:
            raise CloudRuntimeError("Нет активного подключения к серверу.")
        return self.client, self.sftp

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

    def upload_project(self) -> list[str]:
        _, sftp = self._require_client()
        remote_root = self.config.remote_dir.rstrip("/")
        self._remote_mkdirs(remote_root)
        uploaded: list[str] = []
        for local_path in iter_project_files(self.base_dir):
            relative = local_path.relative_to(self.base_dir).as_posix()
            remote_path = f"{remote_root}/{relative}"
            remote_parent = posixpath.dirname(remote_path)
            self._remote_mkdirs(remote_parent)
            sftp.put(str(local_path), remote_path)
            uploaded.append(relative)
        return uploaded

    def initialize_server(self) -> str:
        remote_dir = shlex.quote(self.config.remote_dir)
        password = shlex.quote(self.config.password)
        bootstrap = (
            f"set -e; mkdir -p {remote_dir}; "
            "if command -v apt-get >/dev/null 2>&1; then "
            f"printf '%s\\n' {password} | sudo -S apt-get update -y >/dev/null 2>&1 || true; "
            f"printf '%s\\n' {password} | sudo -S apt-get install -y python3 python3-venv python3-pip curl unzip git >/dev/null 2>&1 || true; "
            "fi; "
            f"cd {remote_dir}; "
            "python3 -m venv .venv >/dev/null 2>&1 || true; "
            ". .venv/bin/activate; "
            "python -m pip install --upgrade pip >/dev/null 2>&1; "
            "python -m pip install openpyxl paramiko >/dev/null 2>&1"
        )
        code, out, err = self.exec(bootstrap, timeout=900)
        if code != 0:
            raise CloudRuntimeError((out + "\n" + err).strip() or "Ошибка инициализации сервера.")
        return (out + "\n" + err).strip() or "Инициализация завершена."

    def start_remote_process(self, argv: list[str]):
        client, _ = self._require_client()
        run_id = uuid.uuid4().hex[:12]
        self.current_run_id = run_id
        remote_dir = self.config.remote_dir.rstrip("/")
        pid_file = f"{remote_dir}/.cloud_task_{run_id}.pid"
        argv_quoted = " ".join(shlex.quote(token) for token in argv)
        command = (
            f"cd {shlex.quote(remote_dir)} && "
            "export PYTHONUNBUFFERED=1 && "
            "if [ -f .venv/bin/activate ]; then . .venv/bin/activate; fi && "
            f"{argv_quoted} & "
            "pid=$!; "
            f"echo $pid > {shlex.quote(pid_file)}; "
            "wait $pid; "
            "status=$?; "
            f"rm -f {shlex.quote(pid_file)}; "
            "exit $status"
        )
        transport = client.get_transport()
        if transport is None:
            raise CloudRuntimeError("SSH transport недоступен.")
        channel = transport.open_session()
        channel.get_pty()
        channel.exec_command(command)
        stdout = channel.makefile("r", encoding="utf-8")
        stderr = channel.makefile_stderr("r", encoding="utf-8")
        return run_id, pid_file, channel, stdout, stderr

    def stop_remote_process(self, run_id: str) -> tuple[bool, str]:
        pid_file = f"{self.config.remote_dir.rstrip('/')}/.cloud_task_{run_id}.pid"
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
            return handle.read().decode("utf-8", errors="replace")

    def upload_text_file(self, remote_path: str, content: str) -> None:
        _, sftp = self._require_client()
        self._remote_mkdirs(posixpath.dirname(remote_path))
        with sftp.open(remote_path, "w") as handle:
            handle.write(content)


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
