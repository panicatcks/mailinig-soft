#!/usr/bin/env python3
"""Проверка email-адресов: синтаксис + наличие MX/A записи у домена.

Без сторонних зависимостей — DNS-запросы через subprocess (nslookup),
который есть и на macOS, и на Linux-серверах.

CLI:
    python email_validator.py --in база.xlsx --email-col G --out очищенная.xlsx --bad bad.txt
    python email_validator.py --in base.txt --out clean.txt --bad bad.txt

Модульное API:
    from email_validator import validate_emails
    results = validate_emails(["a@b.ru", "x@y.zz"], on_progress=print)
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$"
)
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

_dns_cache: dict[str, bool] = {}
_dns_lock = threading.Lock()


def _nslookup(domain: str, record: str) -> tuple[str, bool]:
    """Возвращает (stdout, dns_alive). dns_alive=False если сам DNS-сервер недоступен."""
    try:
        proc = subprocess.run(
            ["nslookup", f"-type={record}", domain],
            capture_output=True,
            text=True,
            timeout=6,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        dead_markers = ("connection timed out", "no servers could be reached",
                        "command not found", "operation timed out")
        alive = not any(marker in out.lower() for marker in dead_markers)
        return out, alive
    except FileNotFoundError:
        return "", False
    except subprocess.TimeoutExpired:
        return "", False
    except Exception:
        return "", False  # любая ошибка nslookup — DNS считаем недоступным, не блочим


def domain_has_mail(domain: str) -> bool:
    """True если у домена есть MX или хотя бы A запись (RFC 5321 implicit MX)."""
    domain = domain.strip().lower().rstrip(".")
    if not domain or not DOMAIN_RE.match(domain):
        return False
    with _dns_lock:
        if domain in _dns_cache:
            return _dns_cache[domain]

    mx_out, alive = _nslookup(domain, "MX")
    if not alive:
        # DNS-сервер недоступен — не рискуем, считаем домен валидным
        return True
    if "mail exchanger" in mx_out.lower():
        with _dns_lock:
            _dns_cache[domain] = True
        return True

    a_out, alive = _nslookup(domain, "A")
    if not alive:
        return True
    has_a = bool(re.search(r"Address:\s+\d+\.\d+\.\d+\.\d+", a_out))
    if has_a:
        with _dns_lock:
            _dns_cache[domain] = True
        return True

    with _dns_lock:
        _dns_cache[domain] = False
    return False


def validate_one(email: str) -> dict:
    """Возвращает {'email': ..., 'ok': bool, 'reason': ...}."""
    e = (email or "").strip()
    if not e:
        return {"email": email, "ok": False, "reason": "empty"}
    if not EMAIL_RE.match(e):
        return {"email": email, "ok": False, "reason": "bad_syntax"}
    domain = e.rsplit("@", 1)[-1]
    if not domain_has_mail(domain):
        return {"email": email, "ok": False, "reason": "no_mx"}
    return {"email": email, "ok": True, "reason": "ok"}


def validate_emails(
    emails: Iterable[str],
    workers: int = 16,
    on_progress: Callable[[int, int, dict], None] | None = None,
) -> list[dict]:
    """Параллельная проверка списка. on_progress(done, total, result)."""
    items = list(emails)
    total = len(items)
    results: list[dict | None] = [None] * total
    done = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(validate_one, e): i for i, e in enumerate(items)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as exc:
                results[i] = {"email": items[i], "ok": False, "reason": f"error: {exc}"}
            with lock:
                done += 1
                if on_progress:
                    on_progress(done, total, results[i])
    return [r for r in results if r is not None]


# ---------- I/O helpers ----------

def _read_text_emails(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _col_to_index(col: str) -> int:
    col = (col or "A").strip().upper()
    idx = 0
    for ch in col:
        if not ch.isalpha():
            break
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(idx - 1, 0)


def _read_xlsx_emails(path: Path, email_col: str, start_row: int = 2) -> tuple[list[str], list[int]]:
    """Возвращает (emails, row_numbers) — параллельные списки."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("openpyxl не установлен. Используй .txt/.csv базу или поставь openpyxl.")
    wb = load_workbook(path, read_only=True, data_only=True)
    col_idx = _col_to_index(email_col)
    emails: list[str] = []
    rows: list[int] = []
    for ws in wb.worksheets:
        for r, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if r < start_row:
                continue
            if col_idx >= len(row):
                continue
            value = row[col_idx]
            if value is None:
                continue
            text = str(value).strip()
            if "@" in text:
                emails.append(text)
                rows.append(r)
    wb.close()
    return emails, rows


def _write_xlsx_cleaned(src: Path, dest: Path, bad_set: set[str], email_col: str, start_row: int = 2) -> int:
    """Перезаписывает xlsx, удаляя строки с плохими адресами. Возвращает кол-во удалённых."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("openpyxl нужен для записи xlsx.")
    wb = load_workbook(src)
    col_idx = _col_to_index(email_col)
    removed = 0
    for ws in wb.worksheets:
        rows_to_delete = []
        for r in range(start_row, ws.max_row + 1):
            cell = ws.cell(row=r, column=col_idx + 1).value
            if cell is None:
                continue
            email = str(cell).strip()
            if email and email in bad_set:
                rows_to_delete.append(r)
        for r in reversed(rows_to_delete):
            ws.delete_rows(r, 1)
            removed += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)
    return removed


def _write_xlsx_clean_dedup(
    src: Path,
    dest: Path,
    bad_set: set[str],
    dedup: bool,
    email_col: str,
    start_row: int = 2,
) -> tuple[int, int]:
    """Чистит xlsx: удаляет невалидные и (опц.) повторные адреса по всем листам.

    Дубликаты считаются глобально по всей книге (первое вхождение остаётся).
    Возвращает (удалено_невалидных, удалено_дубликатов).
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("openpyxl нужен для записи xlsx.")
    wb = load_workbook(src)
    col_idx = _col_to_index(email_col)
    removed_bad = 0
    removed_dup = 0
    seen: set[str] = set()
    for ws in wb.worksheets:
        rows_to_delete: list[int] = []
        for r in range(start_row, ws.max_row + 1):
            cell = ws.cell(row=r, column=col_idx + 1).value
            if cell is None:
                continue
            email = str(cell).strip()
            if not email:
                continue
            key = email.lower()
            if email in bad_set:
                rows_to_delete.append(r)
                removed_bad += 1
                continue
            if dedup:
                if key in seen:
                    rows_to_delete.append(r)
                    removed_dup += 1
                    continue
                seen.add(key)
        for r in reversed(rows_to_delete):
            ws.delete_rows(r, 1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)
    return removed_bad, removed_dup


def main() -> None:
    ap = argparse.ArgumentParser(description="Очистка списка email от невалидных адресов.")
    ap.add_argument("--in", dest="src", required=True, help="Исходный файл (.xlsx/.csv/.txt)")
    ap.add_argument("--email-col", default="A", help="Колонка email в xlsx (по умолч. A)")
    ap.add_argument("--start-row", type=int, default=2, help="С какой строки читать xlsx (по умолч. 2)")
    ap.add_argument("--out", default="", help="Куда сохранить очищенный файл (необязательно)")
    ap.add_argument("--txt-out", dest="txt_out", default="", help="Куда сохранить email через запятую (.txt)")
    ap.add_argument("--bad", default="", help="Куда выписать невалидные адреса (txt)")
    ap.add_argument("--report", default="", help="CSV-отчёт со статусом по каждому адресу")
    ap.add_argument("--dedup", action="store_true", help="Удалять дубликаты email")
    ap.add_argument("--skip-mx", dest="skip_mx", action="store_true",
                    help="Не проверять MX/домен — только синтаксис и дубликаты (быстро, без интернета)")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    src = Path(args.src).expanduser().resolve()
    if not src.exists():
        sys.exit(f"Файл не найден: {src}")

    is_xlsx = src.suffix.lower() in {".xlsx", ".xlsm"}
    if is_xlsx:
        emails, _rows = _read_xlsx_emails(src, args.email_col, args.start_row)
    else:
        emails = _read_text_emails(src)

    if not emails:
        sys.exit("В файле нет адресов.")

    total = len(emails)

    if args.skip_mx:
        print(f"Загружено {total} адресов. Проверяю только синтаксис…", flush=True)
        results = [
            {"email": e, "ok": bool(EMAIL_RE.match((e or "").strip())),
             "reason": "ok" if EMAIL_RE.match((e or "").strip()) else "bad_syntax"}
            for e in emails
        ]
    else:
        print(f"Загружено {total} адресов. Проверяю MX/синтаксис…", flush=True)
        last_pct = -1

        def progress(done: int, total: int, r: dict) -> None:
            nonlocal last_pct
            pct = int(done / total * 100)
            if pct != last_pct and pct % 5 == 0:
                print(f"  {done}/{total} ({pct}%)", flush=True)
                last_pct = pct

        results = validate_emails(emails, workers=args.workers, on_progress=progress)

    bad = [r for r in results if not r["ok"]]
    ok = [r for r in results if r["ok"]]
    bad_set = {r["email"].strip() for r in bad}

    print(f"\nГотово. Валидных: {len(ok)}, плохих: {len(bad)}")
    by_reason: dict[str, int] = {}
    for r in bad:
        by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
    for reason, count in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {count}")

    def dedup_keep_order(items: list[str]) -> tuple[list[str], int]:
        seen: set[str] = set()
        kept: list[str] = []
        removed = 0
        for e in items:
            key = e.strip().lower()
            if not key:
                continue
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            kept.append(e)
        return kept, removed

    if args.bad:
        bad_path = Path(args.bad).expanduser().resolve()
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text("\n".join(r["email"] for r in bad), encoding="utf-8")
        print(f"Плохие выписаны в {bad_path}")

    if args.report:
        rep_path = Path(args.report).expanduser().resolve()
        rep_path.parent.mkdir(parents=True, exist_ok=True)
        with rep_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["email", "ok", "reason"])
            for r in results:
                w.writerow([r["email"], "1" if r["ok"] else "0", r["reason"]])
        print(f"Отчёт: {rep_path}")

    ok_emails = [r["email"] for r in ok]
    if args.dedup:
        ok_emails, removed_dup = dedup_keep_order(ok_emails)
        print(f"Удалено дубликатов: {removed_dup}")

    if args.txt_out:
        txt_path = Path(args.txt_out).expanduser().resolve()
        txt_path.parent.mkdir(parents=True, exist_ok=True)
        txt_path.write_text(", ".join(ok_emails), encoding="utf-8")
        print(f"TXT (через запятую): {txt_path} ({len(ok_emails)} адресов)")

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        if is_xlsx:
            removed_bad, removed_dup = _write_xlsx_clean_dedup(
                src, out_path, bad_set, args.dedup, args.email_col, args.start_row
            )
            print(f"Очищенный xlsx: {out_path} (удалено невалидных: {removed_bad}, дубликатов: {removed_dup})")
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text("\n".join(ok_emails), encoding="utf-8")
            print(f"Очищенный файл: {out_path}")


if __name__ == "__main__":
    main()
