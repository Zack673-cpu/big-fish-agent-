# 大肥鱼 QQ 聊天机器人

大肥鱼是一个跑在自己电脑上的 QQ 聊天机器人：性格活泼可爱、知识面广，用 DeepSeek 大模型生成回复，NapCat 负责 QQ 接入，对话记录和长期记忆保存在本地 SQLite。

## 功能

- 私聊直接聊；群聊默认每条消息都接话，也可以改成只在被 @ 或叫名字时回复
- 表情、表情包和图片交给模型理解后生成回复（QQ 原生小表情以表情名传给模型）
- 被双击头像「戳一戳」时由模型即兴回应；群里戳别人也会接一句
- 语气参考《崩坏：星穹铁道》风堇，并从本地语料库检索片段注入提示词（RAG，不改模型权重）
- 按会话保存上下文，聊久了自动生成摘要，避免上下文越滚越大
- `/remember` 长期记忆，`/reset` 清空当前会话上下文
- 回复自动分段、限流，避免刷屏；大模型出错时有友好兜底
- 所有数据只存在本机 `data/bot.db`，不经过第三方存储

## 目录结构

```
app/
  config.py            配置加载（.env）
  logging_setup.py     日志
  main.py              FastAPI 入口，接收 NapCat 反向 WS
  core/
    persona.py         大肥鱼人格与提示词
    llm.py             DeepSeek 接口封装
    memory.py          SQLite 存储（消息/摘要/长期记忆）
    text.py            文本清洗、别名识别、分段
    trigger.py         触发判断与限流
    commands.py        斜杠指令
    knowledge.py       人格语料检索（RAG）
    engine.py          消息处理主流程
  onebot/
    events.py          OneBot 11 事件解析
    client.py          反向 WS 连接与发消息
knowledge/
  hyacine.md           风堇语料库（可自行增删）
tests/                 单元测试
deploy/linux/          1 GiB 云服务器部署脚本
docs/项目文档.md       可行性/需求/设计/测试文档
docs/部署踩坑记录.md   上云部署踩过的坑与正确做法
```

## 快速开始（Windows 本机）

### 1. 准备 Python

需要 Python 3.10 以上（本项目在 3.12.8 上开发）。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2. 填配置

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`：

- `DEEPSEEK_API_KEY`：在 <https://platform.deepseek.com> 申请
- `ONEBOT_TOKEN`：自己定一个随机字符串，稍后要和 NapCat 里填的一致
- `ADMIN_QQ`：你自己的 QQ 号（群里只有它能用 `/reset`）

### 3. 安装并登录 NapCat

1. 从 <https://napneko.github.io/guide/install> 下载 Windows 一键包 `NapCat.Shell.Windows.OneKey.zip`，解压启动。
2. 用**备用 QQ 小号**扫码登录。不要用主号，NapCat 属于非官方协议，存在风控/封号风险。
3. 打开 NapCat WebUI，进入「网络配置」，新建**反向 WebSocket**：
   - URL：`ws://127.0.0.1:8080/onebot/v11/ws`
   - Token：与 `.env` 的 `ONEBOT_TOKEN` 保持一致
4. 保存并启用该配置。

参考文档：<https://napneko.github.io/guide/boot/Shell>、<https://napneko.github.io/use/integration>。

### 4. 好友与群邀请：只由你手动确认

本程序**不会调用**同意好友或同意入群的接口，收到请求时只在日志里记录一行（例如「收到好友请求（不会自动通过）：QQ xxxx」），然后继续忽略。所有请求都请你在小号上手动点确认。

如果发现小号仍然自动通过，说明是 QQ 账号自己的设置，不是本程序的行为，请在小号上检查：

1. 手机 QQ → 设置 → 隐私 → 「加我为好友时的验证设置」：不要选「允许任何人」，改成「需要验证信息」。
2. 手机 QQ → 设置 → 隐私 → 「加群方式」相关选项：不要选「允许任何人邀请我入群」，改成需要验证。
3. 进入机器人所在的群 → 右上角群设置 → 关闭与「自动通过入群邀请」相关的开关。

改完再用另一个号试一次：请求应该停在「新的朋友 / 群通知」里，等你手动处理。

### 5. 启动机器人

```powershell
.\.venv\Scripts\python.exe run.py
```

看到 `大肥鱼已启动` 和 `NapCat 已连接` 就说明接通了。用另一个 QQ 私聊小号，或把它拉进群 @它试试。

> 顺序建议：先启动本程序，再在 NapCat 里启用反向 WS；或者启用后重启本程序。两者都行，但必须保证地址和 token 一致。

## 指令

| 指令 | 说明 | 使用范围 |
| --- | --- | --- |
| `/help` | 查看说明 | 私聊；群聊需 @ 或叫名字 |
| `/ping` | 检查是否在线 | 私聊；群聊需 @ 或叫名字 |
| `/reset` | 清空当前会话上下文（保留长期记忆） | 私聊本人；群聊仅 `ADMIN_QQ` |
| `/remember 内容` | 记住一件事 | 仅私聊 |
| `/memories` | 查看记住的事 | 仅私聊 |
| `/forget` | 忘掉所有记住的事 | 仅私聊 |

## 常用配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BOT_NAME` | 大肥鱼 | 机器人名字，写进人格提示 |
| `BOT_ALIASES` | 大肥鱼,肥鱼 | 群聊触发别名，逗号分隔 |
| `LLM_MODEL` | deepseek-flash | DeepSeek API 模型 id（显示名为 DeepSeek-V4.1-Flash） |
| `MAX_REPLY_CHARS` | 60 | 每段回复最大字数 |
| `MAX_REPLY_SEGMENTS` | 3 | 一条回复最多分几段 |
| `CONTEXT_MESSAGES` | 20 | 每次带给模型的历史消息条数 |
| `SUMMARY_TRIGGER` | 40 | 未摘要消息达到多少条时生成摘要 |
| `RATE_MIN_INTERVAL` | 0 | 同一会话两次回复的最小间隔（秒） |
| `RATE_PER_MINUTE` | 60 | 同一会话每分钟最多回复次数（安全上限） |
| `GROUP_REPLY_PROBABILITY` | 1 | 群聊接话概率，1 表示每条都接；设为 0 则只回 @ 和叫名字 |
| `GROUP_COOLDOWN` | 0 | 随机接话冷却时间（秒） |
| `KNOWLEDGE_FILE` | knowledge/hyacine.md | 人格语料文件路径 |
| `KNOWLEDGE_TOP_K` | 2 | 每次最多注入几段语料 |
| `QUOTE_REPLY` | true | 群聊回复是否引用原消息 |
| `DATA_DIR` | data | SQLite 数据目录 |

## 人格语料（RAG）

`knowledge/hyacine.md` 是风堇的语料库。每收到一条消息，程序按中文字符重叠度从语料里挑出最相关的 `KNOWLEDGE_TOP_K` 段（默认 2 段），作为参考放进系统提示词。这叫「检索增强生成」（RAG）：**没有微调模型，也不会上传语料**，只影响这一次提示词的内容。

- 想加语料：在 `knowledge/hyacine.md` 里按 `## 小节标题` 追加内容，保存后重启程序生效。
- 语气描述（温柔、先关心情绪、庭院/小伊卡意象）写在 `app/core/persona.py` 的 `HYACINE_STYLE`，不需要语料也能生效。
- 不想启用检索：把 `KNOWLEDGE_FILE` 指向一个不存在的文件即可，程序不会报错，只是没有语料片段。
- 图片识别会把图片内容发送给 DeepSeek；QQ 原生小表情没有图片文件，程序把表情名（如「/菜汪」）作为文字说明发给模型。介意图片上传的话，可以在 `app/core/engine.py` 的 `_build_llm_content` 里去掉图片下载逻辑。

## 给家里人用，需要上云吗

不需要。QQ 是客户端连腾讯服务器，家人从任何网络发消息都能到你这台常开的电脑；电脑上的 NapCat 再把消息交给本程序，不需要公网 IP，也不需要端口映射。

前提是这台电脑在家人要聊天的时候保持开机且联网。如果做不到，再考虑便宜的 2C2G 云服务器；免费云主机通常不稳定，不建议承载长期在线的 QQ 协议端。

注意：NapCat 使用非官方协议，账号有被风控或封禁的可能，只建议用备用小号。不要把主号挂上去。

## 云服务器部署（1 GiB 小内存，宿主机直装）

家里电脑做不到全天开机时用这套。1 GiB 内存跑 Docker 版 NapCat 容易 OOM 掉线，所以这里**不用 Docker**：官方脚本直接装 NapCat，再加 2G swap 兜底。

脚本：[deploy/linux/install-1g.sh](deploy/linux/install-1g.sh)

> 部署时踩过的坑（SSH 私钥密码短语、GitHub 加速、NapCat 配置路径、反向 WS 等）都记在 [docs/部署踩坑记录.md](docs/部署踩坑记录.md)，遇到问题先看这份。

### 1. 在本机打包上传

```powershell
tar -czf bigfish.tar.gz app knowledge run.py requirements.txt .env.example deploy
scp bigfish.tar.gz admin@<服务器公网IP>:/opt/
```

### 2. 服务器上解压并一键安装

```bash
mkdir -p /opt/bigfish
tar -xzf /opt/bigfish.tar.gz -C /opt/bigfish
sudo bash /opt/bigfish/deploy/linux/install-1g.sh
```

脚本依次做：2G swap → 关掉 snapd / apt 定时任务释放内存 → 建 `bigfish` 用户 → 装 Python 依赖（清华源）→ 交互生成 `/opt/bigfish/.env`（API Key 不回显，权限 600）→ 装 NapCat → 写 `bigfish-bot.service` 和 `napcat.service` 两个开机自启服务 → 把 journald 日志上限压到 200M。

重复执行会跳过已完成的步骤。GitHub 直连不通时用加速站（加速站对 release 大文件会限速，换站前先 curl 测速，见踩坑记录）：

```bash
sudo NAPCAT_PROXY=https://gh-proxy.com bash /opt/bigfish/deploy/linux/install-1g.sh
```

### 3. 配 SSH 隧道并扫码

```powershell
ssh -N -L 6099:127.0.0.1:6099 admin@<服务器公网IP>
```

保持这个窗口不关，浏览器打开 `http://127.0.0.1:6099/webui`，用 `journalctl -u napcat -n 50 --no-pager` 里打印的 token 登录，扫小号的码，然后在网络配置里新建反向 WebSocket：`ws://127.0.0.1:8080/onebot/v11/ws`，Token 与 `.env` 的 `ONEBOT_TOKEN` 一致。

> 网络配置请一次写完整：改完要重启 NapCat 才生效，而重启会让 QQ 掉登录、必须重新扫码。`bigfish-bot` 重启则不影响登录，可以随时重启。

### 4. 检查

```bash
systemctl status bigfish-bot napcat
journalctl -u bigfish-bot -f
```

看到「大肥鱼已启动」和「NapCat 已连接」就是通了。

### 注意

- 6099 只监听本机、靠 SSH 隧道访问；安全组只留 22 端口，**不要放行** 8080 / 6099。
- 1 GiB 是下限：`free -h` 里 available 长期低于 150 MiB 就该换 2G 套餐。
- 试用到期会转计费，到期前在控制台续费或释放实例。
- 数据只在 `/opt/bigfish/data/bot.db`；释放实例前先用 `scp` 拉下来备份。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.\.venv\Scripts\python.exe -m pytest -q
```

## 常见问题

- **启动后没有「NapCat 已连接」**：检查 NapCat 反向 WS 的 URL 是否 `ws://127.0.0.1:8080/onebot/v11/ws`、token 是否一致、本程序是否已在运行。
- **能连上但群里 @ 没反应**：确认 NapCat 登录的是机器人小号，并且消息里是真正的 @（不是手打的「@大肥鱼」文字）。手打名字也能触发是别名功能。
- **回复很慢或报错**：检查 `DEEPSEEK_API_KEY` 是否有效、账户余额是否充足；日志会打印具体原因。
- **为什么还会自动通过好友/入群**：程序不会调用审批接口，只记录日志。请按「好友与群邀请：只由你手动确认」检查小号 QQ 自身的隐私与加群设置。
- **群聊每条都回，会不会太吵**：这是当前配置（`GROUP_REPLY_PROBABILITY=1`）；仍保留每分钟 60 条的安全上限。想安静一点就把概率调低或设为 0。
- **发表情/图片它会怎么回**：交给模型理解后生成回复，不是固定文案；图片会上传 DeepSeek 识别。
- **戳一戳为什么有时不回**：回复由模型生成，调用失败或触发每分钟上限时会跳过；机器人自己触发的事件也不接，避免自循环。
- **想换模型**：改 `LLM_MODEL` 即可，只要是 DeepSeek 的 OpenAI 兼容模型名。
- **数据在哪**：`data/bot.db`，删除前请先备份；`/reset` 只清当前会话上下文，`/forget` 只清长期记忆。

## 安全提醒

- `.env` 里有 API Key，不要截图、不要提交到仓库；`.gitignore` 已忽略。
- `ONEBOT_TOKEN` 不要留空，尤其不要把 8080 端口暴露到公网。
- 机器人会接触群里的公开聊天内容，提示词里已限制不泄露系统设定和隐私，但不要把它当保险箱，敏感信息不要发。
