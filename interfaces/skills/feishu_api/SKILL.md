---
name: feishu_api
description: 飞书开放平台 API 速查。feishu_call 工具的路径/参数/错误码参考——sheets/docx/wiki/bitable 增删改查。
version: 1.0.0
platforms: []
---

# 飞书 API 速查 (feishu_call 配套)

> `feishu_call` 工具替你签名 token,你只需组装 `method + path + params + body`。
> 所有 path 不含域名(工具自动拼 `https://open.feishu.cn/open-apis`)。
> 写操作(POST/PUT/PATCH/DELETE)默认只 preview,`confirm=true` 才真发。

## 通用约定

- 返回飞书**原始 JSON**:`{code, msg, data}`。`code=0` 才成功,非 0 看下面错误码表。
- token 是**用户身份**(继承你的权限),不是 bot。不需要把 bot 加进知识库成员。

## 错误码速查

| code | 含义 | 怎么办 |
|---|---|---|
| 0 | 成功 | — |
| 99991679 | 权限不足(scope 不够) | 看返回里 `required privileges`,对应 scope 没授权。读不了=缺 readonly,写不了=缺写权限 |
| 99991672 | 无权限访问该资源 | 目标文档你没权限,或 token 过期需重新授权 |
| 90202 | range 格式错 | range 必须是完整范围如 `!A1:Z1`,不能单格 `!A1` |
| 1254040 | token 不存在 | path 里的 token 写错了,或文档已删 |

## wiki 链接解析(关键前置)

`/wiki/{node_id}` 是"外壳",要先解析拿到真实对象 token:

```
GET /wiki/v2/spaces/get_node?token={node_id}
→ data.node.obj_token  (真实文档 token)
→ data.node.obj_type   (docx / sheet / bitable)
```

拿到 obj_token 后,再按类型调对应 API。

## Sheets 电子表格

**列 sheet 列表**(注意 v3 的 meta 接口不返 sheets,要单独查):
```
GET /sheets/v3/spreadsheets/{token}/sheets/query
→ data.sheets[].{sheet_id, title}
```

**读单元格**:
```
GET /sheets/v2/spreadsheets/{token}/values/{sheetId}!A1:Z50
params: {valueRenderOption: "ToString"}
→ data.valueRange.values  (二维数组)
```

**追加行**(append 自动跳到表末尾,range 只声明列范围):
```
POST /sheets/v2/spreadsheets/{token}/values_append
params: {insertDataOption: "OVERWRITE"}
body: {valueRange: {range: "{sheetId}!A1:Z1", values: [["a","b"],["c","d"]]}}
→ data.updatedRange  (实际写入位置, 如 2K2N4p!A61:B62)
```

**改单元格**(PUT 覆盖指定范围):
```
PUT /sheets/v2/spreadsheets/{token}/values
body: {valueRange: {range: "{sheetId}!A61:C61", values: [["","",""]]}}
```

## Docx 文档

```
GET /docx/v1/documents/{doc_token}              → data.document.title
GET /docx/v1/documents/{doc_token}/raw_content  → data.content (纯文本)
GET /docx/v1/documents/{doc_token}/blocks       → data.items (结构化 blocks)
```

## Bitable 多维表格

```
GET /bitable/v1/apps/{app_token}/tables                    → 表列表
GET /bitable/v1/apps/{app_token}/tables/{table_id}/fields  → 字段定义
GET /bitable/v1/apps/{app_token}/tables/{table_id}/records → 记录
POST /bitable/v1/apps/{app_token}/tables/{table_id}/records → 新增记录
```

## 典型工作流:往 wiki 表格追加数据

1. 从 URL 提取 node_id
2. `GET /wiki/v2/spaces/get_node?token={node_id}` → 拿 obj_token
3. `GET /sheets/v3/spreadsheets/{obj_token}/sheets/query` → 选 sheet_id
4. `POST /sheets/v2/spreadsheets/{obj_token}/values_append` (confirm=true) → 追加
