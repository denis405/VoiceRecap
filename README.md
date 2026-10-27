# VoiceRecap

Telegram-бот: голосовое или кружочек → расшифровка (OpenRouter STT) → краткий пересказ (LLM) → ответ с кнопкой «📄 Полный текст». Доступ только после одобрения администратором.

## Запуск

```bash
cp .env.example .env   # заполнить BOT_TOKEN, OPENROUTER_API_KEY, ADMIN_ID
docker compose up -d --build
docker compose logs -f
```

База SQLite лежит в `./data/bot.db` (volume), переживает пересборки. На Linux-сервере папка `data` должна быть доступна на запись пользователю с uid 1000: `mkdir -p data && sudo chown 1000:1000 data`.

## Деплой

Push в `main` → GitHub Actions (`.github/workflows/deploy.yml`) собирает образ, загружает его на сервер `194.67.200.155` через `docker save | ssh docker load` и перезапускает контейнер в `/opt/voicerecap`. Собирать на самом сервере не стоит: там мало памяти и диска. Запустить вручную: Actions → Deploy → Run workflow.

- secret репозитория `SSH_PRIVATE_KEY` — приватный ключ, чей публичный ключ лежит в `/root/.ssh/authorized_keys` на сервере
- `.env` на сервере (`/opt/voicerecap/.env`) не перезаписывается при деплое: правится руками, затем `docker compose up -d`
- база — `/opt/voicerecap/data`, владелец uid 1000

## Настройки (.env)

| Переменная | По умолчанию | |
|---|---|---|
| `STT_MODEL` | `openai/whisper-large-v3-turbo` | любая модель из [списка транскрибации](https://openrouter.ai/models?output_modalities=transcription) |
| `SUMMARY_MODEL` | `anthropic/claude-haiku-5.5` | любая чат-модель OpenRouter |
| `STT_LANGUAGE` | `ru` | язык расшифровки по умолчанию: ISO-639-1 или `auto` (автоопределение); пользователь может сменить его в `/settings` |
| `MAX_DURATION_SEC` | `3600` | максимальная длина сообщения |
| `MAX_CONCURRENT_JOBS` | `3` | сколько сообщений обрабатывается одновременно |

После изменения `.env`: `docker compose up -d`.

## Язык расшифровки

`/settings` — каждый пользователь выбирает язык: Автоопределение, Русский, English, Українська (список — `LANGUAGES` в `bot/texts.py`). Выбор хранится в таблице `user_settings`; кто ничего не выбирал, получает `STT_LANGUAGE`. Автоопределение Whisper на коротких сообщениях иногда ошибается с языком, поэтому по умолчанию стоит русский.

## Доступ

- Новый пользователь пишет боту → администратору приходит карточка с кнопками **✅ Одобрить / ❌ Отклонить**.
- `/users` — все пользователи и их статусы
- `/approve <id>` — одобрить
- `/revoke <id>` — отключить

## Статистика

Каждое голосовое и кружочек записывается в таблицу `requests`: длительность, размер, длина расшифровки и пересказа, токены и стоимость STT/LLM в долларах (берётся из ответа OpenRouter, поле `usage.cost`). Учитываются и неудачные попытки — деньги, потраченные до ошибки, тоже.

- `/stats` — сводка за сегодня / 7 / 30 дней / всё время и топ пользователей по расходам
- `/stats <id>` — подробно по пользователю
- `/stats_csv` — все запросы файлом (`;`, UTF-8, открывается в Excel)
- `/users` — у каждого пользователя число сообщений и сумма расходов

## Оформление

Имя, описания (на русском для всех языков интерфейса), меню команд и аватар выставляются при старте из `bot/profile.py` — только то, что изменилось. Аватар — `assets/avatar.jpg`; после правки `assets/*.svg` перерисовать: `./tools/render_assets.sh`.

Картинку на экране пустого чата Bot API выставить не умеет: BotFather → `/mybots` → бот → Edit Bot → Edit Description Picture → `assets/description.jpg`.

## Smoke-тест без Telegram

```bash
docker compose run --rm -v "$PWD/sample:/sample" bot python -m bot.openrouter /sample/voice.ogg
```
