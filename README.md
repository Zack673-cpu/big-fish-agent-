# 大肥鱼 QQ 聊天机器人

大肥鱼是一个跑在自己电脑上的 QQ 聊天机器人：性格活泼可爱、知识面广，用 DeepSeek 大模型生成回复，NapCat 负责 QQ 接入，对话记录和长期记忆保存在本地 SQLite。

## 功能

- 私聊直接聊；群聊默认每条消息都接话，也可以改成只在被 @ 或叫名字时回复
- 表情、表情包和图片交给模型理解后生成回复（QQ 原生小表情以表情名传给模型）
- 被双击头像「戳一戳」时由模型即兴回应；群里戳别人也会接一句
- 语气参考《崩坏：星穹铁道》风堇，并从本地语料库按语义检索片段注入提示词（百炼向量检索，失败自动回退字面检索；RAG，不改模型权重）
- 需要时效信息时自动联网搜索（DeepSeek 官方 Search API，复用同一个 Key），普通闲聊不会乱搜
- 天气问题走 Open-Meteo 结构化预报（免 Key），直接给出城市、日期、气温和降水概率，不再拿网页旧页面猜
- 消息里带 B 站或网页链接时自动抓取内容：B 站取标题、UP主、简介、标签、热评和弹幕，普通网页取标题和正文摘要，交给模型参考
- 能听懂别人发的语音（硅基流动 SenseVoice 转文字），也能自己决定用语音回答（Edge TTS 合成，免费无需 Key）
- 过年时有人发红包，会随机等几秒说句吉利话凑热闹（只发祝福，不抢钱、不发钱）
- 默认每天 08:00–23:00 之间随机 20 分钟到 2 小时主动私聊号主一条消息，号主一回复就重新计时；内容由模型按人设即时生成（间隔、时段、开关都可配置）
- 按会话保存上下文，聊久了自动生成摘要，避免上下文越滚越大
- `/remember` 长期记忆，`/reset` 清空当前会话上下文
- 回复自动分段，段与段之间随机停 3–5 秒再发，避免连发被判定为机器人；大模型出错时有友好兜底
- 所有用户数据只存在本机 `data/bot.db`，不经过第三方存储；人格语料索引在 `data/knowledge_index/`，可随时重建

## 目录结构

```
app/
  config.py            配置加载（.env）
  logging_setup.py     日志
  main.py              FastAPI 入口，接收 NapCat 反向 WS
  core/
    persona.py         大肥鱼人格与提示词
    llm.py             DeepSeek 接口封装
    search.py          联网搜索（触发判断、结果整理）
    weather.py         天气查询（Open-Meteo，免 Key）
    link.py            链接解析（B 站视频信息、普通网页正文）
    voice.py           语音识别（硅基流动）与语音合成（Edge TTS）
    memory.py          SQLite 存储（消息/摘要/长期记忆）
    text.py            文本清洗、别名识别、分段
    trigger.py         触发判断与限流
    commands.py        斜杠指令
    knowledge.py       人格语料检索（RAG：FAISS 索引，字面兜底）
    embeddings.py      百炼文本向量接口（OpenAI 兼容）
    proactive.py       主动消息定时任务（私聊号主）
    engine.py          消息处理主流程
  onebot/
    events.py          OneBot 11 事件解析
    client.py          反向 WS 连接与发消息
knowledge/
  *.md                人格语料目录（可多个文件，按 ## 小节切块）
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
| `WEB_SEARCH_ENABLED` | true | 是否自动联网搜索（复用 `DEEPSEEK_API_KEY`） |
| `WEB_SEARCH_COUNT` | 3 | 每次最多取几条搜索结果（1-50） |
| `WEB_SEARCH_TIMEOUT` | 15 | 搜索超时秒数，失败只记日志，不影响聊天 |
| `WEATHER_ENABLED` | true | 是否用 Open-Meteo 查天气（免 Key） |
| `WEATHER_TIMEOUT` | 10 | 天气接口超时秒数，失败只记日志 |
| `LINK_PARSE_ENABLED` | true | 是否抓取消息里的 B 站/网页链接内容 |
| `LINK_PARSE_TIMEOUT` | 10 | 单个链接抓取超时秒数，失败不影响聊天 |
| `LINK_PARSE_MAX_LINKS` | 2 | 每条消息最多解析几个链接 |
| `VOICE_ASR_ENABLED` | true | 是否把收到的语音转成文字（需要 `SILICONFLOW_API_KEY`） |
| `SILICONFLOW_API_KEY` | 空 | 硅基流动 API Key，只用于语音识别；不填则听不了语音 |
| `VOICE_ASR_MODEL` | FunAudioLLM/SenseVoiceSmall | 硅基流动的语音识别模型（官方标价免费） |
| `VOICE_ASR_TIMEOUT` | 30 | 语音识别超时秒数，失败只记日志 |
| `VOICE_TTS_ENABLED` | true | 是否允许机器人自己发语音（Edge TTS，无需 Key） |
| `VOICE_TTS_VOICE` | zh-CN-XiaoyiNeural | 语音音色，默认「晓伊·温柔少女」 |
| `VOICE_TTS_TIMEOUT` | 15 | 语音合成超时秒数，失败改发文字 |
| `VOICE_TTS_MAX_CHARS` | 80 | 超过这个字数的回复不合成语音，直接发文字 |
| `RED_PACKET_ENABLED` | true | 收到红包时是否发祝福凑热闹（不涉及钱） |
| `RED_PACKET_DELAY_MIN` | 1 | 红包反应的最短随机延迟（秒） |
| `RED_PACKET_DELAY_MAX` | 5 | 红包反应的最长随机延迟（秒） |
| `RED_PACKET_COOLDOWN` | 30 | 同一会话两次红包反应的最小间隔（秒） |
| `PROACTIVE_ENABLED` | true | 是否定时主动私聊 `ADMIN_QQ` |
| `PROACTIVE_INTERVAL_MINUTES` | 20 | 主动消息最短间隔（分钟） |
| `PROACTIVE_INTERVAL_MAX_MINUTES` | 120 | 主动消息最长间隔（分钟），每次在这个范围内随机 |
| `PROACTIVE_START_HOUR` | 8 | 发送时段开始（整点，含） |
| `PROACTIVE_END_HOUR` | 23 | 发送时段结束（整点，不含） |
| `PROACTIVE_UTC_OFFSET` | 8 | 计算发送时段用的 UTC 偏移小时数（中国为 8） |
| `MAX_REPLY_CHARS` | 60 | 每段回复最大字数 |
| `MAX_REPLY_SEGMENTS` | 3 | 一条回复最多分几段 |
| `SEGMENT_DELAY_MIN` | 3 | 同一会话相邻两条消息的最短随机间隔（秒） |
| `SEGMENT_DELAY_MAX` | 5 | 同一会话相邻两条消息的最长随机间隔（秒） |
| `CONTEXT_MESSAGES` | 20 | 每次带给模型的历史消息条数 |
| `SUMMARY_TRIGGER` | 40 | 未摘要消息达到多少条时生成摘要 |
| `RATE_MIN_INTERVAL` | 0 | 同一会话两次回复的最小间隔（秒） |
| `RATE_PER_MINUTE` | 60 | 同一会话每分钟最多回复次数（安全上限） |
| `GROUP_REPLY_PROBABILITY` | 1 | 群聊接话概率，1 表示每条都接；设为 0 则只回 @ 和叫名字 |
| `GROUP_COOLDOWN` | 0 | 随机接话冷却时间（秒） |
| `KNOWLEDGE_PATH` | knowledge | 语料目录（也可以指向单个 md 文件） |
| `KNOWLEDGE_FILE` | 空 | 旧配置名，含义同 `KNOWLEDGE_PATH`，新部署建议不用 |
| `KNOWLEDGE_INDEX_DIR` | data/knowledge_index | FAISS 索引目录（可整个删除让程序重建） |
| `KNOWLEDGE_TOP_K` | 2 | 每次最多注入几段语料 |
| `DASHSCOPE_API_KEY` | 空 | 百炼 API Key，只用于语料向量检索；不填则退回字面检索 |
| `EMBEDDING_ENABLED` | true | 是否启用向量检索（需要 `DASHSCOPE_API_KEY`） |
| `EMBEDDING_MODEL` | text-embedding-v4 | 百炼向量模型 id |
| `EMBEDDING_DIMENSIONS` | 1024 | 向量维度（换模型/维度会自动重建索引） |
| `EMBEDDING_MIN_SCORE` | 0.40 | 余弦相似度阈值，低于它的语料不注入 |
| `EMBEDDING_TIMEOUT` | 20 | 向量接口超时秒数，失败回退字面检索 |
| `EMBEDDING_BASE_URL` | https://dashscope.aliyuncs.com/compatible-mode/v1 | 百炼 OpenAI 兼容接口地址 |
| `QUOTE_REPLY` | true | 群聊回复是否引用原消息 |
| `DATA_DIR` | data | SQLite 数据目录 |

## 人格语料（RAG）

`knowledge/` 是语料目录，可以放多个 `.md` 文件。每个文件按 `## 小节标题` 切块；单块超过 600 字时会带 100 字重叠继续切，标题和正文一起参与检索。

程序启动时用文件 SHA256 检查语料变化：

- 第一次启动：把全部语料块发给百炼（DashScope）的 `text-embedding-v4` 算向量，建立 FAISS 索引，存到 `data/knowledge_index/`。
- 之后启动：文件没变就直接加载索引，不重复请求接口。
- 改了某个文件：只重算新增/改动的块，删掉已删除的块，其余块复用旧向量。
- 首次建大索引在后台执行，机器人可以先上线；建好后日志会打印段数和耗时。

检索时把问题转成向量，在 FAISS 里取相似度最高的 `KNOWLEDGE_TOP_K` 段（默认 2 段，低于 `EMBEDDING_MIN_SCORE` 的不注入），作为参考放进系统提示词。这叫「检索增强生成」（RAG）：**不微调模型，也不把语料上传去做训练**，只影响这一次提示词的内容。

- 需要 Key：在[百炼控制台](https://bailian.console.aliyun.com/)创建 API Key，填进 `.env` 的 `DASHSCOPE_API_KEY`；没填 Key 时自动使用字面重叠检索，聊天照常。
- 索引文件：`data/knowledge_index/` 下的 `index.faiss`、`chunks.jsonl`、`manifest.json`；怀疑索引坏了可以整个目录删掉，重启后会重建。
- 失败兜底：接口超时、报错、欠费时只记一条警告日志（5 分钟内不重复刷屏），本次回退字面检索，不会让聊天失败；语料超过 2000 段时字面兜底会跳过，避免每条消息全量扫描。
- 想加语料：在 `knowledge/` 下新增 `.md`，按 `## 小节标题` 写，重启程序即可；也可以用 `KNOWLEDGE_PATH` 指向别的目录或单个文件。
- 内存提醒：向量常驻内存，1024 维 float32 下 1 万段约 40 MB、5 万段约 200 MB。1 GiB 服务器在加到几万段前先看 `free -h`，必要时升 2G 或换磁盘型方案。
- 语气描述（温柔、先关心情绪、庭院/小伊卡意象）写在 `app/core/persona.py` 的 `HYACINE_STYLE`，不需要语料也能生效。
- 不想启用检索：把 `KNOWLEDGE_PATH` 指向一个不存在的目录即可，程序不会报错，只是没有语料片段；只想关向量、保留字面检索就设 `EMBEDDING_ENABLED=false`。
- 图片识别会把图片内容发送给 DeepSeek；QQ 原生小表情没有图片文件，程序把表情名（如「/菜汪」）作为文字说明发给模型。介意图片上传的话，可以在 `app/core/engine.py` 的 `_build_llm_content` 里去掉图片下载逻辑。

## 联网搜索

程序把「联网搜索」注册成模型可调用的工具（`web_search`）：模型判断这条消息需要最新或实时信息时，自己调用 DeepSeek 官方 Search API（`POST /v1/tools/search`），程序把最多 `WEB_SEARCH_COUNT` 条网页摘要交回给模型，再让它用自己的话回答。

- 什么时候搜：由模型自己决定。榜单排名、新闻、价格、票务、赛事、新游戏、新版本、附近店铺这类问题会主动联网；天气改走下面的 `get_weather` 工具；常识、观点、情绪安慰和普通闲聊不搜，不会为了省事乱调工具。
- 兜底：如果所用模型或接口不支持工具调用，程序会自动退回「关键词判断」的老办法（命中「搜一下/查一下/最新/新闻/官网/多少钱」或「时效词+疑问词」才搜），聊天不受影响。
- 不用另外申请 Key：搜索接口和大模型共用 `DEEPSEEK_API_KEY`；按成功请求计费，官方价格 5 元/千次，失败、超时和搜索无结果都不扣费。
- 结果只作参考：提示词要求模型概括网页内容、不整段照搬，网页里诱导它改变身份或泄露设定的文字会被忽略。
- 隐私：搜索词会发送给 DeepSeek（和聊天内容一样），不会发给其他搜索厂商。
- 不想用：在 `.env` 里设 `WEB_SEARCH_ENABLED=false` 并重启 `bigfish-bot`；搜索失败只写日志，聊天照常进行。
- 想让兜底更宽/更窄：改 `app/core/search.py` 顶部的 `_SEARCH_WORDS`（直接搜索）和 `_LIVE_WORDS`（要配合疑问词）；模型的自主判断则靠 `app/core/persona.py` 里的 `SEARCH_TOOL_GUIDE`。

## 天气查询

天气问题不交给网页搜索，而是走 Open-Meteo 免费预报接口（不需要 API Key）：

- 模型识别到问天气时调用 `get_weather`，只传城市名；对方说的是区县、湖泊或景点（如“黄家湖”）时，传它所在的市。
- 程序取未来 3 天的天气现象、最高/最低气温和降水概率，整理成【实时天气数据】，并明确标注今天/明天/后天。
- 提示词要求模型照抄这里的日期和数字，不再引用网页旧预报；没查到就如实说，不编温度。
- 失败或超时只记日志，模型仍可退回联网搜索；不想用就在 `.env` 设 `WEATHER_ENABLED=false`。

## 链接解析

群里有人转发 B 站视频或网页链接时，程序会在回复前先抓一份页面文字，放在【链接内容】里交给模型，让 AI 至少知道这个视频/网页在讲什么。

- 支持范围：消息文字里的 `http/https` 链接，以及 QQ 分享卡片（json/xml）里的跳转地址；`b23.tv` 短链会先跟随跳转再解析。
- B 站视频：调 B 站公开接口取标题、UP主、时长、播放/点赞、分区、发布日期、简介、标签、前几条热评和弹幕摘录。接口需要设备 Cookie（程序自动取 `buvid3`，不需要登录、不需要你自己的 Cookie）。
- 普通网页：取标题、`description` 摘要和去掉标签后的正文摘录（正文最多约 500 字）。
- 能力边界：**这只是页面上的文字，不是视频画面，也不是语音转文字**。没有字幕的视频只能靠标题、简介、评论和弹幕推断内容，AI 看不到画面，提示词也要求它别编造视频细节。
- 花费与延迟：不产生额外费用（都是公开接口）；但每条带链接的消息会多等 1～2 秒，网络慢时接近 `LINK_PARSE_TIMEOUT`。每次最多解析 `LINK_PARSE_MAX_LINKS` 个链接。
- 失败兜底：链接打不开、接口返回错误、内网地址都会被跳过，只写一条日志，聊天照常进行；抓不到内容时提示词要求它如实说没看到。
- 不想用：在 `.env` 设 `LINK_PARSE_ENABLED=false` 并重启 `bigfish-bot`。
- 安全：只允许 http/https 公网地址，`localhost`、`127.0.0.1`、`192.168.x.x` 等内网主机直接拒绝，避免群里发内网链接让服务器乱访问。

## 语音

两条独立的链路，可以分别开关：

**听语音（语音转文字）**：别人发语音消息时，程序把语音文件下载下来（NapCat 给的是本地路径或 http 地址），silk 格式用 `pilk` 解码、amr 等格式用系统 `ffmpeg` 转成 wav，再发给硅基流动的 `FunAudioLLM/SenseVoiceSmall` 模型识别。识别结果以 `[语音转文字]：...` 的形式进入对话上下文，模型就能像看文字一样理解语音。

- 需要 Key：在 [siliconflow.cn](https://siliconflow.cn) 注册后创建 API Key，填进 `.env` 的 `SILICONFLOW_API_KEY`。模型官方标价免费，但接口仍要求 Key；不填 Key 时机器人只会看到 `[语音]` 提示，听不到内容。
- 隐私：语音内容会上传给硅基流动做识别，和图片发给 DeepSeek 是同一类行为；介意就把 `VOICE_ASR_ENABLED=false`。
- 失败兜底：下载失败、转码失败、接口报错都只记日志，聊天照常；不会因为一条语音卡住。

**自己说语音（文字转语音）**：

- 模型会在需要时把 `[语音]` 放在回复开头（提示词要求它只在短句、撒娇、安慰、情绪明显时用，解释和清单仍用文字）。程序把标记去掉、把剩下的文字用 Edge TTS（音色 `VOICE_TTS_VOICE`，默认晓伊）合成 mp3，再通过 NapCat 以语音消息发出去。
- 免费、无需 Key：Edge TTS 调的是微软 Edge 浏览器的在线语音接口；不保证 100% 长期稳定，失败时会自动改发文字，不会丢消息。
- 长度限制：回复超过 `VOICE_TTS_MAX_CHARS`（默认 80 字）就不合成语音，直接发文字，避免念一长串。
- 语音临时文件放在 `data/voice/`，启动时自动清理一天前的文件。
- 不想用：`VOICE_TTS_ENABLED=false` 并重启 `bigfish-bot`。

服务器需要额外装两样东西（部署脚本已包含）：

```bash
sudo apt-get install -y ffmpeg
/opt/bigfish/.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple edge-tts pilk
```

## 红包气氛（不碰钱）

群里/私聊有人发红包时，机器人会在 `RED_PACKET_DELAY_MIN`～`RED_PACKET_DELAY_MAX` 秒内随机挑个时间，说一句吉利话（比如「恭喜发财，万事顺意」），只图热闹。

- **不抢钱，也不发钱**：NapCat 没有领红包、发红包的接口，红包里的钱还是留给人抢。机器人说的话里也不声明金额，避免误以为它真抢到了。
- 怎么识别红包：NapCat 遇到红包这类它不认识的消息元素时，会上报一条空消息（日志里写 `[未实现 (ElementType = 9)]`）。程序就把「空消息」当成红包来响应。
- 副作用：其它 NapCat 不认识的消息元素（比如某些卡片）也会被当成红包，说一句祝福，不会有实际损失。
- 防刷屏：同一会话 `RED_PACKET_COOLDOWN` 秒内只回应一次。
- 不想用：`RED_PACKET_ENABLED=false` 并重启 `bigfish-bot`。

## 主动私聊（随机 20 分钟～2 小时一条）

程序在后台跑一个定时任务：默认每天 08:00–23:00，给 `ADMIN_QQ` 发一条私聊消息。每发完一条，就在 `PROACTIVE_INTERVAL_MINUTES`～`PROACTIVE_INTERVAL_MAX_MINUTES`（默认 20～120 分钟）之间随机挑一个时间再发下一条，内容由 DeepSeek 按大肥鱼人格即时生成（关心状态、提醒喝水吃饭休息，或分享一句小心情），不是固定文案。

- 发给谁：只发给 `ADMIN_QQ`，不会发给其他人或群；`ADMIN_QQ` 为空时不发送，只写一条警告日志。
- 什么时候发：每条发完后重新随机计时；如果算出来的时间超过 `PROACTIVE_END_HOUR`，就顺延到第二天 `PROACTIVE_START_HOUR`。
- 号主互动会重置：你在私聊里发消息或戳一戳，会打断当前等待并重新随机计时，避免你正在聊天时它突然插一条主动消息。
- 时区：按 `PROACTIVE_UTC_OFFSET`（默认 8，即北京时间）计算，云服务器系统时区是 UTC 也不影响。
- 断线：NapCat 没连上时跳过当次，不补发；连上后等下一次随机时间。
- 上下文：发出的消息会作为 `assistant` 记录到和 `ADMIN_QQ` 的私聊上下文里，你回复后模型知道它刚才说了什么。
- 花费：每条约一次大模型调用，按默认时段平均一天约 13 次；生成失败只记日志，不会发空消息。
- 风险：主动消息频率过高可能增加 QQ 风控概率，默认已改成随机低频，并且只用备用小号。
- 不想用：`.env` 设 `PROACTIVE_ENABLED=false` 并重启 `bigfish-bot`。

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
>
> 本实例已给 NapCat 加了 `-q 3772806982` 快速登录参数：启动时先用本地缓存的登录凭证自动登录，成功就不用扫码；只有腾讯判定凭证失效（日志出现「你的用户身份已失效」）时才会退回二维码。换号或重新装系统时，用 `sudo NAPCAT_QUICK_UIN=<QQ号> bash deploy/linux/install-1g.sh` 重新生成服务即可。

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
- 数据在 `/opt/bigfish/data/`（`bot.db` 和 `knowledge_index/`）；`knowledge_index/` 可重建，但一起备份更省事。释放实例前先用 `scp` 拉下来备份。

### 当前部署实例（运维速查）

| 项 | 值 |
|---|---|
| 公网 IP | `8.148.83.1` |
| SSH 用户 | `admin`（阿里云 Ubuntu 镜像默认用户，不是 root） |
| 私钥 | `C:\Users\zack\.ssh\bigfish_deploy`（无密码短语，可直接用） |
| 项目目录 | `/opt/bigfish` |
| 数据文件 | `/opt/bigfish/data/`（`bot.db` + `knowledge_index/`） |
| NapCat 目录 | `/home/bigfish/Napcat` |
| 实例到期 | 2026-12-01（到期前续费或释放，否则转按量计费） |

连接服务器：

```powershell
ssh -i "$env:USERPROFILE\.ssh\bigfish_deploy" admin@8.148.83.1
```

常用运维命令（登录后在服务器上执行）：

```bash
sudo systemctl status bigfish-bot napcat     # 两个服务状态
sudo journalctl -u bigfish-bot -f            # bot 实时日志
sudo journalctl -u napcat -f                 # QQ 实时日志
sudo systemctl restart bigfish-bot           # 只重启 bot，不影响 QQ 登录
sudo systemctl restart napcat                # 已配快速登录，正常不用扫码
free -h                                      # 内存占用
```

> 重启 `napcat` 一般会走 `-q` 快速登录，不用扫码；只有腾讯判定登录凭证失效时才需要重新扫码。重启 `bigfish-bot` 完全没有这个代价。初次部署踩过的坑见 [docs/部署踩坑记录.md](docs/部署踩坑记录.md)。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
.\.venv\Scripts\python.exe -m pytest -q
```

## 常见问题

- **启动后没有「NapCat 已连接」**：检查 NapCat 反向 WS 的 URL 是否 `ws://127.0.0.1:8080/onebot/v11/ws`、token 是否一致、本程序是否已在运行。
- **能连上但群里 @ 没反应**：确认 NapCat 登录的是机器人小号，并且消息里是真正的 @（不是手打的「@大肥鱼」文字）。手打名字也能触发是别名功能。
- **回复很慢或报错**：检查 `DEEPSEEK_API_KEY` 是否有效、账户余额是否充足；日志会打印具体原因。
- **它什么时候会联网搜**：命中「搜一下/查一下/最新/新闻/多少钱」等关键词，或「今天/最近 + 疑问词」时才会搜；普通闲聊不搜。天气问题优先走天气工具，不再用网页搜索。搜索可在 `.env` 设 `WEB_SEARCH_ENABLED=false` 关闭，天气可用 `WEATHER_ENABLED=false` 关闭。
- **它能看懂 B 站视频吗**：只能看到标题、简介、标签、热评和弹幕文字，看不到画面；想让它讲视频细节，最好自己补一句「这个视频大概讲了什么」。
- **它能听懂语音吗**：配置 `SILICONFLOW_API_KEY` 后可以；没配 Key 只会提示「[语音]」。语音识别和视频解析是两回事，B 站视频里的说话内容它仍然听不到。
- **它发语音失败怎么办**：Edge TTS 偶尔会被微软接口限制，程序会自动改发文字；日志里会出现「语音合成失败」。
- **为什么还会自动通过好友/入群**：程序不会调用审批接口，只记录日志。请按「好友与群邀请：只由你手动确认」检查小号 QQ 自身的隐私与加群设置。
- **群聊每条都回，会不会太吵**：这是当前配置（`GROUP_REPLY_PROBABILITY=1`）；仍保留每分钟 60 条的安全上限。想安静一点就把概率调低或设为 0。
- **发表情/图片它会怎么回**：交给模型理解后生成回复，不是固定文案；图片会上传 DeepSeek 识别。
- **戳一戳为什么有时不回**：回复由模型生成，调用失败或触发每分钟上限时会跳过；机器人自己触发的事件也不接，避免自循环。
- **想换模型**：改 `LLM_MODEL` 即可，只要是 DeepSeek 的 OpenAI 兼容模型名。
- **数据在哪**：`data/bot.db`，删除前请先备份；`data/knowledge_index/` 是语料向量索引，删掉后重启会自动重建；`/reset` 只清当前会话上下文，`/forget` 只清长期记忆。

## 安全提醒

- `.env` 里有 API Key，不要截图、不要提交到仓库；`.gitignore` 已忽略。
- `ONEBOT_TOKEN` 不要留空，尤其不要把 8080 端口暴露到公网。
- 机器人会接触群里的公开聊天内容，提示词里已限制不泄露系统设定和隐私，但不要把它当保险箱，敏感信息不要发。
