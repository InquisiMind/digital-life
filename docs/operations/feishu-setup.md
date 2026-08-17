# 飞书配置

在 [飞书开放平台](https://open.feishu.cn/app) 创建自建应用，配置四处。

## 1. 权限

应用详情 → 权限管理 → **导入权限配置**，粘贴以下 JSON：

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
    ],
    "user": [
      "aily:file:read",
      "aily:file:write",
      "base:app:copy",
      "base:app:create",
      "base:app:read",
      "base:app:update",
      "base:field:create",
      "base:field:delete",
      "base:field:read",
      "base:field:update",
      "base:record:create",
      "base:record:delete",
      "base:record:retrieve",
      "base:record:update",
      "base:table:create",
      "base:table:read",
      "base:table:update",
      "base:view:read",
      "base:view:write_only",
      "bitable:app",
      "bitable:app:readonly",
      "board:whiteboard:node:create",
      "board:whiteboard:node:delete",
      "board:whiteboard:node:read",
      "board:whiteboard:node:update",
      "cardkit:card:read",
      "cardkit:card:write",
      "cardkit:template:read",
      "contact:contact.base:readonly",
      "contact:user.base:readonly",
      "contact:user.basic_profile:readonly",
      "contact:user.employee_id:readonly",
      "contact:user:search",
      "docs:document.comment:create",
      "docs:document.comment:delete",
      "docs:document.comment:read",
      "docs:document.comment:update",
      "docs:document.comment:write_only",
      "docs:document.content:read",
      "docs:document.media:download",
      "docs:document.media:upload",
      "docs:document:copy",
      "docs:document:export",
      "docs:document:import",
      "docx:document:create",
      "docx:document:readonly",
      "docx:document:write_only",
      "drive:drive.metadata:readonly",
      "drive:file:download",
      "drive:file:upload",
      "im:chat.access_event.bot_p2p_chat:read",
      "im:chat.announcement:read",
      "im:chat.members:read",
      "im:chat.nickname:read",
      "im:chat:read",
      "im:message",
      "im:message.group_msg:get_as_user",
      "im:message.p2p_msg:get_as_user",
      "im:message:readonly",
      "im:message:update",
      "offline_access",
      "sheets:spreadsheet",
      "sheets:spreadsheet.meta:read",
      "sheets:spreadsheet.meta:write_only",
      "sheets:spreadsheet:create",
      "sheets:spreadsheet:read",
      "sheets:spreadsheet:readonly",
      "sheets:spreadsheet:write_only",
      "space:document:move",
      "task:comment:read",
      "task:comment:write",
      "task:task:read",
      "task:tasklist:read",
      "wiki:node:copy",
      "wiki:node:create",
      "wiki:node:move",
      "wiki:node:read",
      "wiki:node:retrieve",
      "wiki:space:read",
      "wiki:space:retrieve",
      "wiki:space:write_only",
      "wiki:wiki:readonly"
    ]
  }
}
```

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
