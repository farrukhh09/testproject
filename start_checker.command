#!/bin/bash
# Запуск проверщика номеров на Mac. Двойной щелчок по этому файлу в Finder.
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
  echo "Не найдено окружение .venv. Сначала выполните в Terminal: bash setup_mac.command"
  read -r -p "Нажмите Enter, чтобы закрыть окно… "
  exit 1
fi
.venv/bin/python checker/server.py
read -r -p "Нажмите Enter, чтобы закрыть окно… "
