#!/usr/bin/env bash
set -euo pipefail

# Коди виходу:
# 0 - успіх
# 1 - невірні аргументи або відсутній обов'язковий параметр
# 2 - тека-джерело не існує або недоступна для читання
# 3 - помилка створення теки призначення
# 4 - помилка під час створення архіву

SOURCE=""
DEST=""
KEEP=5
DRY_RUN=false

print_help() {
    cat <<HELP
Використання: $(basename "$0") --source <шлях> --dest <шлях> [--keep <N>] [--dry-run] [--help]

Створює стиснений архів (tar.gz) вказаної теки та виконує ротацію старих копій.

Параметри:
  --source <шлях>   Обов'язковий. Тека, яку потрібно заархівувати.
  --dest <шлях>     Обов'язковий. Тека для збереження архівів.
  --keep <N>        Необов'язковий. Кількість останніх архівів для збереження (типово: 5).
  --dry-run         Необов'язковий. Показати дії без створення чи видалення файлів.
  --help            Вивести цю довідку та вийти.

Приклад запуску:
  ./backup.sh --source "/Users/tania/Documents/My Project" --dest "./backups" --keep 3
HELP
}

log_msg() {
    local level="$1"
    local message="$2"
    local timestamp
    timestamp=$(date "+%Y-%m-%d %H:%M:%S")
    local line="[$timestamp] [$level] $message"
    echo "$line"
    if [[ "$DRY_RUN" == false && -d "$DEST" ]]; then
        echo "$line" >> "$DEST/backup.log"
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            [[ $# -lt 2 ]] && { echo "Помилка: параметр --source потребує значення." >&2; exit 1; }
            SOURCE="$2"
            shift 2
            ;;
        --dest)
            [[ $# -lt 2 ]] && { echo "Помилка: параметр --dest потребує значення." >&2; exit 1; }
            DEST="$2"
            shift 2
            ;;
        --keep)
            [[ $# -lt 2 ]] && { echo "Помилка: параметр --keep потребує значення." >&2; exit 1; }
            KEEP="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help)
            print_help
            exit 0
            ;;
        *)
            echo "Помилка: невідомий параметр '$1'. Використайте --help." >&2
            exit 1
            ;;
    esac
done

if [[ -z "$SOURCE" || -z "$DEST" ]]; then
    echo "Помилка: параметри --source та --dest є обов'язковими. Використайте --help." >&2
    exit 1
fi

if ! [[ "$KEEP" =~ ^[1-9][0-9]*$ ]]; then
    echo "Помилка: значення --keep має бути цілим додатним числом (отримано: '$KEEP')." >&2
    exit 1
fi

if [[ ! -d "$SOURCE" || ! -r "$SOURCE" ]]; then
    echo "Помилка: тека-джерело '$SOURCE' не існує або недоступна для читання." >&2
    exit 2
fi

SOURCE_CLEAN="${SOURCE%/}"
[[ -z "$SOURCE_CLEAN" ]] && SOURCE_CLEAN="/"
SOURCE_NAME=$(basename "$SOURCE_CLEAN")
SOURCE_PARENT=$(dirname "$SOURCE_CLEAN")

if [[ ! -d "$DEST" ]]; then
    if [[ "$DRY_RUN" == true ]]; then
        log_msg "DRY-RUN" "Було б створено теку призначення: '$DEST'"
    else
        mkdir -p "$DEST" || {
            echo "Помилка: не вдалося створити теку призначення '$DEST'." >&2
            exit 3
        }
        log_msg "INFO" "Створено теку призначення: '$DEST'"
    fi
fi

TIMESTAMP=$(date "+%Y-%m-%d-%H%M%S")
ARCHIVE_NAME="${SOURCE_NAME}-${TIMESTAMP}.tar.gz"
ARCHIVE_PATH="${DEST%/}/${ARCHIVE_NAME}"

if [[ -e "$ARCHIVE_PATH" ]]; then
    COUNTER=1
    while [[ -e "${DEST%/}/${SOURCE_NAME}-${TIMESTAMP}-${COUNTER}.tar.gz" ]]; do
        ((COUNTER++))
    done
    ARCHIVE_NAME="${SOURCE_NAME}-${TIMESTAMP}-${COUNTER}.tar.gz"
    ARCHIVE_PATH="${DEST%/}/${ARCHIVE_NAME}"
fi

FILES_COUNT=$(find "$SOURCE_CLEAN" -mindepth 1 | wc -l | tr -d ' ')

if [[ "$DRY_RUN" == true ]]; then
    log_msg "DRY-RUN" "Було б створено архів '$ARCHIVE_PATH' (об'єктів у джерелі: $FILES_COUNT)"
    ARCHIVE_SIZE="0B (dry-run)"
else
    if ! tar -czf "$ARCHIVE_PATH" -C "$SOURCE_PARENT" "$SOURCE_NAME"; then
        log_msg "ERROR" "Не вдалося створити архів '$ARCHIVE_PATH'"
        exit 4
    fi
    ARCHIVE_SIZE=$(du -h "$ARCHIVE_PATH" | cut -f1 | tr -d ' ')
    log_msg "INFO" "Створено архів '$ARCHIVE_NAME' (розмір: $ARCHIVE_SIZE, заархівовано об'єктів: $FILES_COUNT)"
fi

EXISTING_ARCHIVES=()
if [[ -d "$DEST" ]]; then
    while IFS= read -r -d '' file; do
        EXISTING_ARCHIVES+=("$file")
    done < <(find "$DEST" -maxdepth 1 -type f -name "${SOURCE_NAME}-*.tar.gz" -print0 | sort -z)
fi

TOTAL_COUNT=${#EXISTING_ARCHIVES[@]}
EFFECTIVE_COUNT=$TOTAL_COUNT
if [[ "$DRY_RUN" == true ]]; then
    EFFECTIVE_COUNT=$((TOTAL_COUNT + 1))
fi

DELETED_COUNT=0
if (( EFFECTIVE_COUNT > KEEP )); then
    TO_DELETE=$((EFFECTIVE_COUNT - KEEP))
    for (( i=0; i<TO_DELETE && i<TOTAL_COUNT; i++ )); do
        OLD_FILE="${EXISTING_ARCHIVES[$i]}"
        if [[ "$DRY_RUN" == true ]]; then
            log_msg "DRY-RUN" "Було б видалено старий архів: '$(basename "$OLD_FILE")'"
        else
            rm -f "$OLD_FILE"
            log_msg "INFO" "Видалено старий архів: '$(basename "$OLD_FILE")'"
        fi
        ((DELETED_COUNT++))
    done
fi

if [[ "$DRY_RUN" == true ]]; then
    log_msg "DRY-RUN" "Підсумок: заархівовано б об'єктів: $FILES_COUNT | видалено б архівів: $DELETED_COUNT | ліміт збереження: $KEEP"
else
    log_msg "SUCCESS" "Підсумок: заархівовано об'єктів: $FILES_COUNT | видалено старих архівів: $DELETED_COUNT | розмір архіву: $ARCHIVE_SIZE"
fi

exit 0
