#!/usr/bin/env bash
# 大肥鱼 QQ 机器人 · 1 GiB 云服务器部署脚本（Ubuntu 24.04，宿主机直装，不用 Docker）
#
# 用法：把项目上传到 /opt/bigfish 后，在服务器上执行
#     sudo bash /opt/bigfish/deploy/linux/install-1g.sh
#
# 做四件事：加 2G swap、精简系统释放内存、装 Python 依赖、装 NapCat（官方脚本，非 Docker）。
# 脚本可重复执行，已做过的步骤会跳过。

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/bigfish}"
APP_USER="${APP_USER:-bigfish}"
SWAP_SIZE_MB="${SWAP_SIZE_MB:-2048}"
PYPI_MIRROR="${PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
NAPCAT_PROXY="${NAPCAT_PROXY:-0}"
NAPCAT_DIR="/home/${APP_USER}/Napcat"

# 下载 install.sh 的地址：用加速站时把加速前缀拼到原始 URL 前面，直连则用原始地址
if [[ "${NAPCAT_PROXY}" == "0" ]]; then
    NAPCAT_SCRIPT_URL="https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.sh"
else
    NAPCAT_SCRIPT_URL="${NAPCAT_PROXY%/}/https://raw.githubusercontent.com/NapNeko/NapCat-Installer/main/script/install.sh"
fi

say()  { printf '\n\033[1;36m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die "请用 sudo 执行：sudo bash $0"

# ---------- 0. 系统检查 ----------
say "0/6 检查系统"
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    ok "系统：${PRETTY_NAME:-unknown}"
    [[ "${ID:-}" == "ubuntu" ]] || warn "本脚本按 Ubuntu 编写，其他发行版可能需要在依赖步骤手动调整"
fi
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
ok "物理内存：${MEM_MB} MiB"

# ---------- 1. swap ----------
say "1/6 配置 ${SWAP_SIZE_MB}MB swap（防止 1G 内存被 OOM 杀掉）"
if swapon --show | grep -q /swapfile; then
    ok "swap 已存在：$(free -m | awk '/Swap:/ {print $2}') MiB"
else
    if ! fallocate -l "${SWAP_SIZE_MB}M" /swapfile 2>/dev/null; then
        warn "fallocate 不可用，改用 dd 写入（慢一点，请等）"
        dd if=/dev/zero of=/swapfile bs=1M count="${SWAP_SIZE_MB}" status=none
    fi
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    ok "已启用 swap：$(free -m | awk '/Swap:/ {print $2}') MiB"
fi

# ---------- 2. 释放内存 ----------
say "2/6 精简系统（关掉对本项目没用的服务）"
for unit in snapd.service snapd.socket apt-daily.timer apt-daily-upgrade.timer motd-news.timer; do
    if systemctl is-enabled "${unit}" >/dev/null 2>&1; then
        systemctl disable --now "${unit}" >/dev/null 2>&1 || true
        ok "已停止 ${unit}"
    fi
done

# ---------- 3. 系统依赖 ----------
say "3/6 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates tar >/dev/null
ok "python3 $(python3 -V 2>&1 | awk '{print $2}') / venv / pip 就绪"

# ---------- 4. 运行用户 ----------
say "4/6 准备运行用户 ${APP_USER}"
if id -u "${APP_USER}" >/dev/null 2>&1; then
    ok "用户已存在"
else
    useradd -m -s /bin/bash "${APP_USER}"
    ok "已创建用户 ${APP_USER}"
fi
# NapCat 官方安装脚本需要自己调用 sudo 装依赖，这里给免密；只用在这台专用机器上。
if [[ ! -f /etc/sudoers.d/90-bigfish ]]; then
    echo "${APP_USER} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-bigfish
    chmod 440 /etc/sudoers.d/90-bigfish
    ok "已授予 ${APP_USER} 免密 sudo（安装依赖用）"
fi

# ---------- 5. 项目与 Python 依赖 ----------
say "5/6 检查项目文件与 Python 依赖"
[[ -f "${APP_DIR}/run.py" && -d "${APP_DIR}/app" ]] || \
    die "没找到 ${APP_DIR}/run.py 或 ${APP_DIR}/app，请先把项目上传到 ${APP_DIR}（见 README）"

mkdir -p "${APP_DIR}/data"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
ok "已把 ${APP_DIR} 归属给 ${APP_USER}"

sudo -u "${APP_USER}" bash -lc "
    set -e
    cd '${APP_DIR}'
    [[ -x .venv/bin/python ]] || python3 -m venv .venv
    .venv/bin/python -m pip install -q --upgrade pip -i '${PYPI_MIRROR}'
    .venv/bin/python -m pip install -q -r requirements.txt -i '${PYPI_MIRROR}'
"
ok "Python 依赖安装完成"

# .env 缺失时交互生成（不写入任何默认密钥）
if [[ ! -f "${APP_DIR}/.env" ]]; then
    warn "${APP_DIR}/.env 不存在，现在生成"
    read -r -s -p "  DeepSeek API Key（粘贴后回车，不回显）: " DS_KEY; echo
    read -r -p "  OneBot Token（NapCat 里也要填一样，随便一串字母数字）: " OB_TOKEN
    read -r -p "  管理员 QQ 号（群聊 /reset 用，可留空）: " ADMIN_QQ
    [[ -n "${DS_KEY}" ]] || die "DeepSeek API Key 不能为空"
    [[ -n "${OB_TOKEN}" ]] || die "OneBot Token 不能为空（留空会让本机 8080 端口无鉴权）"
    cat > "${APP_DIR}/.env" <<ENVFILE
# 本文件由 deploy/linux/install-1g.sh 生成，权限 600，不要提交到仓库
BOT_NAME=大肥鱼
BOT_ALIASES=大肥鱼,肥鱼
ADMIN_QQ=${ADMIN_QQ}

DEEPSEEK_API_KEY=${DS_KEY}
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_TIMEOUT=60

ONEBOT_HOST=127.0.0.1
ONEBOT_PORT=8080
ONEBOT_PATH=/onebot/v11/ws
ONEBOT_TOKEN=${OB_TOKEN}

MAX_REPLY_CHARS=60
MAX_REPLY_SEGMENTS=3
CONTEXT_MESSAGES=20
SUMMARY_TRIGGER=40

RATE_MIN_INTERVAL=0
RATE_PER_MINUTE=60
GROUP_REPLY_PROBABILITY=1
GROUP_COOLDOWN=0
QUOTE_REPLY=true

KNOWLEDGE_FILE=knowledge/hyacine.md
KNOWLEDGE_TOP_K=2

DATA_DIR=data
LOG_LEVEL=INFO
ENVFILE
    chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
    chmod 600 "${APP_DIR}/.env"
    ok "已生成 ${APP_DIR}/.env（权限 600）"
else
    ok ".env 已存在，未改动"
fi

# ---------- 6. NapCat（官方脚本直装，不用 Docker） ----------
say "6/6 安装 NapCat"
if [[ -x "${NAPCAT_DIR}/opt/QQ/qq" ]]; then
    ok "NapCat 已安装在 ${NAPCAT_DIR}"
else
    sudo -u "${APP_USER}" bash -lc "
        set -e
        cd \"\$HOME\"
        curl -fL --connect-timeout 20 --max-time 1800 -o napcat.sh '${NAPCAT_SCRIPT_URL}'
        bash napcat.sh --docker n --cli n --github-proxy '${NAPCAT_PROXY}'
    " || die "NapCat 安装失败。常见原因：GitHub 直连不通。重试时把 NAPCAT_PROXY 变成加速站地址，例如 sudo NAPCAT_PROXY=https://ghproxy.net bash $0"
    ok "NapCat 安装完成：${NAPCAT_DIR}"
fi

# ---------- systemd 服务 ----------
say "写入两个开机自启服务"

cat > /etc/systemd/system/bigfish-bot.service <<UNIT
[Unit]
Description=Da Fei Yu QQ bot (OneBot 11 reverse WS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/python run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/napcat.service <<UNIT
[Unit]
Description=NapCat QQ client (headless via Xvfb)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=/home/${APP_USER}/Napcat
ExecStart=/usr/bin/xvfb-run -a ${NAPCAT_DIR}/opt/QQ/qq --no-sandbox
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
LimitCORE=0

[Install]
WantedBy=multi-user.target
UNIT

# 日志上限：30G 系统盘，journald 默认最多能吃 3G，这里压到 200M
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/99-bigfish.conf <<UNIT
[Journal]
SystemMaxUse=200M
SystemMaxFileSize=20M
UNIT

systemctl daemon-reload
systemctl restart systemd-journald
systemctl enable --now bigfish-bot.service >/dev/null 2>&1 \
    || warn "bigfish-bot 启动失败，看 journalctl -u bigfish-bot -n 50"
systemctl enable --now napcat.service >/dev/null 2>&1 \
    || warn "napcat 启动失败，看 journalctl -u napcat -n 50"
ok "bigfish-bot.service / napcat.service 已启用"

# ---------- 完成 ----------
cat <<TIP

============================================================
 装好了。剩下三步要你手动做：

 1) 配 SSH 隧道（在你自己的 Windows 上开一个 PowerShell，保持窗口不关）
      ssh -N -L 6099:127.0.0.1:6099 <你的SSH用户名>@<服务器公网IP>   # 阿里云 Ubuntu 镜像默认是 admin

 2) 浏览器打开 http://127.0.0.1:6099/webui ：
    - 用 NapCat 日志里打印的 token 登录（journalctl -u napcat -n 50 --no-pager）
    - 扫码登录 QQ 小号（别用主号）
    - 网络配置里新建「反向 WebSocket」：
        ws://127.0.0.1:8080/onebot/v11/ws
      Token 填 .env 里的 ONEBOT_TOKEN

 3) 看日志确认接通
      systemctl status bigfish-bot
      journalctl -u bigfish-bot -f
    看到「大肥鱼已启动」「NapCat 已连接」即成功。

 常用命令
      systemctl restart bigfish-bot napcat
      journalctl -u napcat -f
      free -h && df -h

 内存参考（1G 机器）
      free -h 里 available 长期低于 150MiB 会开始卡；
      这时把 LLM_TIMEOUT 降到 60，或换 2G 套餐。
============================================================
TIP
