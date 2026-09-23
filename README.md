# Telegram 广告审核机器人：Telethon + 大语言模型

纯 Telethon / MTProto 实现，使用 **bot 账号**登录，不使用用户账号运行，不调用 HTTP Bot API。广告性质由你配置的 OpenAI compatible Chat Completions 服务判断。所有凭据与运行配置统一放在 `.env`。

## 功能

1. **用户入群检查昵称、bio 和 Premium 状态表情包名称**：读取用户实体，调用 `users.getFullUser` 获取 `about`，交给模型判定。用户名字旁有效的普通自定义 emoji 状态会查询所属表情包的 `title` / `short_name`，与昵称和 bio 一起送审；不是识别表情图案，也不检查聊天消息中的自定义 emoji。已过期状态不查询，收藏礼物状态不在此项范围内。表情包查询失败会重试，不视为审核通过。资料命中广告则踢出。普通直接入群也检查，无需要求用户先私聊 bot。资料查询失败会重试，并先尝试检查可获得的昵称。
2. **新人前三条可审核发言检查**：文字消息先登记再异步调用模型；默认删除新人发送的图片、视频、语音、贴纸、文件等无法完整审核的媒体，带说明文字也删除，不占用三条名额。三条文字消息完成非广告判定后才允许发送媒体。计数和审核状态保存在 SQLite；原三条消息被编辑仍复查。退出后重新加入重新计数，重复入群更新不会重复重置。
   - 发言审核同时提取回复头的引用文字、隐藏链接、来源标签。跨群回复没有引用文字或链接时，尝试读取被回复的单条原消息，不递归追踪回复、不扫描历史。取得的内容以 `reply_context` 与当前正文分开送审和记录日志；仅引用广告进行举报或讨论不应被认定为推广。跨群原消息无权限、已删除或仅有无法审核的媒体时，任务重试，不算审核通过，不因无法读取而处罚。
   - 回复上下文仍沿用原审核范围：新人前三条及其编辑、群外 bot 输出；不因跨群回复而自动开始审核所有老成员。引用原文的语义准确率仍取决于模型。无引用快照时，获取的是查询时的原消息内容，可能已被原作者编辑。
3. **群外 bot 消息清理与追责**：删除群外 bot 输出，支持 Guest Bot 与 Inline Bot。Guest 使用 `guestchat_via_from` 定位召唤者，Inline 使用真实发送者。输出被判为广告后踢人，独立于前三条限制。群内 bot 自身正常发言保留；普通用户通过群内 inline bot 发出的内容仍受前三条审核。

群主、管理员及 `EXEMPT_USER_IDS` 指定的用户不会被踢。匿名/频道身份不能可靠归因到个人，程序不会猜测。单纯 @ 一个用户名不会立即踢人，等待实际广告回复后依据来源字段追责。

## 快速启动（Linux / Python 3.11+）

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，至少填写以下 7 项：

```dotenv
TG_API_ID=12345678
TG_API_HASH=your_api_hash
TG_BOT_TOKEN=123456789:your_bot_token
TG_CHAT_IDS=-1001234567890
LLM_BASE_URL=https://your-provider.example/v1
LLM_API_KEY=your_model_api_key
LLM_MODEL=your_model_id
```

- `TG_API_ID` / `TG_API_HASH`：在 https://my.telegram.org 的 API development tools 申请。申请需要你的 Telegram 账号，机器人运行时使用 bot token。
- `TG_BOT_TOKEN`：在 @BotFather 创建机器人获得。
- `TG_CHAT_IDS`：目标群数字 ID，多个群用英文逗号分隔；不会审核列表外的群。
- `LLM_BASE_URL`：API 根地址，不包含 `/chat/completions`，程序自动追加该路径。模型服务商可能用 `/v1` 或其他路径，按实际填写。
- `LLM_API_KEY` / `LLM_MODEL`：填你服务商的密钥和模型 ID，不会默认替换成某个指定模型。免鉴权本地服务的 key 可填 `local-unused`。

然后执行：

```sh
.venv/bin/python bot.py --check-config
.venv/bin/python bot.py --check-llm
.venv/bin/python bot.py
```

`--check-config` 只校验格式；`--check-llm` 会发送一条固定正常交流样例到模型接口，不连接 Telegram、不删除消息、不踢人。连通性检查不代表广告判断准确率验证。

程序自动加载当前目录的 `.env`；可用 `--env /path/to/.env` 指定位置。已有环境变量优先。Python dotenv 不进行变量插值，密钥中的 `$` 保持原样。

## Telegram 设置

机器人启动后自动初始化并监听配置的群，**无需私聊或在群里发送 `/start`**，也不会向群里发送启动提示或命令回复。已有群资料和权限时立即启用审核；暂缺资料或权限时自动等待、重试，满足条件后自动启用。运行状态只写入进程日志。

1. 将机器人加入目标群，设为管理员，授予 **删除消息、封禁用户** 权限。取得群访问资料后会检查；确认权限前该群任务保持待处理。
2. 在 BotFather 中开启 **Bot-to-Bot Communication Mode**，以便接收其他 bot 的消息。审核机器人不需要开启 Guest Mode。管理员身份配合此设置用于接收群内其他机器人的消息。
3. 只运行同一份数据目录的一个实例。进程锁会阻止重复运行；不要把相同 bot 配成多套独立审核实例。

本版处理的是实际加入后的事件，不自动批准或拒绝待处理的入群申请。通过申请批准加入后，同样检查昵称与 bio；加入事件使用 Raw 同时覆盖服务消息和成员状态变化。

## 首次启动找不到群实体

如果日志出现 `Could not find the input entity for PeerChannel(...)`，通常是新 session 只有群 ID、没有该 bot 账号可用的 `access_hash`，并不直接说明群 ID 错误。保留现有 `data/telegram.session`，删除 session 会丢失已缓存的资料。

新版会保持连接，日志显示等待群访问资料，未就绪群的审核/删除/踢人任务都先保留，不消耗任务重试次数。取得资料并确认权限后，日志显示“审核已启用”。其他已就绪群照常处理。

**私有群或只知道数字 ID：**确认 bot 已在群内且为管理员。程序静默监听正常群消息、成员变化等更新，取得其中的群资料后自动检查权限并启用审核，不需要发送任何命令。首次使用全新 session、仅配置私有群数字 ID 时，可能需要等待携带群实体的自然更新；数字 ID 本身不能代替缺失的 `access_hash`。保留 session 后，后续启动会自动使用缓存。

启动日志会打印实际机器人用户名；收到目标群的首个更新时记录群 ID 和更新类型，不记录消息正文。尚未就绪期间每 60 秒报告连接状态、更新总数和目标群更新数：

- 更新总数为 0：该进程的 Raw 回调尚未收到更新，检查 Telegram 连接及同一 bot 的其他运行实例。
- 更新总数大于 0、目标群为 0：该进程收到过其他更新，但尚未收到目标群更新；检查群 ID、bot 是否在目标群内及实际机器人用户名。
- 目标群更新数大于 0：更新已到达，继续根据群资料或权限警告排查。

这些计数是本次进程启动后的累计值，不代表 Telegram 服务器当前一定没有消息。不要删除 session 来尝试修复，也不要同时启动第二个正式审核实例。

**公开群：**可以额外配置用户名以主动解析群资料，例如：

```dotenv
TG_CHAT_IDS=-1005100328224
TG_CHAT_REFERENCES='{"-1005100328224":"@your_public_group"}'
```

必须替换为实际公开群用户名；程序会核对解析出的数字 ID，用户名指向其他群时不会启用审核。私有邀请链接不用于这个配置。只有已有缓存缺失时才需要用户名解析。

本程序不会用 `get_dialogs()` 刷新群列表：该接口仅允许用户账号，bot 账号不能调用。等待资料期间只保持监听，不会把尚未取得权限的群标记为可审核。

## Docker

先填写 `.env`，然后：

```sh
docker compose up -d --build
docker compose logs -f --tail=100
```

`.env` 作为只读文件挂载，凭据不会复制到镜像里。数据持久化在 `./data`。修改 `.env` 后执行 `docker compose restart`。

容器里的 `127.0.0.1` 指容器自身；模型服务或代理如果运行在宿主机，需要改成容器能够访问的地址。当前环境没有 Docker，因此此次交付未实际构建镜像。

## 模型协议与广告定义

请求：`POST {LLM_BASE_URL}/chat/completions`，使用 Bearer API key，发送 `model`、`messages`、`stream=false`。默认附带 `response_format={"type":"json_object"}`；服务商不支持时设置 `LLM_JSON_MODE=false`，仍会要求输出 JSON，并在本地验证。

模型输出格式：

```json
{
  "is_ad": true,
  "confidence": 0.97,
  "reason": "招揽业务并引导用户私聊",
  "evidence": "联系我购买"
}
```

只有同时满足以下条件才踢人：`is_ad` 是布尔值 true、`confidence` 达到 `AD_CONFIDENCE_THRESHOLD`、`evidence` 是待审内容中确实存在的非空原文。`confidence` 是模型自评，不是经过校准的真实准确率。

审核系统提示词将普通讨论、新闻引用、举报广告、个人职业描述与真实推广区分；待审内容单独放在 user JSON 数据里，不拼进系统指令。模型不接收工具权限，不能自行发起群操作。这些措施减少提示注入和格式误判，但不能保证语义判断完全正确。

- `GROUP_POLICY`：可编辑本群广告规则；例如是否允许成员自荐开源项目。
- `LLM_EXTRA_BODY`：额外参数 JSON，例如 `{"temperature":0,"max_tokens":512}`。默认 `{}`，避免给不同模型强加不支持的参数。受保护字段如 messages/model/tools 不能被覆盖。
- `LLM_TIMEOUT_SECONDS`、`LLM_CONCURRENCY`：请求超时和并发数。

## 踢出、封禁与失败处理

- `KICK_MODE=kick`：移出后解除封禁，允许以后重新加入；`ban` 为永久封禁，仅超级群支持。
- `DRY_RUN=true`：仍请求模型并消耗对应服务额度，但只记录拟执行动作。计数和任务状态仍推进，切换正式运行不会自动重新执行已完成的 dry-run 任务。
- `CHECK_UNSEEN_MEMBERS=false`：默认只计数观察到加入事件的新成员；设 true 对首次观察到的旧成员也检查前三条。
- `RESTRICT_NEWCOMER_MEDIA=true`：默认开启新人媒体限制；删除而非踢人，不调用模型识别被限制媒体，也不修改成员的长期发送权限。管理员/豁免用户不受此项删除限制。设为 false 可恢复媒体也占用前三条名额的旧行为。
- 媒体限制在三条文字审核完成前持续生效：模型故障或仍在排队不算通过。低置信度广告结论不踢人，但也不算通过，可继续发送其他文字完成审核。普通文字链接及其自动预览允许；投票、位置、联系人和富消息等尚未完整支持的非普通文本内容在新人阶段也删除。
- 模型超时、限流、格式错误、证据捏造或 Telegram 查询失败都不会直接触发踢人，也不会标成“审核干净”。任务按指数退避重试。
- 删消息和踢人使用独立工作协程，避免等待模型请求。Guest 外部来源明确时先排队删除，再判断召唤者是否应被踢；普通 bot 或 inline 来源需要先确认群成员身份，查询失败会重试。
- 默认最多尝试 8 次；耗尽后保留为 failed，控制台记录任务 ID 和错误类型。修复服务或权限后，停止旧实例再运行：

```sh
.venv/bin/python bot.py --retry-failed
```

- 踢人的 ban/unban 分步保存进度，unban 失败后重试会从 unban 恢复。Telegram 操作与本地数据库无法组成原子事务；恰好在远端执行成功、本地保存前断电仍可能产生不确定状态，需要管理员根据日志检查。
- 审核任务带入群轮次和消息版本。用户重新入群或消息被新版替代后，旧模型结果不再触发新的踢人任务。已执行的删除/踢人无法被之后的编辑撤回。

## 数据与覆盖范围

- 待审文本会发送给你在 `LLM_BASE_URL` 指定的模型服务；只发送待审资料/文本与群规则，不发送 Telegram token/API hash。
- 模型审核覆盖可提取的文本。不下载图片、不做 OCR、视频识别、语音转写或二维码识别；默认通过新人媒体删除策略处理这一阶段的无法审核内容。通过新人验证后不再限制普通成员的媒体，群外 bot 清理仍持续生效。
- 新人媒体不占名额，相册逐条删除；带正常说明文字的图片/视频同样删除，防止用说明文字绕过。原本计入审核的文字若被编辑成媒体，删除该消息并撤销其通过状态，允许补发文字完成验证。重复更新不重复计数。
- 升级时自动增加审核状态列，并沿用旧版已有的消息计数，不追溯重审旧记录。新加入或重新加入的用户完整适用新规则。
- `data/telegram.session` 存放 MTProto 会话及实体访问资料；`data/moderation.sqlite3` 保存成员轮次、前三条 ID、任务和日志。待审/失败任务保留文本以便重试；完成任务清空 payload，保留任务元数据和去重指纹。控制台 `review_input` 记录实际送审内容，`review_result` 记录判定、置信度、理由和原文证据，并用任务 ID / 版本关联；JSON 转义换行以避免伪造日志行。audit 保存判定、理由及证据，完成任务的原始输入仍只保留在进程日志中。日志可能包含昵称、bio、状态表情包名称和消息正文，应妥善保管。数据需要按你的保留周期维护。
- 新安装不能补查部署前的全部聊天历史，长时间离线也可能错过历史更新。不会持续扫描用户之后修改的简介。
- Telegram 的权限、可访问资料、消息时效仍限制实际执行；无法查询资料的任务会明确失败。不会凭空获得不可访问用户的 bio。
- 此版本使用新的 `moderation.sqlite3`，不会迁移旧 Bot API 示例的计数；替换旧版时应停止旧进程，仅运行新版。

## 测试与当前验证状态

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试涵盖模型 HTTP 格式、JSON 校验、伪造证据、限流/错误、实际本地 HTTP 接口调用、新人计数和重启恢复、编辑复查、Raw 入群边界、Guest/Inline 追责、管理员保护、动作恢复等。

此次没有真实 Telegram/模型服务凭据，未连接真实群或付费模型服务。测试中的 Telegram 为模拟网关；HTTP 连通测试使用本机模拟兼容服务，不能代替你所选模型的识别准确率评测或真实群验收。

上线时至少用测试群验证：bio 广告入群、第三条广告、前三条编辑成广告、Guest Bot 输出与召唤者、群内 bot 保留、模型不可用时的待审重试。

## 官方依据

- Telethon bot 登录：https://docs.telethon.dev/en/stable/basic/signing-in.html
- bot 可用的完整资料查询：https://core.telegram.org/method/users.getFullUser
- Guest 来源字段：https://core.telegram.org/constructor/message
- Bot-to-Bot 接收设置：https://core.telegram.org/bots/features#bot-to-bot-communication
- OpenAI Chat Completions：https://developers.openai.com/api/reference/resources/chat
