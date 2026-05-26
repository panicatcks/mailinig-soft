#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -f ".venv/bin/python" ]; then
  echo "Сначала запусти setup_macos.command"
  osascript -e 'display alert "Сначала выполни setup" message "Запусти setup_macos.command" as warning'
  exit 1
fi

source .venv/bin/activate
echo "Открываю веб-интерфейс рассылки в браузере…"
python web_app.py
