#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

PYTHON_BIN="/usr/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi

if [ -z "$PYTHON_BIN" ]; then
    echo "Python 3 not found" >&2
    exit 127
fi

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/manager.log"

if [ "$#" -eq 0 ]; then
    set -- --execute
fi

TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
{
    echo "================================================================================"
    echo "[$TIMESTAMP] 开始执行 HHanClub 保种区自动化综合管理流水线: $*"
    echo "================================================================================"
} >> "$LOG_FILE"

"$PYTHON_BIN" "$SCRIPT_DIR/hhan_pzone_manager.py" "$@" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

END_TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
{
    echo "================================================================================"
    echo "[$END_TIMESTAMP] 执行完成，退出状态码: $EXIT_CODE"
    echo "================================================================================"
    echo ""
} >> "$LOG_FILE"

exit $EXIT_CODE
