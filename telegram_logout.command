#!/bin/bash
# Выход из Telegram на Mac: Telegram закроет сеанс проверщика, файл сессии станет бесполезным.
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
  echo "Не найдено окружение .venv. Сначала выполните в Terminal: bash setup_mac.command"
  read -r -p "Нажмите Enter, чтобы закрыть окно… "
  exit 1
fi
.venv/bin/python checker/login.py --logout
read -r -p "Нажмите Enter, чтобы закрыть окно… "
