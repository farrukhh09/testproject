#!/bin/bash
# Первая настройка на Mac: окружение Python и пакеты. Один раз, в Terminal: bash setup_mac.command
cd "$(dirname "$0")" || exit 1

PY="$(command -v python3.14 || command -v python3.13 || command -v python3.12 || command -v python3)"
if [ -z "$PY" ]; then
  echo "Python 3 не найден. Установите Python 3.12 или новее: https://www.python.org/downloads/macos/"
  read -r -p "Нажмите Enter, чтобы закрыть окно… "
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo "Нужен Python 3.12 или новее, а найден $("$PY" --version 2>&1)."
  echo "Установите новый: https://www.python.org/downloads/macos/ или brew install python@3.12"
  read -r -p "Нажмите Enter, чтобы закрыть окно… "
  exit 1
fi

echo "Создаю окружение .venv на $("$PY" --version 2>&1)…"
"$PY" -m venv .venv || exit 1
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt || exit 1

# Файлы запуска: разрешить запуск двойным щелчком (права теряются при копировании с Windows)
chmod +x start_checker.command telegram_logout.command
xattr -d com.apple.quarantine start_checker.command telegram_logout.command 2>/dev/null

echo
echo "Готово. Дальше:"
echo "  1. Если файла checker/.env ещё нет — скопируйте checker/.env.example в checker/.env и впишите ключи."
echo "  2. Дважды щёлкните start_checker.command"
read -r -p "Нажмите Enter, чтобы закрыть окно… "
