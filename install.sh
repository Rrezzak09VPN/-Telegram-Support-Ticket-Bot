#!/usr/bin/env bash
# ============================================================
#  Support Ticket Bot — Installer
#  Tested on: Ubuntu 20.04 / 22.04 / 24.04, Debian 11 / 12
#  Usage:
#    bash <(curl -sL https://raw.githubusercontent.com/Rrezzak09VPN/-Telegram-Support-Ticket-Bot/main/install.sh)
#
#  Что нового в этой версии установщика:
#    • поддержка апгрейда: при повторной установке новые ключи
#      MAX_TICKETS_PER_DAY и TICKET_CREATE_COOLDOWN_MINUTES
#      автоматически дописываются в существующий config.env
#      (имеющиеся значения не перезаписываются);
#    • bot.py сам делает миграцию схемы БД (добавляет колонки
#      tickets.closed_by и tickets.closed_by_user_id) при старте;
#    • bot.py сам регистрирует команды бота для синей "/"-кнопки
#      (set_my_commands + per-admin scope).
# ============================================================

set -euo pipefail

# ── Цвета ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ── Константы ──
INSTALL_DIR="/opt/support-bot"
DATA_DIR="${INSTALL_DIR}/data"
BACKUP_DIR="${INSTALL_DIR}/backups"
CONFIG_FILE="${INSTALL_DIR}/config.env"
SERVICE_NAME="support-bot"
VENV_DIR="${INSTALL_DIR}/venv"
REPO_URL="https://raw.githubusercontent.com/Rrezzak09VPN/-Telegram-Support-Ticket-Bot/main"
MIN_PYTHON="3.10"

# ── Дефолты новых параметров (используются и при первой установке, и при миграции) ──
DEFAULT_MAX_FILE_SIZE="20971520"
DEFAULT_MAX_OPEN_TICKETS="1"
DEFAULT_MAX_MESSAGES_PER_MINUTE="10"
DEFAULT_MAX_TICKETS_PER_DAY="5"
DEFAULT_TICKET_CREATE_COOLDOWN_MINUTES="15"
DEFAULT_MAX_LOG_SIZE_MB="50"
DEFAULT_LOG_BACKUP_COUNT="5"

# ── Функции вывода ──
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }
fatal() { err "$*"; exit 1; }

header() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║     🎫 Support Ticket Bot — Installer       ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════╝${NC}"
    echo ""
}

# ── Проверка root ──
check_root() {
    if [[ $EUID -ne 0 ]]; then
        fatal "Скрипт нужно запускать от root. Используйте: sudo bash install.sh"
    fi
}

# ── Проверка ОС ──
check_os() {
    if [[ ! -f /etc/os-release ]]; then
        fatal "Не удалось определить ОС. Поддерживаются Ubuntu/Debian."
    fi
    source /etc/os-release
    case "$ID" in
        ubuntu|debian) ok "ОС: $PRETTY_NAME" ;;
        *) fatal "Неподдерживаемая ОС: $ID. Нужен Ubuntu или Debian." ;;
    esac
}

# ── Установка системных пакетов ──
install_system_deps() {
    info "Обновление пакетов..."
    apt-get update -qq > /dev/null 2>&1
    apt-get install -y -qq software-properties-common curl wget sqlite3 logrotate > /dev/null 2>&1
    ok "Системные пакеты установлены"
}

# ── Установка Python 3.10+ ──
install_python() {
    local py_cmd=""

    # Сначала ищем уже установленный Python >= MIN_PYTHON
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" &> /dev/null; then
            local ver
            ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
            if python3 -c "
import sys
cur = tuple(map(int, '${ver}'.split('.')))
req = tuple(map(int, '${MIN_PYTHON}'.split('.')))
sys.exit(0 if cur >= req else 1)
" 2>/dev/null; then
                py_cmd="$candidate"
                break
            fi
        fi
    done

    if [[ -z "$py_cmd" ]]; then
        info "Python >= ${MIN_PYTHON} не найден, устанавливаю..."
        add-apt-repository -y ppa:deadsnakes/ppa > /dev/null 2>&1 || true
        apt-get update -qq > /dev/null 2>&1
        apt-get install -y -qq python3.11 python3.11-venv python3.11-dev > /dev/null 2>&1
        py_cmd="python3.11"
    fi

    # Ставим python3-venv если не стоит
    local py_ver
    py_ver=$("$py_cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    apt-get install -y -qq "python${py_ver}-venv" > /dev/null 2>&1 || \
    apt-get install -y -qq python3-venv > /dev/null 2>&1 || true

    PYTHON_CMD="$py_cmd"
    ok "Python: $PYTHON_CMD ($($PYTHON_CMD --version 2>&1))"
}

# ── Валидация токена бота ──
validate_bot_token() {
    local token="$1"
    local total_len=${#token}
    # Telegram Bot API: BOT_ID (6-12 цифр) : SECRET (35 символов base64url)
    if (( total_len < 43 || total_len > 50 )); then
        return 1
    fi
    if [[ ! "$token" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{35}$ ]]; then
        return 1
    fi
    return 0
}

# ── Валидация и нормализация GROUP_ID ──
normalize_group_id() {
    local input="$1"

    # https://web.telegram.org/k/#-1234567890
    if [[ "$input" =~ \#-?([0-9]+)$ ]]; then
        local gid="${BASH_REMATCH[1]}"
        if [[ ${#gid} -le 10 ]]; then
            echo "-100${gid}"
        else
            echo "-${gid}"
        fi
        return 0
    fi

    # https://t.me/c/1234567890
    if [[ "$input" =~ t\.me/c/([0-9]+) ]]; then
        echo "-100${BASH_REMATCH[1]}"
        return 0
    fi

    # Длинное положительное число → дописываем -100
    if [[ "$input" =~ ^[0-9]{10,}$ ]]; then
        echo "-100${input}"
        return 0
    fi

    # Уже с минусом
    if [[ "$input" =~ ^-[0-9]+$ ]]; then
        if [[ "$input" =~ ^-100[0-9]+$ ]]; then
            echo "$input"
        else
            local raw="${input#-}"
            echo "-100${raw}"
        fi
        return 0
    fi

    return 1
}

# ── Валидация ADMIN_IDS ──
validate_admin_ids() {
    local input="$1"
    input="${input// /}"
    if [[ ! "$input" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
        return 1
    fi
    echo "$input"
    return 0
}

# ── Валидация числа в диапазоне ──
validate_int_range() {
    local input="$1"
    local min="$2"
    local max="$3"
    if [[ ! "$input" =~ ^[0-9]+$ ]]; then
        return 1
    fi
    if (( input < min || input > max )); then
        return 1
    fi
    return 0
}

# ── Интерактивный ввод конфигурации (только при первой установке) ──
configure() {
    echo ""
    echo -e "${BOLD}── Настройка бота ──${NC}"
    echo ""

    # --- BOT_TOKEN ---
    local bot_token=""
    while true; do
        echo -e "${CYAN}1/6${NC} Введите токен бота (получить у @BotFather):"
        read -rp "     Token: " bot_token
        if validate_bot_token "$bot_token"; then
            ok "Токен принят"
            break
        else
            err "Неверный формат токена! Пример: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
        fi
    done

    # --- GROUP_ID ---
    local group_id=""
    while true; do
        echo ""
        echo -e "${CYAN}2/6${NC} Введите ID группы (супергруппа с включёнными Темами)."
        echo "     Можно вставить:"
        echo "     • Числовой ID:  -1001234567890"
        echo "     • Ссылку из веба: https://web.telegram.org/k/#-1234567890"
        echo "     • Ссылку t.me:   https://t.me/c/1234567890/1"
        read -rp "     Group: " raw_group
        group_id=$(normalize_group_id "$raw_group" 2>/dev/null || echo "")
        if [[ -n "$group_id" ]]; then
            ok "GROUP_ID: ${group_id}"
            break
        else
            err "Не удалось распознать ID группы. Попробуйте ещё раз."
        fi
    done

    # --- ADMIN_IDS ---
    local admin_ids=""
    while true; do
        echo ""
        echo -e "${CYAN}3/6${NC} Введите ваш Telegram ID (узнать у @userinfobot)."
        echo "     Несколько ID через запятую: 111222333,444555666"
        read -rp "     Admin IDs: " raw_admins
        admin_ids=$(validate_admin_ids "$raw_admins" 2>/dev/null || echo "")
        if [[ -n "$admin_ids" ]]; then
            ok "ADMIN_IDS: ${admin_ids}"
            break
        else
            err "Только цифры и запятые! Пример: 123456789"
        fi
    done

    # --- PROJECT_NAME ---
    echo ""
    echo -e "${CYAN}4/6${NC} Название проекта (отображается в сообщениях бота)."
    echo "     Можно использовать эмодзи. Оставьте пустым для значения по умолчанию."
    read -rp "     Name [🎫 Support Bot]: " project_name
    if [[ -z "$project_name" ]]; then
        project_name="🎫 Support Bot"
    fi
    ok "PROJECT_NAME: ${project_name}"

    # --- MAX_TICKETS_PER_DAY ---
    local max_per_day=""
    while true; do
        echo ""
        echo -e "${CYAN}5/6${NC} Максимум тикетов от одного пользователя в сутки."
        echo "     Защита от спама \"создать-закрыть-создать-...\" (рекомендуем 3–10)."
        read -rp "     MAX_TICKETS_PER_DAY [${DEFAULT_MAX_TICKETS_PER_DAY}]: " max_per_day
        max_per_day="${max_per_day:-$DEFAULT_MAX_TICKETS_PER_DAY}"
        if validate_int_range "$max_per_day" 1 1000; then
            ok "MAX_TICKETS_PER_DAY: ${max_per_day}"
            break
        else
            err "Введите целое число в диапазоне 1..1000."
        fi
    done

    # --- TICKET_CREATE_COOLDOWN_MINUTES ---
    local cooldown=""
    while true; do
        echo ""
        echo -e "${CYAN}6/6${NC} Кулдаун между созданием тикетов одним пользователем (минуты)."
        echo "     0 = выключено. Рекомендуем 10–15 минут."
        read -rp "     TICKET_CREATE_COOLDOWN_MINUTES [${DEFAULT_TICKET_CREATE_COOLDOWN_MINUTES}]: " cooldown
        cooldown="${cooldown:-$DEFAULT_TICKET_CREATE_COOLDOWN_MINUTES}"
        if validate_int_range "$cooldown" 0 1440; then
            ok "TICKET_CREATE_COOLDOWN_MINUTES: ${cooldown}"
            break
        else
            err "Введите целое число в диапазоне 0..1440."
        fi
    done

    # --- Сохраняем ---
    BOT_TOKEN_VAL="$bot_token"
    GROUP_ID_VAL="$group_id"
    ADMIN_IDS_VAL="$admin_ids"
    PROJECT_NAME_VAL="$project_name"
    MAX_TICKETS_PER_DAY_VAL="$max_per_day"
    TICKET_CREATE_COOLDOWN_MINUTES_VAL="$cooldown"
}

# ── Создание структуры ──
create_structure() {
    info "Создание директорий..."
    mkdir -p "$INSTALL_DIR" "$DATA_DIR" "$BACKUP_DIR"
    ok "Директории созданы"
}

# ── Запись config.env (только первая установка) ──
write_config() {
    cat > "$CONFIG_FILE" <<ENVEOF
# ============================================
# Support Bot Configuration
# Создан: $(date '+%Y-%m-%d %H:%M:%S')
# Редактировать: nano ${CONFIG_FILE}
# После изменений: systemctl restart ${SERVICE_NAME}
# ============================================

BOT_TOKEN="${BOT_TOKEN_VAL}"
GROUP_ID=${GROUP_ID_VAL}
ADMIN_IDS=${ADMIN_IDS_VAL}
PROJECT_NAME="${PROJECT_NAME_VAL}"

# === Лимиты ===
# Размер вложения, байт (20 МБ)
MAX_FILE_SIZE=${DEFAULT_MAX_FILE_SIZE}
# Сколько одновременно открытых тикетов может быть у одного пользователя
MAX_OPEN_TICKETS=${DEFAULT_MAX_OPEN_TICKETS}
# Антифлуд по сообщениям (per minute)
MAX_MESSAGES_PER_MINUTE=${DEFAULT_MAX_MESSAGES_PER_MINUTE}
# Жёсткий антиспам: сколько тикетов один юзер может создать за сутки
MAX_TICKETS_PER_DAY=${MAX_TICKETS_PER_DAY_VAL}
# Кулдаун между созданием тикетов (минуты). 0 = выключено
TICKET_CREATE_COOLDOWN_MINUTES=${TICKET_CREATE_COOLDOWN_MINUTES_VAL}

# === Пути ===
DATA_DIR="${DATA_DIR}"

# === Логирование ===
MAX_LOG_SIZE_MB=${DEFAULT_MAX_LOG_SIZE_MB}
LOG_BACKUP_COUNT=${DEFAULT_LOG_BACKUP_COUNT}
ENVEOF

    chmod 600 "$CONFIG_FILE"
    ok "Конфигурация сохранена: ${CONFIG_FILE}"
}

# ── Миграция config.env при апгрейде ──
# Дописывает только те ключи, которых ещё нет в файле.
# Существующие значения НЕ перезаписывает.
migrate_config() {
    info "Проверка конфигурации на новые ключи..."

    declare -A new_keys=(
        ["MAX_TICKETS_PER_DAY"]="${DEFAULT_MAX_TICKETS_PER_DAY}"
        ["TICKET_CREATE_COOLDOWN_MINUTES"]="${DEFAULT_TICKET_CREATE_COOLDOWN_MINUTES}"
    )

    local added=0
    local appended_block=""

    for key in "${!new_keys[@]}"; do
        # Ищем ключ в начале строки (без учёта пробелов перед ним).
        if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$CONFIG_FILE"; then
            continue
        fi
        appended_block+="${key}=${new_keys[$key]}"$'\n'
        added=$((added + 1))
        info "  + добавлен ключ: ${key}=${new_keys[$key]}"
    done

    if (( added > 0 )); then
        {
            echo ""
            echo "# === Добавлено миграцией $(date '+%Y-%m-%d %H:%M:%S') ==="
            printf "%s" "$appended_block"
        } >> "$CONFIG_FILE"
        ok "Миграция конфига: добавлено новых ключей — ${added}"
    else
        ok "Конфигурация актуальна, новых ключей не требуется"
    fi

    # Гарантируем разумные права
    chmod 600 "$CONFIG_FILE"
}

write_bot() {
    if [[ -f "$(dirname "$0")/bot.py" ]]; then
        cp "$(dirname "$0")/bot.py" "${INSTALL_DIR}/bot.py"
        cp "$(dirname "$0")/requirements.txt" "${INSTALL_DIR}/requirements.txt"
    else
        info "Загрузка bot.py..."
        curl -sL "${REPO_URL}/bot.py" -o "${INSTALL_DIR}/bot.py"
        curl -sL "${REPO_URL}/requirements.txt" -o "${INSTALL_DIR}/requirements.txt"
    fi
    ok "Файлы бота скопированы"
}

# ── Python venv + зависимости ──
setup_venv() {
    info "Создание / обновление виртуального окружения..."
    if [[ ! -d "$VENV_DIR" ]]; then
        "$PYTHON_CMD" -m venv "$VENV_DIR"
    fi
    "${VENV_DIR}/bin/pip" install --upgrade pip -q
    "${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q --upgrade
    ok "Python-зависимости установлены"
}

# ── Systemd-сервис ──
create_service() {
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<SVCEOF
[Unit]
Description=Telegram Support Ticket Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment="BOT_CONFIG=${CONFIG_FILE}"
ExecStart=${VENV_DIR}/bin/python ${INSTALL_DIR}/bot.py
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=5

# Безопасность
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR} ${BACKUP_DIR}
ProtectHome=true

# Логирование в journald
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
SVCEOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" > /dev/null 2>&1
    ok "Systemd-сервис создан и включён в автозапуск"
}

# ── Скрипт бэкапов ──
create_backup_script() {
    cat > "${INSTALL_DIR}/backup.sh" <<'BKEOF'
#!/usr/bin/env bash
# Ежедневный бэкап базы данных тикетного бота.
# Хранит последние 14 бэкапов.

set -euo pipefail

INSTALL_DIR="/opt/support-bot"
DATA_DIR="${INSTALL_DIR}/data"
BACKUP_DIR="${INSTALL_DIR}/backups"
DB_FILE="${DATA_DIR}/tickets.db"
MAX_BACKUPS=14

mkdir -p "$BACKUP_DIR"

if [[ ! -f "$DB_FILE" ]]; then
    echo "[backup] DB not found: $DB_FILE"
    exit 0
fi

TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
BACKUP_FILE="${BACKUP_DIR}/tickets_${TIMESTAMP}.db.gz"

# Используем sqlite3 .backup для консистентной копии
TEMP_BACKUP=$(mktemp)
sqlite3 "$DB_FILE" ".backup '${TEMP_BACKUP}'"
gzip -c "$TEMP_BACKUP" > "$BACKUP_FILE"
rm -f "$TEMP_BACKUP"

# Удаляем старые бэкапы
cd "$BACKUP_DIR"
ls -t tickets_*.db.gz 2>/dev/null | tail -n +$((MAX_BACKUPS + 1)) | xargs -r rm -f

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[backup] Created: $BACKUP_FILE ($SIZE)"
BKEOF

    chmod +x "${INSTALL_DIR}/backup.sh"
    ok "Скрипт бэкапов создан"
}

# ── Cron для бэкапов ──
setup_cron_backup() {
    local cron_line="0 3 * * * ${INSTALL_DIR}/backup.sh >> ${DATA_DIR}/backup.log 2>&1"
    (crontab -l 2>/dev/null | grep -v "support-bot/backup.sh" ; echo "$cron_line") | crontab -
    ok "Cron-бэкап настроен: ежедневно в 03:00"
}

# ── Logrotate ──
setup_logrotate() {
    cat > "/etc/logrotate.d/${SERVICE_NAME}" <<LREOF
${DATA_DIR}/bot.log {
    daily
    missingok
    rotate 10
    compress
    delaycompress
    notifempty
    maxsize 50M
    create 0640 root root
    postrotate
        systemctl reload ${SERVICE_NAME} > /dev/null 2>&1 || true
    endscript
}

${DATA_DIR}/backup.log {
    weekly
    missingok
    rotate 4
    compress
    notifempty
    maxsize 10M
    create 0640 root root
}
LREOF

    ok "Logrotate настроен (макс. ~500 МБ логов суммарно)"
}

# ── Скрипт удаления ──
create_uninstall() {
    cat > "${INSTALL_DIR}/uninstall.sh" <<'UNEOF'
#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SERVICE_NAME="support-bot"
INSTALL_DIR="/opt/support-bot"

echo -e "${YELLOW}⚠️  Вы собираетесь ПОЛНОСТЬЮ удалить Support Bot${NC}"
echo ""
read -rp "Удалить бота и ВСЕ данные? (yes/no): " confirm
if [[ "$confirm" != "yes" ]]; then
    echo "Отменено."
    exit 0
fi

echo "Остановка сервиса..."
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

echo "Удаление logrotate..."
rm -f "/etc/logrotate.d/${SERVICE_NAME}"

echo "Удаление cron..."
(crontab -l 2>/dev/null | grep -v "support-bot/backup.sh") | crontab - 2>/dev/null || true

read -rp "Удалить все файлы включая бэкапы? (yes/no): " del_files
if [[ "$del_files" == "yes" ]]; then
    rm -rf "$INSTALL_DIR"
    echo -e "${GREEN}✅ Всё удалено.${NC}"
else
    rm -rf "${INSTALL_DIR}/venv" "${INSTALL_DIR}/bot.py" "${INSTALL_DIR}/requirements.txt"
    echo -e "${GREEN}✅ Бот удалён, данные и бэкапы сохранены в ${INSTALL_DIR}${NC}"
fi
UNEOF

    chmod +x "${INSTALL_DIR}/uninstall.sh"
    ok "Скрипт удаления создан: ${INSTALL_DIR}/uninstall.sh"
}

# ── Запуск бота ──
start_bot() {
    info "Запуск бота..."
    systemctl restart "$SERVICE_NAME"
    sleep 2

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        ok "Бот запущен и работает!"
    else
        err "Бот не запустился. Смотрите логи:"
        echo "    journalctl -u ${SERVICE_NAME} -n 30 --no-pager"
        echo "    cat ${DATA_DIR}/bot.log"
        exit 1
    fi
}

# ── Финальный вывод ──
print_summary() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════╗${NC}"
    if [[ "$UPGRADE" -eq 1 ]]; then
        echo -e "${BOLD}║          ✅ Обновление завершено!           ║${NC}"
    else
        echo -e "${BOLD}║          ✅ Установка завершена!             ║${NC}"
    fi
    echo -e "${BOLD}╚══════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  📁 Директория:   ${CYAN}${INSTALL_DIR}${NC}"
    echo -e "  ⚙️  Конфигурация: ${CYAN}${CONFIG_FILE}${NC}"
    echo -e "  🗄  База данных:  ${CYAN}${DATA_DIR}/tickets.db${NC}"
    echo -e "  📋 Логи:         ${CYAN}${DATA_DIR}/bot.log${NC}"
    echo -e "  💾 Бэкапы:       ${CYAN}${BACKUP_DIR}/${NC}"
    echo ""
    echo -e "  ${BOLD}Управление:${NC}"
    echo -e "    systemctl status  ${SERVICE_NAME}   — статус"
    echo -e "    systemctl restart ${SERVICE_NAME}   — перезапуск"
    echo -e "    systemctl stop    ${SERVICE_NAME}   — остановка"
    echo -e "    journalctl -u ${SERVICE_NAME} -f    — лог в реальном времени"
    echo ""
    echo -e "  ${BOLD}Конфигурация:${NC}"
    echo -e "    nano ${CONFIG_FILE}"
    echo -e "    systemctl restart ${SERVICE_NAME}"
    echo ""
    echo -e "  ${BOLD}Бэкап вручную:${NC}"
    echo -e "    ${INSTALL_DIR}/backup.sh"
    echo ""
    echo -e "  ${BOLD}Удаление:${NC}"
    echo -e "    ${INSTALL_DIR}/uninstall.sh"
    echo ""
    echo -e "  ${BOLD}Новое в этой версии:${NC}"
    echo -e "    • суточный лимит тикетов:        ${CYAN}MAX_TICKETS_PER_DAY${NC}"
    echo -e "    • кулдаун между тикетами:        ${CYAN}TICKET_CREATE_COOLDOWN_MINUTES${NC}"
    echo -e "    • постоянное меню (нижние кнопки) у юзера и админа"
    echo -e "    • меню \"/\" возле скрепки (set_my_commands)"
    echo -e "    • имя темы при закрытии: ${CYAN}🔴 🛠 #N | @user (id)${NC}"
    echo -e "    • БД пишет ${CYAN}closed_by${NC} (user/admin/system) и ${CYAN}closed_by_user_id${NC}"
    echo -e "    • reconcile при старте: сверяет темы Telegram <-> БД"
    echo ""
    echo -e "  ${YELLOW}⚠️  Не забудьте включить Темы (Topics) в настройках Telegram-группы!${NC}"
    echo -e "  ${YELLOW}⚠️  Бот должен быть администратором в этой группе.${NC}"
    echo ""
}

# ── Проверка: уже установлен? ──
check_existing() {
    if [[ -f "${CONFIG_FILE}" ]]; then
        echo ""
        warn "Обнаружена существующая установка!"
        echo ""
        echo "  1) Обновить (код и зависимости, данные и конфиг сохраняются,"
        echo "     новые ключи дописываются автоматически)"
        echo "  2) Отмена"
        echo ""
        read -rp "  Выбор [1/2]: " choice
        case "$choice" in
            1)
                info "Обновление... данные и конфиг сохраняются."
                systemctl stop "$SERVICE_NAME" 2>/dev/null || true
                UPGRADE=1
                ;;
            *)
                info "Отменено."
                exit 0
                ;;
        esac
    fi
}

# ══════════════════════════════
# MAIN
# ══════════════════════════════
main() {
    UPGRADE=0

    header
    check_root
    check_os
    check_existing

    if [[ "$UPGRADE" -eq 0 ]]; then
        configure
    fi

    install_system_deps
    install_python
    create_structure

    if [[ "$UPGRADE" -eq 0 ]]; then
        write_config
    else
        migrate_config
    fi

    write_bot
    setup_venv
    create_service
    create_backup_script
    setup_cron_backup
    setup_logrotate
    create_uninstall
    start_bot
    print_summary
}

main "$@"
