# Выкладка LeadHunter на VPS 5.83.140.177

Для сессии Claude Code на компьютере владельца, у которой есть SSH-ключ `~/.ssh/flex_server`.

## Правила сервера

- Работать только в `/opt/leadhunter`, отдельный compose-проект `leadhunter`, `mem_limit: 512m`.
- Никаких портов наружу: агенту нужен только исходящий интернет. 80/443 (Caddy), 5432, 6379, 8790 не трогать.
- Не перезапускать docker и контейнеры flex, не запускать `docker system prune` и `docker image prune -a`.
- Чужие каталоги (`flex`, `dialogspy`, `orderfilter`, `pyspy`, `2gis-worker`, `projects`, `actions-runner`) не менять.
- Ключи агента хранятся только в `/opt/leadhunter/.env` на сервере, права 600. В репозитории и в переписке их нет.

## 1. Проверка входа

```bash
ssh -i ~/.ssh/flex_server root@5.83.140.177 'echo ok && free -h && docker compose version'
```

## 2. Код на сервер

Из локальной копии репозитория `potapenkofil-rgb/search`:

```bash
git fetch origin claude/pensive-volta-nx0nwj
```

```bash
git archive --format=tar origin/claude/pensive-volta-nx0nwj | ssh -i ~/.ssh/flex_server root@5.83.140.177 'mkdir -p /opt/leadhunter && tar -x -C /opt/leadhunter'
```

Повторная выкладка — та же команда: файлы перезапишутся, `.env`, `config.yaml` и `data/` останутся.

## 3. Ключи и конфиг

```bash
ssh -i ~/.ssh/flex_server root@5.83.140.177 'cd /opt/leadhunter && [ -f .env ] || cp .env.example .env; [ -f config.yaml ] || cp config.example.yaml config.yaml; chmod 600 .env; mkdir -p data'
```

Владелец вписывает в `/opt/leadhunter/.env` (через `nano`, не через чат):

- `OPENAI_API_KEY` — новый ключ (старый светился в переписке, его надо отозвать);
- `TELEGRAM_BOT_TOKEN` — новый токен после `/revoke` в @BotFather;
- `TELEGRAM_OWNER_ID=7835543351`;
- `OPENAI_MODEL=gpt-6-luna`, `DAILY_BUDGET_USD=1.0`.

## 4. Проверка и запуск

```bash
ssh -i ~/.ssh/flex_server root@5.83.140.177 'cd /opt/leadhunter && docker compose build && docker compose run --rm leadhunter python -m leadhunter check'
```

```bash
ssh -i ~/.ssh/flex_server root@5.83.140.177 'cd /opt/leadhunter && docker compose up -d && docker compose logs --tail 30'
```

Все команды docker выполняются только из `/opt/leadhunter`, compose-проект называется `leadhunter`, чужие контейнеры не задеваются.

## 5. Чтение Telegram-чатов (по желанию)

```bash
ssh -t -i ~/.ssh/flex_server root@5.83.140.177 'cd /opt/leadhunter && docker compose run --rm -it leadhunter python -m leadhunter tg-login && docker compose restart leadhunter'
```

## 6. Парсер 2ГИС из /opt/2gis-worker

Каталог чужой: только смотреть, ничего не менять. Нужно понять, как получить из него компании без сайта
по нише и городу: CLI, HTTP API или выгрузка в файл/БД. Секреты и `.env` воркера не выводить.

```bash
ssh -i ~/.ssh/flex_server root@5.83.140.177 'ls -la /opt/2gis-worker; cat /opt/2gis-worker/README* 2>/dev/null | head -80; docker ps --format "{{.Names}}\t{{.Image}}\t{{.Ports}}" | grep -i 2gis'
```

Дальше вписать в `/opt/leadhunter/config.yaml` секцию `parser` (см. README проекта): команда должна печатать
JSON или CSV в stdout. Агент запускает её сам внутри своего контейнера, поэтому:

- если у воркера есть HTTP API на 127.0.0.1 — подойдёт маленький скрипт-обёртка с `curl`/`httpx`
  и `network_mode`/`extra_hosts` в compose для доступа к хосту;
- если это CLI — смонтировать `/opt/2gis-worker` в контейнер только на чтение (`:ro`) и вызывать его;
- docker.sock в контейнер агента не монтировать.

## Остановка

```bash
ssh -i ~/.ssh/flex_server root@5.83.140.177 'cd /opt/leadhunter && docker compose stop'
```
