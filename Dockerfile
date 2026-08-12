FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
        poppler-utils \
        libgl1 \
        libgomp1 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ntdrevizor
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY data/ntd_catalog.json data/engineering_tables.json data/check_catalog.json data/prompt_rules.md ./data/
COPY scripts ./scripts
COPY samples ./samples
COPY .env.example .

RUN mkdir -p /opt/ntdrevizor/data/uploads /opt/ntdrevizor/data/reports /opt/ntdrevizor/data/tmp \
    && useradd --system --home /opt/ntdrevizor --shell /usr/sbin/nologin revizor \
    && chown -R revizor:revizor /opt/ntdrevizor

USER revizor
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
