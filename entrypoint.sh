#!/bin/sh
set -e

echo "========================================================"
echo "=== [1/2] Running Alembic database migrations...     ==="
echo "========================================================"
alembic upgrade head

echo "========================================================"
echo "=== [2/2] Starting Telegram Bot (Long-Polling)...    ==="
echo "========================================================"
exec python bot.py
