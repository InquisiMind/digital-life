# 飞书配置

在 [飞书开放平台](https://open.feishu.cn/app) 创建自建应用，配置四处。

配置分两部分，按需选择：

- **基础配置（必做）**：tenant 权限 + 事件订阅 + 机器人 + 发布。Bot 身份收发消息、群聊协作用这套。
- **全接管配置（可选）**：user 权限（见 1b）+ 控制台授权。数字生命以**你的身份**拉取全部群聊/私聊消息（社交感知）。不开全接管则完全不需要。

## 1. 权限 — 基础（tenant，必做）

应用详情 → 权限管理 → **导入权限配置**，粘贴以下 JSON（只含 tenant 权限）：

```json
{
  "scopes": {
    "tenant": [
      "aily:file:read",
      "aily:file:write",
      "aily:knowledge:read",
      "application:application.app_message_stats.overview:readonly",
      "application:application:self_manage",
      "application:bot.menu:write",
      "bitable:app",
      "bitable:app:readonly",
      "cardkit:card:read",
      "cardkit:card:write",
      "contact:contact.base:readonly",
      "contact:user.base:readonly",
      "contact:user.employee_id:readonly",
      "corehr:file:download",
      "docs:document.comment:create",
      "docs:document.comment:delete",
      "docs:document.comment:read",
      "docs:document.comment:update",
      "docs:document.comment:write_only",
      "docx:document",
      "docx:document.block:convert",
      "docx:document:create",
      "docx:document:readonly",
      "docx:document:write_only",
      "drive:drive.metadata:readonly",
      "drive:drive:version",
      "drive:drive:version:readonly",
      "event:ip_list",
      "im:chat",
      "im:chat.access_event.bot_p2p_chat:read",
      "im:chat.members:bot_access",
      "im:chat:create",
      "im:chat:read",
      "im:chat:readonly",
      "im:chat:update",
      "im:message",
      "im:message.group_at_msg.include_bot:readonly",
      "im:message.group_at_msg:readonly",
      "im:message.group_msg",
      "im:message.p2p_msg:readonly",
      "im:message.pins:read",
      "im:message.pins:write_only",
      "im:message.reactions:read",
      "im:message.reactions:write_only",
      "im:message:readonly",
      "im:message:send_as_bot",
      "im:message:send_multi_users",
      "im:message:send_sys_msg",
      "im:message:update",
      "im:resource",
      "speech_to_text:speech",
      "wiki:wiki"
    ]
  }
}
```

> 只需要基础 Bot 功能的话，导入这一份就够了（tenant 权限多数免审批或自动通过）。
> 去掉了 `task:task:write` 和 `task:tasklist:write`（写飞书任务需要审批，数字生命有自己的 todo 系统不需要）。
> `im:message.reactions:write_only` 是表情收条（可选）。

## 1b. 权限 — 全接管（user，可选）

只有开启「飞书全接管」（控制台 → 实例 Config → 社交接管）才需要。user 权限以**你的身份**生效，多数需要企业管理员审批。按你要用的功能分两档申请：

### 档位 A：社交感知（只要拉消息）

全接管拉群聊/私聊消息所需的最小集，单独导入：

```json
{
  "scopes": {
    "user": [
      "im:chat:read",
      "im:message",
      "im:message.group_msg:get_as_user",
      "im:message.p2p_msg:get_as_user",
      "contact:user.base:readonly",
      "offline_access"
    ]
  }
}
```

### 档位 B：`feishu_call` 通用 API 代理（要数字生命替你操作飞书文档/表格/多维表/知识库/任务）

`feishu_call` 工具以你的 user_access_token 调任意飞书 open-apis（读 + 两步确认的写）。它的能力边界 = 你申请的 user 权限边界。要哪些能力就加哪些权限，以下按域分组（`task:task:write` / `task:tasklist:write` 已去掉——写飞书任务需额外审批，且数字生命有自己的 todo 系统）：

```json
{
  "scopes": {
    "user": [
      "im:chat:read", "im:chat.members:read", "im:chat.nickname:read",
      "im:message", "im:message:readonly",
      "docx:document:create", "docx:document:readonly", "docx:document:write_only",
      "docs:document.content:read", "docs:document.comment:read", "docs:document.comment:create",
      "docs:document.media:download", "docs:document.media:upload",
      "drive:drive.metadata:readonly", "drive:file:download", "drive:file:upload",
      "sheets:spreadsheet", "sheets:spreadsheet.meta:read", "sheets:spreadsheet:read", "sheets:spreadsheet:write_only",
      "wiki:wiki:readonly", "wiki:space:read", "wiki:space:retrieve", "wiki:node:read", "wiki:node:retrieve", "wiki:node:create",
      "base:app:read", "base:app:update", "base:record:retrieve", "base:record:create", "base:record:update",
      "bitable:app", "bitable:app:readonly",
      "task:task:read", "task:tasklist:read", "task:comment:read", "task:comment:write",
      "contact:user:search", "contact:user.basic_profile:readonly"
    ]
  }
}
```

> 只用部分域就删掉其他行（比如只碰飞书表格 → 只留 sheets 组）。权限越多审批越慢，按需裁剪。

### 导入说明

- 导入是**合并**语义：会把新 JSON 的 user 权限加进现有配置，不影响已导入的 tenant 权限。
- 档位 A 是档位 B 的前置（拉消息是最基础的能力），两个都要就 A、B 合并后一次导入。
- 改完权限需要**重新发布应用版本**并等管理员审批，user 权限才生效。

> 去掉了 `task:task:write` 和 `task:tasklist:write`（写飞书任务需要审批，数字生命有自己的 todo 系统不需要）。
> `im:message.reactions:write_only` 是表情收条（可选）。

## 2. 事件订阅

应用详情 → 事件与回调 → 选「长连接」（不需公网回调地址）→ 开启 `im.message.receive_v1`。

## 3. 机器人

应用详情 → 应用功能 → 启用机器人。

## 4. 发布 + 加群

创建版本并发布。

在飞书群聊 → 右上角设置 → **群机器人** → 添加机器人 → 选择你刚发布的自建应用 bot。不是通过联系人拉人，是通过群设置里的"群机器人"入口添加。

## 填入实例

实例 Config → 飞书 → 填 App ID + App Secret → 重启。

## 关于 @ 与跨实例消息

**同一服务下的多个实例**（都跑在同一个 gateway 上）：群消息不 @ 也能互相收到，因为内部做了消息广播适配。

**不同服务的实例**（比如你朋友的独立部署）：飞书服务端会过滤掉非 @ 的机器人消息，别人家的 bot 发的消息你 @ 不到就收不到。这是飞书平台限制，无法突破。所以跨服务协作时必须 @。

## 全接管白名单 / 黑名单（控制拉取范围）

全接管授权后，数字生命默认以你的身份拉取**全部**群聊消息。如需控制范围（隐私边界），在实例 `app.yaml` 配置 `social.takeover` 段：

```yaml
social:
  takeover:
    mode: allowlist          # all（默认，全拉）| allowlist（只拉白名单）| blocklist（排除黑名单）
    allowlist:               # mode=allowlist 时生效；群名或 chat_id（oc_ 开头）均可
      - 数字生命讨论群
      - oc_52d66b75817fcc616a360a64ba9971f7
    blocklist:               # mode=blocklist 时生效；如排除家人群
      - 家庭群
```

- 匹配规则：条目与 chat_id 完全相等，或与群名完全相等（大小写不敏感）
- `mode: allowlist` 但 `allowlist` 为空 → 视为配置错误，不过滤（拉全部），日志有警告
- 也可以在控制台 → 实例 Config → 「社交接管范围」区图形化配置，效果相同
- 改动后下一轮拉取自动生效（轮询间隔约 30 分钟），无需重启

群名查 chat_id 的方法：飞书群设置里复制群号，或看网关日志 `social_takeover: chats refreshed` 后 `GET /im/v1/chats` 返回值。
