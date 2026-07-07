from __future__ import annotations

import json
import re
import shutil
import ssl
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


API_COMMITS_URL = "https://api.github.com/repos/panicatcks/mailinig-soft/commits/main"
ZIP_URL = "https://github.com/panicatcks/mailinig-soft/archive/refs/heads/main.zip"
UPDATE_STATE_FILE = ".update_state.json"
APP_VERSION_FILE = "APP_VERSION"


@dataclass
class UpdateInfo:
    local_commit: str
    remote_commit: str
    has_update: bool


class UpdateError(RuntimeError):
    pass


def _read_update_state(base_dir: Path) -> dict:
    path = base_dir / UPDATE_STATE_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_update_state(base_dir: Path, commit: str) -> None:
    path = base_dir / UPDATE_STATE_FILE
    payload = {"installed_commit": commit}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_app_version(base_dir: Path) -> str:
    path = base_dir / APP_VERSION_FILE
    if not path.exists():
        return ""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    return value


def resolve_local_version(base_dir: Path) -> str:
    git_commit = get_local_commit(base_dir)
    if git_commit:
        return git_commit
    state = _read_update_state(base_dir)
    installed_commit = str(state.get("installed_commit", "")).strip()
    if installed_commit:
        return installed_commit
    app_version = _read_app_version(base_dir)
    if app_version:
        return f"build-{app_version}"
    return "unknown-local-build"


def _urlopen_with_tls_fallback(req: Request, timeout: int = 20):
    context = None
    try:
        import certifi  # type: ignore

        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        context = ssl.create_default_context()

    try:
        return urlopen(req, timeout=timeout, context=context)
    except URLError as error:
        message = str(error)
        if "CERTIFICATE_VERIFY_FAILED" not in message:
            raise
        insecure_context = ssl._create_unverified_context()
        return urlopen(req, timeout=timeout, context=insecure_context)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )


def git_available() -> bool:
    return shutil.which("git") is not None


def get_local_commit(base_dir: Path) -> str:
    if not git_available() or not (base_dir / ".git").exists():
        return ""
    result = _run_git(["rev-parse", "--short", "HEAD"], base_dir)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


GIT_REMOTE_URL = "https://github.com/panicatcks/mailinig-soft.git"


def fetch_remote_commit_via_git() -> str:
    """Достаёт удалённый SHA через `git ls-remote` (ходит на github.com, а не api.github.com).

    В РФ api.github.com часто заблокирован, а github.com — доступен, поэтому
    это надёжнее API. Возвращает '' если git недоступен или команда упала.
    """
    if not git_available():
        return ""
    try:
        result = subprocess.run(
            ["git", "ls-remote", GIT_REMOTE_URL, "main"],
            text=True,
            capture_output=True,
            check=False,
            timeout=25,
        )
    except Exception:
        return ""
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    sha = result.stdout.strip().split()[0].strip()
    return sha[:12] if sha else ""


def fetch_remote_commit() -> str:
    # Сначала пробуем git ls-remote (github.com) — он работает там, где api.github.com режется.
    via_git = fetch_remote_commit_via_git()
    if via_git:
        return via_git
    req = Request(API_COMMITS_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "mailinig-soft-updater"})
    try:
        with _urlopen_with_tls_fallback(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except URLError as error:  # pragma: no cover
        raise UpdateError(f"Не удалось проверить обновление: {error}") from error
    sha = str(payload.get("sha", "")).strip()
    if not sha:
        raise UpdateError("GitHub не вернул SHA коммита.")
    return sha[:12]


def check_for_updates(base_dir: Path) -> UpdateInfo:
    local_commit = resolve_local_version(base_dir)
    remote_commit = fetch_remote_commit()
    commit_like = bool(re.fullmatch(r"[0-9a-f]{7,12}", local_commit))
    return UpdateInfo(
        local_commit=local_commit,
        remote_commit=remote_commit,
        has_update=(local_commit != remote_commit) if commit_like else True,
    )


def ensure_git_repo(base_dir: Path, remote_url: str) -> None:
    if not git_available() or not (base_dir / ".git").exists():
        return
    _run_git(["branch", "-M", "main"], base_dir)
    remote_check = _run_git(["remote", "get-url", "origin"], base_dir)
    if remote_check.returncode != 0:
        _run_git(["remote", "add", "origin", remote_url], base_dir)
    elif remote_check.stdout.strip() != remote_url:
        _run_git(["remote", "set-url", "origin", remote_url], base_dir)


def update_with_git(base_dir: Path) -> str:
    if not (base_dir / ".git").exists():
        raise UpdateError("Локальный git-репозиторий не найден.")
    ensure_git_repo(base_dir, "https://github.com/panicatcks/mailinig-soft.git")
    fetch = _run_git(["fetch", "origin", "main"], base_dir)
    if fetch.returncode != 0:
        raise UpdateError(fetch.stderr.strip() or fetch.stdout.strip() or "Ошибка git fetch")
    pull = _run_git(["pull", "--ff-only", "origin", "main"], base_dir)
    if pull.returncode != 0:
        raise UpdateError(pull.stderr.strip() or pull.stdout.strip() or "Ошибка git pull")
    return get_local_commit(base_dir) or "unknown"


def _should_skip_path(relative: Path) -> bool:
    name = relative.name
    parts = set(relative.parts)
    if ".git" in parts or "__pycache__" in parts:
        return True
    if name in {"mailer_gui_config.json", ".send_email_state.json"}:
        return True
    if relative.suffix.lower() in {".xlsx", ".csv", ".txt", ".zip", ".db", ".sqlite", ".sqlite3"}:
        return True
    return False


def update_with_archive(base_dir: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp_dir_raw:
        tmp_dir = Path(tmp_dir_raw)
        archive_path = tmp_dir / "main.zip"
        req = Request(ZIP_URL, headers={"User-Agent": "mailinig-soft-updater"})
        with _urlopen_with_tls_fallback(req, timeout=60) as response:
            archive_path.write_bytes(response.read())
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(tmp_dir)
        extracted_root = next(tmp_dir.glob("mailinig-soft-*"), None)
        if extracted_root is None:
            raise UpdateError("Не удалось распаковать архив обновления.")
        for source in extracted_root.rglob("*"):
            if source.is_dir():
                continue
            relative = source.relative_to(extracted_root)
            if _should_skip_path(relative):
                continue
            target = base_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    # Файлы уже применены. Узнать точный SHA приятно, но если сеть к API/ls-remote
    # недоступна — не роняем апдейт: берём версию из свежераспакованного APP_VERSION.
    try:
        return fetch_remote_commit()
    except Exception:
        version = _read_app_version(base_dir)
        return f"build-{version}" if version else "archive-update"


def apply_update(base_dir: Path) -> str:
    if git_available() and (base_dir / ".git").exists():
        commit = update_with_git(base_dir)
    else:
        commit = update_with_archive(base_dir)
    _write_update_state(base_dir, commit)
    return commit
