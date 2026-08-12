#!/usr/bin/env bash
# Установка «Ревизор НТД» на Ubuntu 22.04/24.04 или Debian 12 (ВМ Proxmox).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ntdrevizor}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="${APP_USER:-revizor}"

echo "=== Ревизор НТД: установка ==="
echo "Источник: $SRC_DIR"
echo "Назначение: $APP_DIR"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Запустите от root: sudo bash scripts/install.sh" >&2
  exit 1
fi

. /etc/os-release || true
echo "ОС: ${PRETTY_NAME:-unknown}"

echo "--- CPU / AVX ---"
if grep -qw avx2 /proc/cpuinfo; then
  echo "AVX2 есть. Локальные LLM можно собирать со стандартными флагами."
else
  echo "AVX2 НЕТ (характерно для AMD FX-8120)."
  echo "Локальный llama.cpp / llama-cpp-python собирать ТОЛЬКО с -DGGML_AVX2=OFF -DGGML_FMA=OFF -DGGML_BMI2=OFF -DGGML_F16C=OFF"
  echo "HD 7950 3 ГБ для современных LLM не используется."
fi
echo "RAM: $(awk '/MemTotal/ {printf \"%.1f ГиБ\", $2/1024/1024}' /proc/meminfo)"
df -h / | tail -1

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev \
  build-essential curl ca-certificates rsync \
  tesseract-ocr tesseract-ocr-rus poppler-utils \
  libgomp1

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

mkdir -p "$APP_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  rsync -a --delete \
    --exclude '.venv' --exclude 'data/app.db' --exclude 'data/uploads' \
    --exclude 'data/reports' --exclude 'data/secret.key' --exclude '.env' \
    "$SRC_DIR"/ "$APP_DIR"/
fi

cd "$APP_DIR"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Создан $APP_DIR/.env — заполните ключи облачных моделей при необходимости."
fi

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python samples/make_samples.py
.venv/bin/python -m app.seed

chown -R "$APP_USER":"$APP_USER" "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 640 "$APP_DIR/.env" || true

cat >/etc/systemd/system/ntdrevizor.service <<EOF
[Unit]
Description=Ревизор НТД
After=network.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=4
LimitNOFILE=8192

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ntdrevizor

echo
echo "=== Готово ==="
echo "Интерфейс:  http://$(hostname -I | awk '{print $1}'):8080"
echo "Логин:      admin"
echo "Пароль:     Revizor#2026"
echo "Смените пароль сразу после входа (Настройки)."
echo "Журнал:     journalctl -u ntdrevizor -f"
echo "Локальная модель (опционально): sudo bash $APP_DIR/scripts/install-local-llm.sh"
