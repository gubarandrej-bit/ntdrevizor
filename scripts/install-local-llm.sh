#!/usr/bin/env bash
# Опциональная локальная модель. На AMD FX-8120 без AVX2 и без свободных ~3 ГиБ
# на диске скрипт откажется ставить 7B+. Рекомендуется Qwen2.5 1.5B Q4 (~1 ГиБ).
#
# Важно: llama-cpp-python на FX-8120 нужно собирать из исходников (официальные
# wheel собраны под AVX2 и упадут на Bulldozer), но cmake/ninja и прочие
# сборочные зависимости должны ставиться ИЗ ГОТОВЫХ КОЛЁС, иначе pip начнёт
# компилировать их из исходников и упадёт (ошибка "Failed building wheel for cmake").
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/ntdrevizor}"
MODEL_DIR="$APP_DIR/data/models"
THREADS="${THREADS:-4}"
PY="${PY:-$APP_DIR/.venv/bin/python}"
PIP="${PIP:-$APP_DIR/.venv/bin/pip}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Запустите от root: sudo bash scripts/install-local-llm.sh" >&2
  exit 1
fi

free_kb=$(df -k "$APP_DIR" | awk 'NR==2{print $4}')
if [[ "$free_kb" -lt 2500000 ]]; then
  echo "Мало места: нужно ≥2,5 ГиБ свободно (у вас ~$((free_kb/1024)) МиБ)." >&2
  exit 1
fi

# --- 1. Системные инструменты сборки -----------------------------------------
echo "==> Системные зависимости: компилятор, cmake, ninja"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential cmake ninja-build
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y gcc gcc-c++ make cmake ninja-build
elif command -v yum >/dev/null 2>&1; then
  yum install -y gcc gcc-c++ make cmake ninja-build
else
  echo "Не удалось определить пакетный менеджер. Установите вручную: gcc, g++, make, cmake, ninja" >&2
  exit 1
fi
command -v cmake >/dev/null && cmake --version | head -1
command -v ninja >/dev/null && ninja --version

# --- 2. Обновляем pip (старый pip не понимает manylinux-колёса cmake/ninja) ---
echo "==> Обновление pip/setuptools/wheel"
"$PY" -m pip install --upgrade pip setuptools wheel

# --- 3. scikit-build-core — это сборочный движок, ставится из колеса ---------
echo "==> scikit-build-core (wheel)"
"$PY" -m pip install "scikit-build-core>=0.8"

# --- 4. Сборка llama-cpp-python -------------------------------------------------
AVX2=0
grep -qw avx2 /proc/cpuinfo && AVX2=1

if [[ "$AVX2" -eq 0 ]]; then
  echo "==> Сборка llama-cpp-python БЕЗ AVX2/FMA/BMI2/F16C (AMD Bulldozer, FX-8120)"
  # --no-binary только для самого llama-cpp-python (собираем из исходников),
  # --no-build-isolation: используем уже установленные cmake/ninja из системы,
  # чтобы pip НЕ пытался компилировать cmake/ninja из исходников.
  CMAKE_ARGS="-DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=OFF -DGGML_FMA=OFF -DGGML_BMI2=OFF -DGGML_F16C=OFF" \
    "$PY" -m pip install --no-build-isolation --no-binary=llama-cpp-python llama-cpp-python
else
  echo "==> Установка llama-cpp-python (есть AVX2, используем официальный wheel)"
  "$PIP" install llama-cpp-python
fi

echo "==> Проверка импорта"
"$PY" -c "import llama_cpp; print('llama-cpp-python OK, AVX2 в сборке не требуется')"

# --- 5. Модель -------------------------------------------------------------------
mkdir -p "$MODEL_DIR"
# Ссылка — Qwen2.5-1.5B-Instruct Q4_K_M (Hugging Face, свободная лицензия Apache-2.0)
URL="${MODEL_URL:-https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf}"
DEST="$MODEL_DIR/qwen2.5-1.5b-instruct-q4_k_m.gguf"
if [[ ! -f "$DEST" ]]; then
  echo "==> Загрузка $URL"
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
