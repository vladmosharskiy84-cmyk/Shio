#!/usr/bin/env bash
# Shio на сервере.
#   ./deploy.sh                — обновить код (git pull) и перезапустить движок
#   ./deploy.sh logs           — журнал движка в реальном времени (выход: Ctrl+C)
#   ./deploy.sh doctor         — самопроверка: настройки, база, биржи, Telegram, сторож
#   ./deploy.sh restore <файл> — восстановить базу из копии shio-ГГГГ-ММ-ДД.db.enc
set -euo pipefail

HEALTH_WAIT_S=150
ENGINE_UID=1000  # пользователь shio в контейнере

die() {
    echo "Ошибка: $*" >&2
    exit 1
}

check_docker() {
    command -v docker >/dev/null 2>&1 \
        || die "не найден Docker. Установите его: https://docs.docker.com/engine/install/"
    docker compose version >/dev/null 2>&1 \
        || die "не найден Docker Compose v2 (команда «docker compose»). Установите пакет docker-compose-plugin."
}

# Файлы, которые compose монтирует в контейнер: если их нет, Docker создаст на их месте папки
check_files() {
    [[ ! -d config.yaml ]] \
        || die "config.yaml — папка, а должен быть файлом. Удалите её (rm -r config.yaml) и верните файл: git checkout config.yaml"
    [[ -f config.yaml ]] \
        || die "нет файла config.yaml в $ROOT. Верните его: git checkout config.yaml"
    [[ ! -d config.local.yaml ]] \
        || die "config.local.yaml — папка, а должен быть файлом. Удалите её (rm -r config.local.yaml); образец — config.local.yaml.example"
    if [[ ! -e config.local.yaml ]]; then
        : > config.local.yaml
        echo "Создан пустой config.local.yaml (ваши настройки; образец — config.local.yaml.example)"
    fi
    [[ -f .env ]] \
        || die "нет файла .env с секретами. Создайте его: cp .env.example .env и впишите значения."
}

# Может ли пользователь контейнера (uid 1000) писать в path
writable_by_engine() {
    local path="$1" owner mode
    [[ -e "$path" ]] || return 0
    owner="$(stat -c %u "$path")"
    mode="$(stat -c %a "$path")"
    if [[ "$owner" == "$ENGINE_UID" ]]; then
        (( (8#$mode & 8#200) != 0 ))
    else
        (( (8#$mode & 8#002) != 0 ))
    fi
}

data_ok() {
    local p
    for p in data data/backups data/shio.db data/shio.db-wal data/shio.db-shm; do
        writable_by_engine "$p" || return 1
    done
}

check_data() {
    mkdir -p data/backups
    data_ok && return 0
    echo "Папка data недоступна для записи движку (uid $ENGINE_UID), исправляю владельца..."
    chown -R "$ENGINE_UID:$ENGINE_UID" data 2>/dev/null \
        || sudo -n chown -R "$ENGINE_UID:$ENGINE_UID" data 2>/dev/null \
        || true
    data_ok || die "движок не сможет писать в data. Выполните один раз:
    sudo chown -R $ENGINE_UID:$ENGINE_UID $ROOT/data
и повторите команду."
}

prepare() {
    check_docker
    check_files
    check_data
}

container_id() {
    docker compose ps -q engine
}

wait_healthy() {
    local waited=0 cid state health
    echo "Жду, пока движок подаст сигнал «жив» (до ${HEALTH_WAIT_S} с)..."
    while (( waited < HEALTH_WAIT_S )); do
        cid="$(container_id 2>/dev/null || true)"
        if [[ -n "$cid" ]]; then
            state="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo "?")"
            health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
                "$cid" 2>/dev/null || echo "?")"
        else
            state="нет контейнера"
            health=""
        fi
        echo "  ${waited} с: контейнер ${state}${health:+, здоровье ${health}}"
        if [[ "$health" == "healthy" ]]; then
            return 0
        fi
        if [[ "$state" == "restarting" || "$state" == "exited" || "$state" == "dead" ]]; then
            return 1
        fi
        sleep 5
        waited=$(( waited + 5 ))
    done
    return 1
}

show_failure() {
    echo "$1 Последние 50 строк журнала:" >&2
    docker compose logs --no-color --tail 50 engine >&2 || true
    echo "Подсказка: ./deploy.sh doctor покажет, что не так." >&2
}

cmd_deploy() {
    prepare
    echo "== Обновляю код =="
    git pull --ff-only || die "git pull не удался: на сервере изменены файлы проекта или история разошлась.
Посмотрите, что изменено: git status
Свои настройки держите в config.local.yaml (он не в git), а не в config.yaml.
Вернуть файл проекта: git checkout <файл>"
    echo "== Собираю образ =="
    docker compose build
    echo "== Запускаю =="
    docker compose up -d
    if ! wait_healthy; then
        show_failure "Движок не запустился."
        exit 1
    fi
    echo "== Движок работает =="
    docker compose exec engine python -m engine status
}

cmd_logs() {
    prepare
    docker compose logs -f --tail 200
}

cmd_doctor() {
    prepare
    docker compose run --rm engine python -m engine doctor
}

cmd_restore() {
    local src="${1:-}"
    [[ -n "$src" ]] || die "укажите файл копии: ./deploy.sh restore data/backups/shio-ГГГГ-ММ-ДД.db.enc"
    [[ -f "$src" ]] || die "файл не найден: $src"
    prepare
    # Контейнер видит только ./data (как /app/data): файл снаружи копируем в data/backups
    local abs rel
    abs="$(readlink -f "$src")"
    if [[ "$abs" != "$ROOT/data/"* ]]; then
        cp -- "$abs" "data/backups/$(basename -- "$abs")"
        abs="$ROOT/data/backups/$(basename -- "$abs")"
        echo "Файл скопирован в $abs"
    fi
    rel="${abs#"$ROOT"/}"
    echo "== Останавливаю движок (до 60 с: он дозавершит текущий шаг) =="
    docker compose stop engine
    if [[ -n "$(docker compose ps -q --status running engine 2>/dev/null || true)" ]]; then
        die "движок не остановился; восстановление отменено"
    fi
    echo "== Восстанавливаю базу из $rel =="
    local rc=0
    # Контейнер точно остановлен (проверено выше). После чистой остановки сигнала «жив» уже нет,
    # --force нужен только если движок был убит без остановки (kill, нехватка памяти).
    docker compose run --rm engine python -m engine restore --force "/app/$rel" || rc=$?
    echo "== Запускаю движок =="
    docker compose start engine || docker compose up -d engine  # контейнера могло не быть
    if (( rc != 0 )); then
        die "восстановление не выполнено (код $rc), движок запущен со старой базой"
    fi
    if ! wait_healthy; then
        show_failure "Движок не запустился после восстановления."
        exit 1
    fi
    echo "Готово."
}

main() {
    cd "$(dirname "$(readlink -f "$0")")"
    ROOT="$(pwd)"
    case "${1:-deploy}" in
        deploy) cmd_deploy ;;
        logs) cmd_logs ;;
        doctor) cmd_doctor ;;
        restore) shift; cmd_restore "$@" ;;
        -h|--help|help) sed -n '2,6p' "$(readlink -f "$0")" | sed 's/^# \{0,1\}//' ;;
        *) die "неизвестная команда «$1». Команды: logs, doctor, restore <файл> (без команды — обновить)" ;;
    esac
}

main "$@"
exit $?
