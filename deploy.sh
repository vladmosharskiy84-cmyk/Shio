#!/usr/bin/env bash
# Shio на сервере.
#   ./deploy.sh                — обновить код (git pull) и перезапустить движок
#   ./deploy.sh logs           — журнал движка в реальном времени (выход: Ctrl+C)
#   ./deploy.sh doctor         — самопроверка: настройки, база, биржи, Telegram, сторож
#   ./deploy.sh restore <файл> — восстановить базу из копии shio-ГГГГ-ММ-ДД.db.enc
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
ROOT="$(pwd)"
HEALTH_WAIT_S=120

die() {
    echo "Ошибка: $*" >&2
    exit 1
}

check_env() {
    if [[ ! -f .env ]]; then
        die "нет файла .env с секретами. Создайте его: cp .env.example .env и впишите значения."
    fi
}

# Контейнер работает от пользователя с uid 1000: ему нужна запись в ./data
check_data() {
    mkdir -p data/backups
    local owner
    owner="$(stat -c %u data 2>/dev/null || echo "?")"
    if [[ "$owner" != "1000" ]] && ! [[ -w data && "$(stat -c %a data)" =~ [2367]$ ]]; then
        echo "Внимание: папка data принадлежит uid $owner, а движок в контейнере работает от uid 1000"
        echo "и не сможет писать базу. Исправьте один раз командой:"
        echo "    sudo chown -R 1000:1000 data"
    fi
}

container_id() {
    docker compose ps -q engine
}

wait_healthy() {
    local waited=0 cid state health
    echo "Жду, пока движок подаст сигнал «жив» (до ${HEALTH_WAIT_S} с)..."
    while (( waited < HEALTH_WAIT_S )); do
        cid="$(container_id)"
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

cmd_deploy() {
    check_env
    check_data
    echo "== Обновляю код =="
    git pull --ff-only
    echo "== Собираю образ =="
    docker compose build
    echo "== Запускаю =="
    docker compose up -d
    if ! wait_healthy; then
        echo "Движок не запустился. Последние 50 строк журнала:" >&2
        docker compose logs --no-color --tail 50 engine >&2 || true
        echo "Подсказка: ./deploy.sh doctor покажет, что не так." >&2
        exit 1
    fi
    echo "== Движок работает =="
    docker compose exec engine python -m engine status
}

cmd_logs() {
    docker compose logs -f --tail 200
}

cmd_doctor() {
    check_env
    check_data
    docker compose run --rm engine python -m engine doctor
}

cmd_restore() {
    local src="${1:-}"
    [[ -n "$src" ]] || die "укажите файл копии: ./deploy.sh restore data/backups/shio-ГГГГ-ММ-ДД.db.enc"
    [[ -f "$src" ]] || die "файл не найден: $src"
    check_env
    check_data
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
    if [[ -n "$(docker compose ps -q --status running engine)" ]]; then
        die "движок не остановился; восстановление отменено"
    fi
    echo "== Восстанавливаю базу из $rel =="
    local rc=0
    # --force: проверка «бот жив» по сигналу не нужна, контейнер только что остановлен
    docker compose run --rm engine python -m engine restore --force "/app/$rel" || rc=$?
    echo "== Запускаю движок =="
    docker compose start engine || docker compose up -d engine  # контейнера могло не быть
    if (( rc != 0 )); then
        die "восстановление не выполнено (код $rc), движок запущен со старой базой"
    fi
    if ! wait_healthy; then
        echo "Движок не запустился после восстановления. Последние 50 строк журнала:" >&2
        docker compose logs --no-color --tail 50 engine >&2 || true
        exit 1
    fi
    echo "Готово."
}

case "${1:-deploy}" in
    deploy) cmd_deploy ;;
    logs) cmd_logs ;;
    doctor) cmd_doctor ;;
    restore) shift; cmd_restore "$@" ;;
    -h|--help|help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "неизвестная команда «$1». Команды: logs, doctor, restore <файл> (без команды — обновить)" ;;
esac
