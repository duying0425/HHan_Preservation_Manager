#!/bin/bash
# ==============================================================================
# HHanClub 保种区自动化综合管理运行脚本 (run.sh for Linux / Synology NAS)
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 优先使用用户目录或系统安装的 python3
PYTHON_BIN="/usr/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(which python3 || which python)"
fi

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/manager.log"

TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
echo "================================================================================" >> "$LOG_FILE"
echo "[$TIMESTAMP] 开始执行 HHanClub 保种区自动化综合管理流水线" >> "$LOG_FILE"
echo "================================================================================" >> "$LOG_FILE"

"$PYTHON_BIN" "$SCRIPT_DIR/hhan_pzone_manager.py" --execute >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

END_TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
echo "================================================================================" >> "$LOG_FILE"
echo "[$END_TIMESTAMP] 执行完成，退出状态码: $EXIT_CODE" >> "$LOG_FILE"
echo "================================================================================" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

exit $EXIT_CODE
