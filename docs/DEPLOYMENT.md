# Развёртывание

Инструкция для установки `twitch-recorder` на чистый Linux-хост (systemd) с нуля.
Для автоматизации большей части шагов используйте `deploy/install.sh` - он
идемпотентен, его можно перезапускать без риска перезаписать существующие
секреты или конфиг.

## 1. Зависимости

Системные пакеты (Debian/Ubuntu):

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv ffmpeg
```

`streamlink` и `rclone` в стандартных репозиториях часто устаревшие - лучше
ставить из их собственных источников:

```bash
python3 -m pip install --user --upgrade streamlink   # либо через pipx
curl https://rclone.org/install.sh | sudo bash
```

Проверьте, что все четыре бинарника видны в `PATH` (или лежат рядом с
`.venv/bin/python`, куда сервис тоже заглядывает - см. `require_binary`):

```bash
streamlink --version
ffmpeg -version
ffprobe -version
rclone version
```

## 2. Автоматическая установка

```bash
git clone <url-этого-репозитория> /tmp/twitch-recorder-src
cd /tmp/twitch-recorder-src
sudo ./deploy/install.sh
```

По умолчанию скрипт:

- создаёт системного пользователя `twitch-recorder` без домашнего логина;
- раскладывает код и venv в `/opt/twitch-recorder`;
- создаёт `/etc/twitch-recorder` (конфиг, `0750`) и `/var/lib/twitch-recorder`
  (данные, `0750`);
- копирует `config.example.yaml` → `/etc/twitch-recorder/config.yaml` и
  `credentials.example.yaml` → `/etc/twitch-recorder/twitch-credentials.yaml`,
  **только если этих файлов там ещё нет**;
- ставит systemd-юнит `twitch-recorder.service`.

Пути настраиваются переменными окружения: `INSTALL_DIR`, `CONFIG_DIR`,
`DATA_DIR`, `SERVICE_USER`, `SERVICE_NAME`. Пример с нестандартными путями:

```bash
sudo env INSTALL_DIR=/opt/twitch-recorder \
         CONFIG_DIR=/etc/twitch-recorder \
         DATA_DIR=/data/twitch-recorder \
         ./deploy/install.sh
```

## 3. Секреты и права доступа

Файл секретов должен принадлежать сервисному пользователю и быть закрыт от
остальных:

```bash
sudo chmod 600 /etc/twitch-recorder/twitch-credentials.yaml
sudo chown twitch-recorder:twitch-recorder /etc/twitch-recorder/twitch-credentials.yaml
```

`install.sh` уже выставляет эти права при первой раскладке из примера. Заполните
файл реальными `client_id`/`client_secret` (и `oauth_token`, если нужен) - см.
README.md, где написано, как их получить.

Если используете rclone-remote, его файл конфигурации (обычно
`~/.config/rclone/rclone.conf` для сервисного пользователя, либо путь, заданный
в `rclone.config`) тоже должен быть доступен только этому пользователю.

## 4. Конфиг сервиса

Отредактируйте `/etc/twitch-recorder/config.yaml`: список `channels`,
`download_directory`, при необходимости `rclone` или `processor`. Полное
описание всех ключей - в `docs/CONFIGURATION.md`.

Проверьте конфиг перед первым запуском:

```bash
sudo -u twitch-recorder /opt/twitch-recorder/.venv/bin/python \
  /opt/twitch-recorder/twitch_recorder.py \
  --config /etc/twitch-recorder/config.yaml --check-config
```

Команда ничего не пишет на диск и не обращается к сети: она разбирает конфиг,
печатает настройки (секреты маскированы) и проверяет наличие `streamlink`,
`ffmpeg`, `ffprobe`, `rclone`. Код возврата `0` - всё в порядке, `1` - есть
проблема (см. вывод и журнал).

## 5. systemd

Юнит устанавливается автоматически `install.sh`, но при ручной установке:

```bash
sudo cp deploy/twitch-recorder.service /etc/systemd/system/
# отредактируйте WorkingDirectory/ExecStart/ReadWritePaths под ваши пути
sudo systemctl daemon-reload
sudo systemctl enable --now twitch-recorder
```

Обратите внимание на две строки в юните:

- `ProtectSystem=strict` делает всю файловую систему хоста доступной только на
  чтение для этого юнита. Это защита по умолчанию, а не опечатка - если вы
  переносите `download_directory`, файл секретов или `rclone.config` за пределы
  путей, перечисленных в `ReadWritePaths`, сервис будет падать с ошибками
  доступа, а не с понятной ошибкой конфигурации.
- `ReadWritePaths=/var/lib/twitch-recorder /etc/twitch-recorder` - явный
  список каталогов, куда разрешена запись. Добавляйте туда любой новый путь,
  который сервис должен писать (например, отдельный каталог логов rclone).

## 6. Проверка после запуска

```bash
sudo systemctl status twitch-recorder
sudo journalctl -u twitch-recorder -f
```

В логе должна появиться строка `Monitoring: <список каналов>` при старте, а
затем - записи о начале/окончании записи конкретных каналов, когда они уходят
в лайв.

Диагностический разовый прогон (одна итерация опроса Twitch без демонизации):

```bash
sudo -u twitch-recorder /opt/twitch-recorder/.venv/bin/python \
  /opt/twitch-recorder/twitch_recorder.py \
  --config /etc/twitch-recorder/config.yaml --once
```

## 7. Обновление

```bash
cd /tmp/twitch-recorder-src && git pull
sudo systemctl stop twitch-recorder
sudo install -m 644 -o twitch-recorder -g twitch-recorder \
  twitch_recorder.py /opt/twitch-recorder/twitch_recorder.py
sudo -u twitch-recorder /opt/twitch-recorder/.venv/bin/pip install \
  -r requirements.txt --quiet
sudo systemctl start twitch-recorder
```

Либо просто повторно запустите `deploy/install.sh` - он безопасно обновит код и
зависимости и не тронет уже существующие `config.yaml`/`twitch-credentials.yaml`.

Остановка сервиса не теряет прогресс: активная запись доигрывает текущий чанк
(см. `Recorder.run` - у ffmpeg есть до 30 секунд на штатное завершение и ещё до
15 секунд после `SIGINT`, прежде чем сервис принудительно его убьёт), а уже
записанные, но не отправленные чанки подхватываются заново при следующем
старте (`App._recover_chunks`) - раздел про `.uploaded.json` см. в README.md.

## 8. Интеграция с процессором StreamSlice

Если в `config.yaml` задана секция `processor`, каждый готовый чанк передаётся
внешней команде вместо (или в дополнение к) прямой заливки в rclone:

```yaml
processor:
  command:
    - /opt/streamslice/.venv/bin/python
    - /opt/streamslice/tools/process_chunk_for_queue.py
    - --config
    - /opt/streamslice/config/remote.yaml
```

`twitch-recorder` сам дописывает `--input <путь-к-chunk.mp4>` в конец команды.
Единственное, что важно для процессора: он должен завершиться с кодом `0` при
успехе и с любым другим кодом при неудаче - никакого другого протокола обмена
(stdout/stderr, файлы состояния) `twitch-recorder` не ожидает и не парсит.

Если процессор и `twitch-recorder` развёрнуты в одном systemd, но с разными
переменными окружения (например, для доступа к общим ключам API StreamSlice),
подключите их через drop-in вместо правки основного юнита:

```bash
sudo mkdir -p /etc/systemd/system/twitch-recorder.service.d
sudo tee /etc/systemd/system/twitch-recorder.service.d/streamslice-env.conf >/dev/null <<'EOF'
[Service]
EnvironmentFile=/etc/streamslice.env
ReadWritePaths=/var/lib/streamslice
EOF
sudo systemctl daemon-reload
sudo systemctl restart twitch-recorder
```

`ReadWritePaths` в drop-in **добавляется** к списку из основного юнита, а не
заменяет его - оба пути (данные `twitch-recorder` и данные StreamSlice)
остаются доступны на запись.
