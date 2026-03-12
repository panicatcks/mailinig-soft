#!/bin/bash
set -euo pipefail

TARGET_VERSION="3.11.3"
TARGET_MM="3.11"
PKG_URL="https://www.python.org/ftp/python/3.11.3/python-3.11.3-macos11.pkg"
PKG_PATH="/tmp/python-3.11.3-macos11.pkg"

say_step() {
  echo
  echo "==> $1"
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Этот скрипт только для macOS."
  exit 1
fi

echo "Скрипт удалит python.org-версии Python (кроме ${TARGET_MM}) и установит Python ${TARGET_VERSION}."
echo "Системный /usr/bin/python3 не удаляется."
read -r -p "Продолжить? (yes/no): " ans
if [[ "$ans" != "yes" ]]; then
  echo "Отменено."
  exit 0
fi

say_step "Поиск python.org версий"
if [[ -d "/Library/Frameworks/Python.framework/Versions" ]]; then
  ls -1 /Library/Frameworks/Python.framework/Versions || true
else
  echo "Framework python.org не найден (это нормально)."
fi

say_step "Удаление python.org версий, кроме ${TARGET_MM}"
if [[ -d "/Library/Frameworks/Python.framework/Versions" ]]; then
  for vpath in /Library/Frameworks/Python.framework/Versions/*; do
    [[ -d "$vpath" ]] || continue
    vname="$(basename "$vpath")"
    [[ "$vname" =~ ^[0-9]+\.[0-9]+$ ]] || continue
    if [[ "$vname" != "$TARGET_MM" ]]; then
      echo "Удаляю /Library/Frameworks/Python.framework/Versions/$vname"
      sudo rm -rf "/Library/Frameworks/Python.framework/Versions/$vname"
    fi
  done
fi

say_step "Очистка старых ссылок /usr/local/bin/python* / pip*"
for b in /usr/local/bin/python /usr/local/bin/python3 /usr/local/bin/python3.* /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*; do
  [[ -e "$b" ]] || continue
  target="$(readlink "$b" || true)"
  if [[ "$target" == *"/Library/Frameworks/Python.framework/Versions/"* ]]; then
    echo "Удаляю ссылку $b -> $target"
    sudo rm -f "$b"
  fi
done

say_step "Скачивание Python ${TARGET_VERSION}"
curl -fL "$PKG_URL" -o "$PKG_PATH"

say_step "Установка Python ${TARGET_VERSION}"
sudo installer -pkg "$PKG_PATH" -target /

say_step "Проверка установки"
if [[ -x "/usr/local/bin/python3" ]]; then
  /usr/local/bin/python3 --version || true
  echo "python3 path: $(/usr/bin/which /usr/local/bin/python3 || true)"
else
  echo "ВНИМАНИЕ: /usr/local/bin/python3 не найден после установки."
fi

if command -v python3 >/dev/null 2>&1; then
  echo "default python3: $(python3 --version 2>/dev/null || true)"
  echo "which python3: $(which python3 || true)"
fi

echo
echo "Готово. Если версия в терминале не обновилась — перезапусти Terminal."
echo "Если в ~/.zshrc есть старые алиасы python/python3, убери их вручную."
