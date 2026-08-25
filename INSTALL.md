# Установка «Ревизор НТД» на сервер (Proxmox)

Целевая конфигурация, под которую рассчитана поставка:

| Параметр | Значение | Следствие |
|---|---|---|
| CPU | AMD FX-8120, 8 ядер (Bulldozer, **нет AVX2, нет FMA3, нет F16C**) | llama.cpp / Ollama собирать без AVX2 |
| RAM | 24 ГиБ | гостю отдать 12–16 ГиБ, хосту Proxmox оставить ≥6 ГиБ |
| Диск | SSD 60 ГиБ | не ставить модели 7B; Ubuntu + приложение + 1.5B GGUF |
| GPU | нет; опционально HD 7950 3 ГБ | **не использовать** для LLM (GCN 1.0) |
| Гипервизор | Proxmox VE | рекомендуется **QEMU VM**, не LXC (Docker-in-LXC требует nesting) |

## 1. Создать виртуальную машину в Proxmox

1. Datacenter → local → ISO Images: загрузите **Ubuntu Server 22.04.5 LTS** (или 24.04).
2. Create VM:
   - CPU: host, **6 ядер** (2 ядра оставьте гипервизору);
   - RAM: **12288–16384 МиБ**;
   - Disk: SCSI, **40–50 ГиБ** на `local-lvm` (не все 60 — нужен запас хосту);
   - BIOS: OVMF или SeaBIOS;
   - NIC: virtio, мост `vmbr0`;
   - QEMU Guest Agent: включить.
3. Установите Ubuntu Server: пользователь `ubuntu`, OpenSSH да, Docker **не обязателен**.
4. После установки:

```bash
sudo apt update && sudo apt -y upgrade
sudo timedatectl set-timezone Asia/Krasnoyarsk
```

Пробросьте порт 8080 (или поставьте nginx на 80/443 — см. §6).

## 2. Скопировать репозиторий

С машины, где лежит архив:

```bash
scp -r ntdrevizor ubuntu@IP_ВМ:~/
```

На ВМ:

```bash
sudo bash ~/ntdrevizor/scripts/install.sh
```

Скрипт:

- поставит Python 3, Tesseract (rus), Poppler;
- создаст пользователя `revizor` и каталог `/opt/ntdrevizor`;
- поднимет venv и systemd-службу `ntdrevizor` на `0.0.0.0:8080`;
- инициализирует SQLite и базу НТД.

## 3. Вход администратора

Откройте `http://IP_ВМ:8080`

| Поле | Значение |
|---|---|
| **Логин** | `admin` |
| **Пароль** | `Revizor#2026` |

Сразу: **Настройки → сменить пароль** (не короче 8 символов).

Роли:

- `admin` — пользователи, правка НТД, настройки;
- `engineer` — создание и запуск проверок;
- `viewer` — только свои/просмотр.

## 4. Режимы и модели ИИ

В карточке проверки: **локальный / облачный / гибридный** + чекбоксы доступных моделей.

Ключи прописываются **только в** `/opt/ntdrevizor/.env` (не в браузере):

```bash
sudo nano /opt/ntdrevizor/.env
sudo systemctl restart ntdrevizor
```

| Переменная | Где взять (бесплатно) |
|---|---|
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `GROQ_API_KEY` | https://console.groq.com/keys |
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys |
| `GIGACHAT_CREDENTIALS` | https://developers.sber.ru/gigachat — Authorization Key |
| `GIGACHAT_SCOPE` | `GIGACHAT_API_PERS` (физлицо) или `GIGACHAT_API_B2B` |
| `YANDEX_API_KEY` + `YANDEX_FOLDER_ID` | https://console.yandex.cloud/ |

Без ключей система полностью рабочая: детерминированные сверки выполняются, ИИ-проверки схем помечаются *«не проводилась»* с причиной.

### Локальная модель (необязательно)

```bash
sudo bash /opt/ntdrevizor/scripts/install-local-llm.sh
```

Скрипт сам установит `build-essential`, `cmake`, `ninja-build` (apt), обновит pip и соберёт `llama-cpp-python` **без AVX2**, затем скачает Qwen2.5-1.5B Q4 (~1 ГиБ).  
На FX-8120 — примерно 2–5 токенов/с. Для разбора больших схем используйте облако.

Если при сборке видите `Failed building wheel for cmake` / `Failed to build 'ninja'` — это значит, что pip пытается компилировать сборочные инструменты из исходников. Убедитесь, что скрипт выполнялся под root с доступом к сети (для `apt-get`), либо вручную:

```bash
sudo apt-get update && sudo apt-get install -y build-essential cmake ninja-build
sudo /opt/ntdrevizor/.venv/bin/pip install --upgrade pip setuptools wheel
```

Либо установите [Ollama](https://ollama.com) на хост/ВМ:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:1.5b
```

В интерфейсе появится `ollama:qwen2.5:1.5b`, если демон отвечает на `127.0.0.1:11434`.

**Не ставьте** `llama3:8b` и крупнее: не влезут по диску и будут неприемлемо медленны.

## 5. DWG

`ezdxf` читает **DXF** напрямую.

**DWG** — закрытый формат. Система пробует по очереди:

1. ODA File Converter (`ezdxf.addons.odafc`);
2. LibreDWG (`dwg2dxf`).

Если ни того ни другого нет, проверка планов **не проводится** с явной причиной.  
Рекомендация: выгружать чертежи из AutoCAD / nanoCAD как DXF, либо установить [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter) в ВМ.

Сканированные PDF без текстового слоя идут в Tesseract (`tesseract-ocr-rus`). Качество OCR на старом CPU низкое; при пустом результате проверка схем не проводится, ничего не дорисовывается.

PDF, экспортированные из CAD/XPS (где pypdf отдаёт текст посимвольно), читаются через **PyMuPDF** (`pip install pymupdf` — уже в `requirements.txt`). Если PyMuPDF недоступен, система пробует `pdftotext` (poppler-utils) и затем pdfplumber. Кабельный журнал в таких PDF распознаётся по координатам слов.

## 6. Обратный прокси (по желанию)

```nginx
server {
    listen 80;
    server_name revizor.local;
    client_max_body_size 128m;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 300s;
    }
}
```

## 7. Docker (альтернатива)

На ВМ с Docker:

```bash
cd ntdrevizor
cp .env.example .env
# заполните ключи
docker compose up -d --build
```

Данные — в томе `ntd_data`. Для Ollama на хосте уже прописан `host.docker.internal`.

В LXC Proxmox нужен `features: nesting=1,keyctl=1` и это менее предсказуемо, чем VM.

## 8. Эксплуатация

```bash
sudo systemctl status ntdrevizor
sudo journalctl -u ntdrevizor -f
sudo sqlite3 /opt/ntdrevizor/data/app.db '.backup /root/ntdrevizor.db'
```

Каталоги:

- `/opt/ntdrevizor/data/app.db` — пользователи, проверки, НТД;
- `/opt/ntdrevizor/data/uploads/` — исходные файлы;
- `/opt/ntdrevizor/data/reports/` — сформированные DOC/XLS.

База НТД редактируется в веб-интерфейсе. Полные тексты СП/ГОСТ администратор вставляет в карточку документа (поле «полный текст») — тогда модель видит их при разборе. В поставку полные официальные тексты **не входят**.

Каталог НТД актуален на **2026-08-19**: 40 документов, в том числе СП 6.13130.2025 (с 29.06.2026), СП 3.13130.2026 (с 01.06.2026), ГОСТ Р 21.101-2026 (с 01.04.2026), ГОСТ Р 53246-2025 (с 01.02.2026), СП 519.1325800.2023, ГОСТ Р 21.703-2020, ГОСТ Р 58238-2018, СП 48.13330.2019, ГОСТ 21.208-2013. Перед проверкой нажмите «Проверить актуальность» или просто запустите проверку — актуализация выполняется автоматически.

## 9. Контрольный комплект

```bash
sudo -u revizor /opt/ntdrevizor/.venv/bin/python /opt/ntdrevizor/samples/make_samples.py
```

Файлы в `samples/`: спецификация, журнал, расчёт, записка с **намеренными** ошибками (ВВГ без нг, кабель СПС без FR, автомат 25 А на 1,5 мм², АКБ 7 А·ч при C=9, ссылки на СП 6.13130.2021 и «СОУЭ 3 типа»). Загрузите их в новую проверку систем ЭО+ПС+СОУЭ и сверьте отчёт.

## 10. Самопроверка репозитория

```bash
cd /opt/ntdrevizor
sudo -u revizor .venv/bin/pip install httpx   # уже в requirements
sudo -u revizor .venv/bin/python tests/test_system.py
```

Ожидается строка `ВСЕ ПРОВЕРКИ РЕПОЗИТОРИЯ ПРОЙДЕНЫ`.
