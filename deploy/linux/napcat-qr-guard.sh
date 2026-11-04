#!/usr/bin/env bash
# NapCat 二维码登录失败守护。
# 连续登录失败（二维码过期/Login Error）达到阈值后自动停止 napcat，
# 避免掉线后整夜反复生成二维码、加重腾讯风控。
# 恢复：确认账号状态正常后执行 sudo systemctl start napcat，并立即用手机扫码。

set -u

LIMIT="${NAPCAT_QR_FAIL_LIMIT:-3}"
TAG="${NAPCAT_QR_GUARD_TAG:-napcat-qr-guard}"
JOURNAL_CMD="${NAPCAT_QR_GUARD_JOURNAL_CMD:-journalctl -u napcat -n 0 -f -o cat}"
STOP_CMD="${NAPCAT_QR_GUARD_STOP_CMD:-systemctl stop napcat}"

fail_count=0
log() { logger -t "$TAG" "$*"; }

log "守护启动：连续登录失败达到 ${LIMIT} 次将自动停止 napcat"

# shellcheck disable=SC2086
$JOURNAL_CMD | while IFS= read -r line; do
    case "$line" in
        *"Login Error"*)
            fail_count=$((fail_count + 1))
            log "检测到登录失败/二维码过期（连续 ${fail_count}/${LIMIT} 次）"
            if [ "$fail_count" -ge "$LIMIT" ]; then
                log "因连续 ${LIMIT} 次登录失败已自动停止 napcat；恢复方法：确认账号状态正常后执行 sudo systemctl start napcat 并立即扫码"
                $STOP_CMD
                exit 0
            fi
            ;;
        *"OneBot11 适配器初始化完成"*)
            if [ "$fail_count" -gt 0 ]; then
                log "检测到登录成功，失败计数 ${fail_count} 清零"
            fi
            fail_count=0
            ;;
    esac
done
