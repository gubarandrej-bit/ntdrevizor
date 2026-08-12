#!/usr/bin/env bash
# Опциональная локальная модель. На AMD FX-8120 без AVX2 и без свободных ~3 ГиБ
# на диске скрипт откажется ставить 7B+. Рекомендуется Qwen2.5 1.5B Q4 (~1 ГиБ).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ntdrevizor}"
MODEL_DIR="$APP_DIR/data/models"
THREADS="${THREADS:-4}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "sudo bash scripts/install-local-llm.sh" >&2
  exit 1
fi

free_kb=$(df -k "$APP_DIR" | awk 'NR==2{print $4}')
if [[ "$free_kb" -lt 2500000 ]]; then
  echo "Мало места: нужно ≥2,5 ГиБ свободно (у вас ~$((free_kb/1024)) МиБ)." >&2
  exit 1
fi

AVX2=0
grep -qw avx2 /proc/cpuinfo && AVX2=1

echo "Установка llama-cpp-python (CPU)..."
cd "$APP_DIR"
if [[ "$AVX2" -eq 0 ]]; then
  echo "Сборка БЕЗ AVX2/FMA/BMI2/F16C под AMD Bulldozer (FX-8120)."
  CMAKE_ARGS="-DGGML_AVX=ON -DGGML_AVX2=OFF -DGGML_FMA=OFF -DGGML_BMI2=OFF -DGGML_F16C=OFF" \
    "$APP_DIR/.venv/bin/pip" install --no-binary=:all: llama-cpp-python
else
  "$APP_DIR/.venv/bin/pip" install llama-cpp-python
fi

mkdir -p "$MODEL_DIR"
# Ссылка — Qwen2.5-1.5B-Instruct Q4_K_M (Hugging Face, свободная лицензия Apache-2.0)
URL="${MODEL_URL:-https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf}"
DEST="$MODEL_DIR/qwen2.5-1.5b-instruct-q4_k_m.gguf"
if [[ ! -f "$DEST" ]]; then
  echo "Загрузка $URL"
  curl -L --fail -o "$DEST" "$URL" || {
    echo "Автозагрузка не удалась. Скачайте GGUF вручную в $MODEL_DIR и пропишите LOCAL_GGUF_PATH в .env"
    exit 1
  }
fi

if ! grep -q '^LOCAL_GGUF_PATH=' "$APP_DIR/.env"; then
  echo "LOCAL_GGUF_PATH=$DEST" >> "$APP_DIR/.env"
else
  sed -i "s|^LOCAL_GGUF_PATH=.*|LOCAL_GGUF_PATH=$DEST|" "$APP_DIR/.env"
fi
sed -i "s|^LOCAL_GGUF_N_THREADS=.*|LOCAL_GGUF_N_THREADS=$THREADS|" "$APP_DIR/.env" || true

chown -R revizor:revizor "$MODEL_DIR" 2>/dev/null || true
systemctl restart ntdrevizor || true
echo "Локальная модель: $DEST"
echo "На FX-8120 ожидайте 2–5 ток/с. Для рабочих схем используйте облачный/гибридный режим."
