#!/usr/bin/env bash
# NapCat 登录守护。
# 被腾讯踢下线后，最多重启 RETRY_LIMIT 次尝试快速登录。
# 快速登录始终失败时直接停止 napcat，不保留二维码。
# 需要扫码时：先 sudo systemctl stop napcat-qr-guard，再 sudo systemctl start napcat，
# 在 WebUI 扫码；登录成功后执行 sudo systemctl start napcat-qr-guard。

set -u

QR_LIMIT="${NAPCAT_QR_FAIL_LIMIT:-1}"
RETRY_LIMIT="${NAPCAT_RELOGIN_LIMIT:-3}"
RETRY_DELAY="${NAPCAT_RELOGIN_DELAY:-8}"
TAG="${NAPCAT_QR_GUARD_TAG:-napcat-qr-guard}"
JOURNAL_CMD="${NAPCAT_QR_GUARD_JOURNAL_CMD:-journalctl -u napcat -n 0 -f -o cat}"
STOP_CMD="${NAPCAT_QR_GUARD_STOP_CMD:-systemctl stop napcat}"
RESTART_CMD="${NAPCAT_RELOGIN_RESTART_CMD:-systemctl restart napcat}"

retrying=0
qr_expired=0
log() { logger -t "$TAG" "$*"; }

log "守护启动：被踢后最多重试 ${RETRY_LIMIT} 次快速登录；二维码过期 ${QR_LIMIT} 次即停止"

# shellcheck disable=SC2086
$JOURNAL_CMD | while IFS= read -r line; do
    case "$line" in
        *"OneBot11 适配器初始化完成"*)
            if [ "$retrying" -eq 1 ] || [ "$qr_expired" -gt 0 ]; then
                log "登录成功，重试与过期计数清零"
            fi
            retrying=0
            qr_expired=0
            ;;

        *"KickedOffLine"*)
            if [ "$retrying" -eq 0 ]; then
                retrying=1
                qr_expired=0
                log "检测到被腾讯踢下线，开始快速登录重试"
                i=1
                while [ "$i" -le "$RETRY_LIMIT" ]; do
                    log "快速登录第 ${i}/${RETRY_LIMIT} 次尝试：重启 napcat"
                    $RESTART_CMD
                    sleep "$RETRY_DELAY"
                    # 如果这次快速登录已经成功，不再继续重启
                    if journalctl -u napcat --since '-20 seconds' --no-pager -q 2>/dev/null \
                        | grep -q "OneBot11 适配器初始化完成"; then
                        log "快速登录成功，停止重试"
                        retrying=0
                        break
                    fi
                    i=$((i + 1))
                done
                if [ "$retrying" -eq 1 ]; then
                    log "快速登录已重试 ${RETRY_LIMIT} 次仍失败，停止 napcat；需要登录时先停守护再启动"
                    $STOP_CMD
                    exit 0
                fi
            fi
            ;;

        *"Login Error"*)
            qr_expired=$((qr_expired + 1))
            log "二维码已过期（第 ${qr_expired}/${QR_LIMIT} 次）"
            if [ "$qr_expired" -ge "$QR_LIMIT" ]; then
                log "二维码过期达到 ${QR_LIMIT} 次，停止 napcat；恢复请先停守护再启动 napcat"
                $STOP_CMD
                exit 0
            fi
            ;;
    esac
done
