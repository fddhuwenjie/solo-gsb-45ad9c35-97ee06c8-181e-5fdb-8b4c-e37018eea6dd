# Offline JSON Merge API

基于 **FastAPI + Pydantic** 的离线 JSON 服务，提供四组能力：

- **并发合并**：两个副本从同一初始文档离线编辑、各自产生操作日志，之后通过
  `POST /merge` 一次性合并；`POST /apply` 用于单副本在本地重放同一份日志。
  合并语义为 CRDT 风格：向量时钟判定因果，并发写确定性裁决，删除保留墓碑，
  数组插入按操作 ID 稳定排序。两个接口都支持可选的 `exclude` 列表，在合并前
  舍弃指定操作（及其连带操作），并返回可直接交给 `POST /apply` 重放的裁剪日志。
- **快照变更订阅**：`POST /route-changes` 比较两个 JSON 快照，递归得到最小
  变化路径上的 add/remove/replace 事件，并按每位订阅者的 JSON Pointer
  通配模式（`*` 一段、`**` 零到多段）离线分发；服务全程不发起外部回调。
- **受限 JSONPath 查询**：`POST /query` 对同一份文档批量执行 1～20 条只读
  查询，支持点号/括号成员、数组下标、通配、递归下降与数组过滤，结果按文档
  先序返回 `{path, value}`；不执行脚本、不修改文档。
- **跨文档引用物化**：`POST /resolve` 接收 1～50 份带唯一 `document_id` 的
  文档和 `root` 引用，把 `document_id#/JSON/Pointer` 形式的 `$ref` 递归
  物化为单个 JSON；支持覆盖层合并、按完整引用链检测循环，并返回依赖顺序与
  叶子来源映射；输入文档不会被修改。
- **分批提交会话**：`POST /sessions` 创建会话，固定共同的初始文档与初始
  版本向量并返回修订号 0，创建时可附带**最多 20 条互不重复的受保护 JSON
  Pointer 路径**（`protected_paths`），规则随会话持久化、创建后不可更改；
  `POST /sessions/{id}/batches` 凭期望修订号提交非空操作批次，校验、重算与
  修订提交原子完成（多进程共享存储目录时同样成立）；
  `POST /sessions/{id}/replace` 用一条沿用 `/query` 语法的 JSONPath 一次
  替换多个已存在位置的值（最多 20 个命中，同一临界区内选词、生成操作、检查
  修订并落盘）；批次与替换提交都会在持锁提交中比较受保护路径操作前后的值，
  任一路径变化（含缺失与 `null` 互变、布尔与数字互变、祖先整体替换、数组
  插入/删除导致下标位移）整次返回 `422`，不写日志、修订与变更历史；
  `GET /sessions/{id}` 读取最新修订号、文档、版本向量、冲突、墓碑与受保护
  路径；`POST /sessions/{id}/changes` 传入已读修订号和 1～100 条订阅，按
  修订顺序增量补读之后最多 20 次已提交修订各自的路由事件，返回下次读取
  游标。每次提交都从初始文档重算累计日志（不把上次结果当作新基线），每次
  提交的文档快照（历史边界）与修订号一同持久化，会话持久化到磁盘卷，重启后
  仍可读取、仍可接续分页。

## 快速启动

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互式文档见 http://localhost:8000/docs
```

本地开发（无需 Docker）：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
.venv/bin/pytest tests/ -q     # 运行测试
```

## 操作模型

每条操作：

| 字段      | 说明                                                                 |
| --------- | -------------------------------------------------------------------- |
| `actor`   | 副本（作者）唯一标识                                                 |
| `seq`     | 该 actor 的递增序号，必须从初始版本之后连续编号                      |
| `version` | 发出操作时的因果版本向量，必须满足 `version[actor] == seq`           |
| `path`    | JSON Pointer（RFC 6901），`""` 表示文档根；`insert` 时指向目标数组   |
| `action`  | `set` / `delete` / `insert`                                          |
| `value`   | `set`/`insert` 的载荷（`delete` 忽略）                               |
| `index`   | `insert` 在数组中的目标位置（缺省为追加到末尾）                      |

## 合并语义

- **因果校验**：每个 actor 的序号必须连续（紧接初始版本）；操作必须在自己的
  版本向量中携带自身位置；所有因果引用（`version` 中声明的他人序号）必须能由
  初始版本或提交的日志满足；同一 actor 的版本向量不得回退。违反任一规则返回 `422`。
- **重复操作**：`(actor, seq)` 相同且内容一致的重复操作幂等去重（计入
  `duplicates`）；同 ID 不同内容直接 `422`。
- **因果排序**：按向量时钟做拓扑排序后应用；并发操作按 `(actor, seq)` 升序应用，
  因此同一路径上的并发 `set`/`delete` 由最大的 `(actor, seq)` 确定性胜出。
- **冲突明细**：仅报告“因果上最大”的并发写冲突（被后续操作覆盖的历史冲突不算）；
  效果相同的并发写自动收敛，不报冲突。
- **墓碑**：`delete` 生效后在合并状态中保留墓碑并随响应返回；之后的 `set`
  可复活该路径并清除墓碑。
- **数组插入**：并发的同位置 `insert` 不会互相覆盖，按操作 ID `(actor, seq)`
  稳定排序依次插入。
- **合并前裁剪**：请求携带可选的 `exclude`（字段也可写作
  `exclude_operations`）操作 ID 列表时，先按上面的完整规则校验**全部**日志
  （被排除的操作无效仍按原规则返回 `422`），再执行裁剪：指定操作、依赖它的
  跨 actor 后续操作（含传递依赖）以及同 actor 更高序号一并剔除；保留操作的
  版本向量**原样保留、不改写**。裁剪后用保留操作重新计算文档、版本、墓碑、
  冲突与数组插入次序；不携带 `exclude`（或为空列表）时响应与原来完全一致。

## 合并前裁剪（exclude）

`POST /merge` 与 `POST /apply` 的请求体都可携带 `exclude` 列表（每项为
`{"actor", "seq"}`，字段也可写作 `exclude_operations`）：

- 裁剪**之前**先按原规则校验完整日志、去重重复操作；`exclude` 中的每个 ID
  必须存在于提交日志中（只存在于初始 `version` 不算），否则 `422`。
- 连带剔除规则：① 与被剔除操作同 actor 且 `seq` 更大的操作（该 actor 只
  保留连续前缀）；② 版本向量中见到被剔除操作的**跨 actor** 操作，且该规则
  传递生效——依赖了连带剔除操作的操作也被连带剔除。与被排除操作**并发且不
  依赖**它的编辑原样保留。
- 携带 `exclude` 时响应额外包含两个字段：
  - `replay_log`：按因果顺序排列的保留操作（操作自身的 `version` 不被
    改写），可从**同一初始文档**交给原 `POST /apply` 重放；重放所得
    `document` 与 `version` 与裁剪响应一致。
  - `excluded`：按 `(actor, seq)` 排序的剔除清单，`mode` 区分
    `"explicit"`（主动排除）与 `"cascade"`（连带剔除），`reason` 对主动
    排除为 `"explicitly excluded"`，对连带剔除给出触发它的 `actor:seq`。
- 全部操作都被剔除时，文档与版本回到初始值，`replay_log` 为空。

### 跨副本依赖：舍弃一次编辑会带走依赖它的操作

`bob:1` 的版本向量声明见过 `alice:1`；排除 `alice:1` 后，`bob:1` 因跨
actor 依赖被连带剔除，文档回到初始状态：

```bash
curl -s -X POST localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "document": {"title": "draft"},
    "replica_a": [
      {"actor":"alice","seq":1,"version":{"alice":1},"path":"/title","action":"set","value":"alice-title"}
    ],
    "replica_b": [
      {"actor":"bob","seq":1,"version":{"alice":1,"bob":1},"path":"/title","action":"set","value":"bob-title"}
    ],
    "exclude": [{"actor": "alice", "seq": 1}]
  }'
```

响应（`bob` 依赖被排除的 `alice:1`，连带剔除；`excluded` 按 `(actor, seq)`
排序并区分两种剔除原因）：

```json
{
  "document": {"title": "draft"},
  "version": {},
  "conflicts": [],
  "tombstones": [],
  "applied": 0,
  "duplicates": 0,
  "replay_log": [],
  "excluded": [
    {"actor": "alice", "seq": 1, "mode": "explicit", "reason": "explicitly excluded"},
    {"actor": "bob", "seq": 1, "mode": "cascade", "reason": "alice:1"}
  ]
}
```

### 独立并发编辑被保留，同 actor 后续操作连带剔除

`alice:2` 随 `alice:1` 一并剔除（同 actor 更高序号）；`bob:1` 与
`alice:1` 并发、不依赖它，因此保留——数组插入次序也只用保留操作重新计算：

```bash
curl -s -X POST localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "document": {"items": ["x"]},
    "replica_a": [
      {"actor":"alice","seq":1,"version":{"alice":1},"path":"/items","action":"insert","value":"A","index":0},
      {"actor":"alice","seq":2,"version":{"alice":2},"path":"/items","action":"insert","value":"A2","index":0}
    ],
    "replica_b": [
      {"actor":"bob","seq":1,"version":{"bob":1},"path":"/items","action":"insert","value":"B","index":0}
    ],
    "exclude": [{"actor": "alice", "seq": 1}]
  }'
# => {"document": {"items": ["B", "x"]}, "version": {"bob": 1},
#     "conflicts": [], "tombstones": [], "applied": 1, "duplicates": 0,
#     "replay_log": [{"actor":"bob","seq":1,"version":{"bob":1},
#                     "path":"/items","action":"insert","value":"B","index":0}],
#     "excluded": [
#       {"actor":"alice","seq":1,"mode":"explicit","reason":"explicitly excluded"},
#       {"actor":"alice","seq":2,"mode":"cascade","reason":"alice:1"}]}
```

`replay_log` 可以直接从同一初始文档交给 `POST /apply` 重放，得到的
`document`、`version` 与上面的裁剪结果完全相同：

```bash
curl -s -X POST localhost:8000/apply -H 'Content-Type: application/json' -d '{
  "document": {"items": ["x"]},
  "operations": [
    {"actor":"bob","seq":1,"version":{"bob":1},"path":"/items","action":"insert","value":"B","index":0}
  ]
}'
# => {"document": {"items": ["B", "x"]}, "version": {"bob": 1}, ...}
```

`POST /apply` 自身也接受 `exclude`（例如下例把唯一的操作排除后回到初始
文档）；全部剔除时文档与版本回到初始值：

```bash
curl -s -X POST localhost:8000/apply -H 'Content-Type: application/json' -d '{
  "document": {"count": 0},
  "operations": [{"actor":"carol","seq":1,"version":{"carol":1},"path":"/count","action":"set","value":5}],
  "exclude": [{"actor": "carol", "seq": 1}]
}'
# => {"document": {"count": 0}, "version": {}, "conflicts": [], "tombstones": [],
#     "applied": 0, "duplicates": 0, "replay_log": [],
#     "excluded": [{"actor":"carol","seq":1,"mode":"explicit","reason":"explicitly excluded"}]}
```

排除项不存在、或被排除的操作本身违反校验规则时，仍然按原规则返回 `422`：

```bash
curl -s -X POST localhost:8000/merge -H 'Content-Type: application/json' -d '{
  "document": {},
  "replica_a": [{"actor":"alice","seq":1,"version":{"alice":1},"path":"/x","action":"set","value":1}],
  "exclude": [{"actor": "bob", "seq": 1}]
}'
# => {"detail": "excluded operation bob:1 is not present in the logs"}
```

## 调用示例

### POST /merge —— 合并两个副本的日志

```bash
curl -s -X POST localhost:8000/merge \
  -H 'Content-Type: application/json' \
  -d '{
    "document": {"title": "draft", "items": ["x"]},
    "version": {},
    "replica_a": [
      {"actor":"alice","seq":1,"version":{"alice":1},"path":"/title","action":"set","value":"alice-title"},
      {"actor":"alice","seq":2,"version":{"alice":2},"path":"/items","action":"insert","value":"A","index":0}
    ],
    "replica_b": [
      {"actor":"bob","seq":1,"version":{"bob":1},"path":"/title","action":"set","value":"bob-title"},
      {"actor":"bob","seq":2,"version":{"bob":2},"path":"/items","action":"insert","value":"B","index":0}
    ]
  }'
```

响应（并发写 `/title` 由 `("bob",1)` 裁决胜出；两个并发 insert 按操作 ID 稳定排序）：

```json
{
  "document": {"title": "bob-title", "items": ["A", "B", "x"]},
  "version": {"alice": 2, "bob": 2},
  "conflicts": [
    {
      "path": "/title",
      "reason": "concurrent writes; deterministic arbitration by (actor, seq)",
      "winner": {"actor": "bob", "seq": 1, "action": "set", "value": "bob-title"},
      "losers": [{"actor": "alice", "seq": 1, "action": "set", "value": "alice-title"}]
    }
  ],
  "tombstones": [],
  "applied": 4,
  "duplicates": 0
}
```

### 删除与墓碑

`zoe` 并发删除 `/title`，`("zoe",1)` 在裁决中胜过 `alice` 的 `set`：

```bash
curl -s -X POST localhost:8000/merge -H 'Content-Type: application/json' -d '{
  "document": {"title": "draft", "keep": 1},
  "version": {},
  "replica_a": [{"actor":"alice","seq":1,"version":{"alice":1},"path":"/title","action":"set","value":"edited"}],
  "replica_b": [{"actor":"zoe","seq":1,"version":{"zoe":1},"path":"/title","action":"delete"}]
}'
# => {"document": {"keep": 1}, "tombstones": ["/title"], "conflicts": [{"path": "/title", ...}], ...}
```

### POST /apply —— 单副本重放日志

```bash
curl -s -X POST localhost:8000/apply \
  -H 'Content-Type: application/json' \
  -d '{
    "document": {"count": 0},
    "version": {},
    "operations": [
      {"actor":"carol","seq":1,"version":{"carol":1},"path":"/count","action":"set","value":5}
    ]
  }'
# => {"document": {"count": 5}, "version": {"carol": 1}, "conflicts": [], "tombstones": [], "applied": 1, "duplicates": 0}
```

### 校验失败示例（422）

```bash
# bob 声称见过 alice:5，但日志与初始版本中都不存在 -> 因果引用缺失
curl -s -X POST localhost:8000/merge -H 'Content-Type: application/json' -d '{
  "document": {},
  "version": {},
  "replica_a": [],
  "replica_b": [{"actor":"bob","seq":1,"version":{"alice":5,"bob":1},"path":"/x","action":"set","value":1}]
}'
# => {"detail": "operation bob:1 depends on alice:5 which is not present in the logs or the initial version"}
```

### POST /route-changes —— 快照差异比较与订阅分发

请求包含 `before`、`after` 两个 JSON 快照和 1～100 条订阅，每条订阅含唯一
`subscriber_id`、`include`、`exclude`。比较规则：

- **对象递归**到最小变化路径；新增/删除的整棵子树各产生一条 `add`/`remove`
  事件（带整棵子树的 `new`/`old` 值），类型变化或标量变化产生一条 `replace`
  事件。
- **数组作为整体比较**：不同即一条 `replace`，不会逐下标展开。
- 路径是 RFC 6901 JSON Pointer（`""` 为文档根，键名中的 `/`、`~` 分别转义为
  `~1`、`~0`）。
- 事件字段按类型出现：`add` 只有 `new`，`remove` 只有 `old`，`replace` 两者
  都有（显式 JSON `null` 值会保留）。

订阅模式沿用 JSON Pointer 转义，并允许两个通配段：

| 模式片段 | 含义                                   | 示例                                  |
| -------- | -------------------------------------- | ------------------------------------- |
| `*`      | 恰好匹配一段                           | `/users/*` 匹配 `/users/u1`           |
| `**`     | 匹配零到多段                           | `/users/**` 还匹配 `/users` 本身及任意深度 |
| 字面量   | RFC 6901 转义后的普通段                | `/a~1b` 匹配键 `a/b`                  |

通配符必须独占一整段（`/users/*-x`、`/a**` 均为非法模式）。事件**至少命中一项
include 且不命中任何 exclude** 才归入该订阅者；同一事件可分发给多位订阅者，每位
订阅者内部按 `(path, type)` 去重；没有任何订阅者命中的事件进入 `unrouted`。
结果中订阅者按 `subscriber_id` 排序，没有事件的订阅者也会以空列表返回。

```bash
curl -s -X POST localhost:8000/route-changes \
  -H 'Content-Type: application/json' \
  -d '{
    "before": {
      "users": {"u1": {"name": "Ada", "age": 36}, "u2": {"name": "Lin"}},
      "tags": ["x", "y"]
    },
    "after": {
      "users": {"u1": {"name": "Ada", "age": 37}, "u3": {"name": "Zoe"}},
      "tags": ["x", "y", "z"]
    },
    "subscriptions": [
      {"subscriber_id": "user-svc", "include": ["/users/**"]},
      {"subscriber_id": "age-watch", "include": ["/users/*/age"]},
      {"subscriber_id": "tag-svc", "include": ["/tags"]}
    ]
  }'
```

响应（数组整体 replace；`/users/u2`、`/users/u3` 作为整棵子树各一条事件）：

```json
{
  "routes": [
    {
      "subscriber_id": "age-watch",
      "events": [
        {"type": "replace", "path": "/users/u1/age", "old": 36, "new": 37}
      ]
    },
    {
      "subscriber_id": "tag-svc",
      "events": [
        {"type": "replace", "path": "/tags", "old": ["x", "y"], "new": ["x", "y", "z"]}
      ]
    },
    {
      "subscriber_id": "user-svc",
      "events": [
        {"type": "replace", "path": "/users/u1/age", "old": 36, "new": 37},
        {"type": "remove", "path": "/users/u2", "old": {"name": "Lin"}},
        {"type": "add", "path": "/users/u3", "new": {"name": "Zoe"}}
      ]
    }
  ],
  "unrouted": []
}
```

exclude 优先于 include，未被任何订阅者命中的事件进入 `unrouted`：

```bash
curl -s -X POST localhost:8000/route-changes -H 'Content-Type: application/json' -d '{
  "before": {"users": {"u1": 1}, "secret": 0},
  "after":  {"users": {"u1": 2}, "secret": 1},
  "subscriptions": [
    {"subscriber_id": "public", "include": ["/**"], "exclude": ["/secret"]}
  ]
}'
# => {"routes": [{"subscriber_id": "public", "events": [
#       {"type": "replace", "path": "/users/u1", "old": 1, "new": 2}]}],
#     "unrouted": [{"type": "replace", "path": "/secret", "old": 0, "new": 1}]}
```

非法模式、重复 `subscriber_id`、订阅数不在 1～100 之间均返回 `422`：

```bash
curl -s -X POST localhost:8000/route-changes -H 'Content-Type: application/json' -d '{
  "before": {}, "after": {"a": 1},
  "subscriptions": [
    {"subscriber_id": "s1", "include": ["/users/*-profile"]},
    {"subscriber_id": "s1", "include": []}
  ]
}'
# => {"detail": "invalid pattern '/users/*-profile': wildcard must be an entire segment ('*' or '**'), got '*-profile'"}
```

### POST /query —— 受限 JSONPath 查询

请求体包含一份 `document` 和 1～20 条查询，每条查询含唯一 `query_id` 和
JSONPath 表达式 `path`（也接受别名字段 `query`）。支持的语法（此外一律 `422`）：

| 语法 | 含义 |
| ---- | ---- |
| `$` | 文档根 |
| `.name` | 对象成员（点号简写） |
| `['name']` / `["name"]` | 括号成员键，支持反斜杠转义（`\'`、`\\`、`\uXXXX` 等） |
| `[0]` | 数组下标（负数从末尾计数，如 `[-1]` 为最后一个元素） |
| `[*]` | 通配：数组全部元素 / 对象全部成员值 |
| `..name` | 递归下降：子树（含自身）中每个对象的 `name` 成员 |
| `..*` | 递归下降：全部后代节点 |
| `[?(@.field op literal)]` | 数组过滤，`op` 为 `== != < <= > >=`，`literal` 仅允许 JSON 标量 |

语义要点：

- **过滤**：仅作用于数组元素；字段缺失或字段值为对象/数组时不命中（`!=`
  亦然）。`==`/`!=` 区分 JSON 类型（`1 ≠ true ≠ "1"`，但 `1 == 1.0`）；
  `< <= > >=` 只在两侧都是非布尔数值时比较，否则不命中（不是错误）。
- **结果**：每条查询返回 `matches`——按文档先序排列的 `{path, value}`；
  `path` 为 RFC 6901 JSON Pointer（`""` 为根，键中的 `~`、`/` 转义为
  `~0`、`~1`），同一路径只保留一次。
- **限制**：语法错误、重复 `query_id`、单条查询访问超过 10000 个节点均
  返回 `422`；查询数量须在 1～20 之间。
- **只读**：查询不执行任何脚本，也不修改文档。

```bash
curl -s -X POST localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{
    "document": {
      "store": {
        "book": [
          {"title": "Sayings", "price": 8.95, "tags": ["quote"]},
          {"title": "Sword", "price": 12.99},
          {"title": "Moby Dick", "price": 8.99}
        ],
        "bicycle": {"color": "red", "price": 19.95}
      }
    },
    "queries": [
      {"query_id": "cheap", "path": "$.store.book[?(@.price < 9)].title"},
      {"query_id": "all-prices", "path": "$..price"},
      {"query_id": "first-book-tags", "path": "$.store.book[0].tags[*]"}
    ]
  }'
```

响应（结果按请求中查询的顺序返回，每条查询内部按文档先序排列）：

```json
{
  "results": [
    {
      "query_id": "cheap",
      "matches": [
        {"path": "/store/book/0/title", "value": "Sayings"},
        {"path": "/store/book/2/title", "value": "Moby Dick"}
      ]
    },
    {
      "query_id": "all-prices",
      "matches": [
        {"path": "/store/book/0/price", "value": 8.95},
        {"path": "/store/book/1/price", "value": 12.99},
        {"path": "/store/book/2/price", "value": 8.99},
        {"path": "/store/bicycle/price", "value": 19.95}
      ]
    },
    {
      "query_id": "first-book-tags",
      "matches": [
        {"path": "/store/book/0/tags/0", "value": "quote"}
      ]
    }
  ]
}
```

语法错误、重复 `query_id`、节点超限均返回 `422`：

```bash
curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' -d '{
  "document": {"a": 1},
  "queries": [{"query_id": "bad", "path": "$.a[0:2]"}]
}'
# => {"detail": "query 'bad': invalid JSONPath '$.a[0:2]': expected ']' at position 5, got ':'"}

curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' -d '{
  "document": {},
  "queries": [{"query_id": "q", "path": "$"}, {"query_id": "q", "path": "$.a"}]
}'
# => {"detail": "duplicate query_id 'q'"}
```

### POST /resolve —— 跨文档引用物化

请求体包含 1～50 份文档（每份带唯一的 `document_id`）和一个 `root` 引用：

- **引用格式**：`document_id#/JSON/Pointer`，Pointer 遵循 RFC 6901（键名中的
  `~`、`/` 分别转义为 `~0`、`~1`）。`root` 可省略 Pointer（`"root": "base"`
  即整份文档），但必须含 document_id；嵌套 `$ref` 必须含 `#`，省略
  document_id 时（`#/JSON/Pointer`）指向**当前文档**。
- **覆盖层**：对象含 `$ref` 时先递归解析目标，再将其余键作为覆盖层——双方
  都是对象则递归合并，数组和标量由覆盖值整体替换；覆盖层的值本身也会继续
  解析（可再含 `$ref`）。目标不是对象却带覆盖键时返回 `422`。
- **循环检测**：按完整引用链检测循环（同一节点在解析链上重复出现即循环）；
  缺失文档、无效 Pointer、不存在的节点、非对象目标带覆盖键均返回 `422`，
  并在 `detail` 中给出引用链。
- **响应**：`resolved` 为物化结果；`dependencies` 为按首次访问去重的文档 id
  列表（root 文档在最前）；`sources` 为输出叶子 Pointer → 来源引用的映射，
  按输出 Pointer 排序（空对象、空数组也算叶子）。输入文档不会被修改。

跨文档覆盖示例——`prod` 引用 `base` 的 `/service` 并覆盖其中两个字段：

```bash
curl -s -X POST localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d '{
    "documents": [
      {"document_id": "base", "document": {
        "service": {
          "image": "app:1.0",
          "ports": [80],
          "limits": {"cpu": "500m", "mem": "256Mi"}
        }
      }},
      {"document_id": "prod", "document": {
        "service": {
          "$ref": "base#/service",
          "image": "app:2.0",
          "limits": {"mem": "1Gi"}
        }
      }}
    ],
    "root": "prod"
  }'
```

响应（`image`、`limits.mem` 来自覆盖层，其余来自 `base`；`sources` 给出每个
叶子的来源）：

```json
{
  "resolved": {
    "service": {
      "image": "app:2.0",
      "ports": [80],
      "limits": {"cpu": "500m", "mem": "1Gi"}
    }
  },
  "dependencies": ["prod", "base"],
  "sources": {
    "/service/image": "prod#/service/image",
    "/service/limits/cpu": "base#/service/limits/cpu",
    "/service/limits/mem": "prod#/service/limits/mem",
    "/service/ports/0": "base#/service/ports/0"
  }
}
```

循环引用返回 `422` 并给出完整引用链：

```bash
curl -s -X POST localhost:8000/resolve -H 'Content-Type: application/json' -d '{
  "documents": [
    {"document_id": "a", "document": {"x": {"$ref": "b#/y"}}},
    {"document_id": "b", "document": {"y": {"$ref": "a#/x"}}}
  ],
  "root": "a"
}'
# => {"detail": "circular reference detected: a -> b#/y -> a#/x -> b#/y"}
```

缺失文档、无效 Pointer、不存在的节点、非对象目标带覆盖键同样返回 `422`：

```bash
curl -s -X POST localhost:8000/resolve -H 'Content-Type: application/json' -d '{
  "documents": [{"document_id": "a", "document": {"x": {"$ref": "b#/y/z"}}}],
  "root": "a"
}'
# => {"detail": "document 'b' not found (reference chain: a -> b#/y/z)"}
```

## 分批提交会话

离线副本的操作不一定一次到齐。会话把**共同的初始文档、初始版本向量和
已接收的累计日志**保存在服务端，副本可以分任意多批提交操作：

- `POST /sessions`：创建会话，固定初始文档与初始版本向量，返回
  `{"session_id", "revision": 0, "protected_paths"}`。请求体可选
  `protected_paths`：最多 **20 条互不重复**的合法 RFC 6901 JSON Pointer
  （`""` 表示文档根，键名中的 `~`、`/` 分别转义为 `~0`、`~1`）；非法
  Pointer、重复路径或超过 20 条返回 `422`，不创建会话。规则随会话文件
  持久化、创建后不可更改，旧版本写出的会话按**空规则**读取。
- `POST /sessions/{session_id}/batches`：凭 `expected_revision` 提交一个
  **非空**操作批次。校验、重算与修订提交**原子**完成；响应
  `{"session_id", "revision", "accepted", "duplicates"}`。
- `POST /sessions/{session_id}/replace`：凭 `expected_revision`、`actor`、
  一条沿用 `/query` 语法的 JSONPath（字段名 `path`，别名 `query`）和
  替换值 `value`，在**当前修订**的合并文档上取**最多 20 个**已存在命中，
  按文档先序为该 actor 生成连续编号的 `set` 操作并按原有合并规则原子提交；
  响应 `{"session_id", "revision", "matched_paths", "operations"}`。
- `GET /sessions/{session_id}`：读取最新修订号、文档、版本向量、冲突、
  墓碑与 `protected_paths`（该会话创建时固定的受保护路径，旧会话为
  `[]`）。
- `POST /sessions/{session_id}/changes`：传入 `after_revision`（已读修订号）
  与 1～100 条沿用 `/route-changes` 规则的订阅，按修订顺序返回该游标之后
  最多 20 次已提交修订，每条修订是该修订文档相对**前一修订**文档的路由
  事件；响应带 `next_cursor` 供下次补读。

语义要点：

- **每次从初始文档重算**：每个批次被接受后，都按现有合并规则（因果校验、
  确定性裁决、墓碑、数组插入排序）从初始文档处理**累计日志**，上次结果
  不会被当作新基线——晚到的并发写仍参与原有裁决和冲突统计，跨批的因果
  引用（`version` 中的他人序号）可以指向此前任何批次已存的操作。
- **历史边界不可变**：每次新增修订时，提交时刻的合并文档作为该修订的
  历史边界与修订号、日志在**同一个原子写**中落盘。修订 `r` 的事件就是
  边界 `r-1 → r` 的快照 diff（与 `POST /route-changes` 的规则完全一致）。
  晚到的并发编辑会改变**当前**合并结果，但永远不会改写先前修订已返回的
  事件。修订 0 的边界是创建会话时的初始文档。
- **增量补读与空事件**：`POST /sessions/{id}/changes` 每次最多返回 20 条
  修订；`next_cursor` 为本次最后一条修订（恰好追上当前修订时即等于当前
  修订号），`has_more` 提示后面是否还有修订；没有新修订时 `changes` 为空、
  `next_cursor` 原样等于 `after_revision`。即使某次提交后的文档与前一修订
  **完全相同**（例如把字段设成已有值），该修订仍保留在历史中并以空
  `routes`/`unrouted` 返回——游标必须能逐修订推进。
- **游标范围**：`after_revision` 不得早于会话的历史起点，也不得大于当前
  修订号，否则返回 `422`；会话不存在返回 `404`；非法模式、重复
  `subscriber_id`、订阅数不在 1～100 之间同样返回 `422`。
- **升级前已有会话**：旧版本写出的会话文件没有历史边界。首次访问
  （任意读取，或第一次成功提交/整批重传）时，以当时可见的当前修订作为
  历史起点并原子持久化；早于该起点的游标返回 `422`。此后提交的新批次
  正常追加边界。过期（`409`）或非法（`422`）批次不会写入起点，也不会
  新增历史。
- **重传幂等**：操作 ID `(actor, seq)` 相同且内容一致的重传不重复保存
  （计入 `duplicates`）；整批没有新操作时不递增修订号、不新增历史边界，
  仍返回成功。
- **失败不落盘**：同 ID 内容不同、日志非法（序号断档、因果引用缺失、
  版本向量回退等）或执行失败（如对非数组 `insert`）返回 `422`；
  `expected_revision` 过期返回 `409`；会话不存在返回 `404`。这些失败
  都不会写入部分批次，会话保持原有修订与日志。
- **受保护路径**：创建会话时可在 `protected_paths` 中给出最多 20 条互不
  重复的合法 JSON Pointer（`""` 为文档根）。规则随会话持久保存、创建后
  不变；`/batches` 与 `/replace` 都在**当前修订的持锁提交**中重算出新
  文档后，逐条比较每条受保护路径在操作前、后文档上解析到的值：
  - 值不同即拦截：显式 `null` 与**缺失**不同（删除一个值为 `null` 的受
    保护位置、新建一个此前缺失的受保护位置都算变化）；布尔值与数字不同
    （`true ≠ 1`），字符串与数字不同；`1` 与 `1.0` 视为相等；对象/数组
    按结构递归比较。
  - 不需要改动命中路径本身：**祖先整体替换**导致受保护后代的值变化、
    数组 `insert`/`delete` 使受保护下标位置上的元素发生变化（下标位移）
    同样拦截；而在受保护下标之后追加元素、祖先替换后受保护子树的值原样
    保留则照常提交。
  - 任一路径变化，整次提交返回 `422`，操作日志、修订号、变更历史边界
    都不写入（旧会话迁移用的历史起点也不落盘）；错误信息指出第一条变化
    的路径。受保护位置**未变**的操作仍按原规则提交——包括确实生成了新
    操作、新修订，但可见文档未变的情况（把字段设为已有值）；整批重传与
    JSONPath 零命中保持原有行为（不递增修订、不写历史）。
  - 过期修订仍返回 `409`（修订检查先于受保护检查），日志本身非法仍按
    原规则 `422`。规则对共享同一 `SESSION_STORE_DIR` 的多个进程一致
    生效，服务（或 `docker compose`）重启后继续有效；旧版本会话按空
    规则读取，提交行为与升级前完全一致。
- **JSONPath 批量替换**：`POST /sessions/{id}/replace` 在持锁期间对
  **期望修订**的合并文档求值（选词绝不会基于旧文档写到新修订），命中按
  文档先序转成该 actor 的连续 `set` 操作：首条 seq 接续该 actor 在初始
  版本与已有日志中的最大序号，版本向量覆盖全部已有日志，并逐条覆盖本批
  先前操作；随后走与 `/batches` 完全相同的累计日志校验、从初始文档重算、
  历史边界追加与原子落盘。命中**超过 20 个**整批 `422`；命中路径之间存在
  **父子包含**关系（一个路径是另一个的祖先，含根路径 `""`）同样整批
  `422`；JSONPath 语法错误或超出查询节点预算也是 `422`。**零命中**不新增
  任何操作、不递增修订、不追加历史边界，仍返回成功（`matched_paths` 与
  `operations` 为空）；有命中时即使所有值都与现值相同（文档未变化）也
  照常递增修订并保留空事件修订。任何 `409`/`422` 失败都不留下操作或历史。
- **并发提交**：同一 `expected_revision` 的并发提交只有一个能成功，
  其余因修订号已推进而收到 `409`——该保证在共用同一 `SESSION_STORE_DIR`
  的多个服务进程之间同样成立（见下文「共享存储目录下的并发调用」）。
- **持久化**：每个会话以 JSON 文件存于 `SESSION_STORE_DIR`（默认
  `./.sessions`；docker compose 中设为 `/data/sessions`，挂载命名卷
  `session-data`），历史边界随会话同文件保存，`docker compose restart`
  或 `down` + `up` 之后会话仍可读取、可继续提交，增量分页也能从上次
  游标接续。

### 共享存储目录下的并发调用

多个服务进程（或多个容器挂载同一卷、`docker compose up --scale api=N`
多副本）可以共用同一个 `SESSION_STORE_DIR`。此时提交路径在进程内锁之外
再按会话加一把 `flock` 文件锁（存储目录下的 `.{session_id}.lock` 文件），
并在持锁期间**重新从磁盘读取**会话：修订号检查、累计日志重算、历史边界
追加与原子落盘（临时文件 + `os.replace`）作为一个跨进程临界区整体生效。
因此：

- 两个进程都已读到修订号 0、随后各自以 `expected_revision=0` 提交不同
  批次时，只有一笔成功，另一笔在锁内看到修订号已推进而返回 `409`；
  不会出现后写覆盖先写的修订丢失，成功批次的历史边界也必然与修订号
  一起对所有进程可见。
- 会话不做进程内缓存，`GET /sessions/{id}` 与增量补读总是反映磁盘上最新
  已提交的状态，任一进程都能立即读到成功批次、最新修订号并接续分页。
- 重传幂等、非法批次 `422`、过期修订号 `409`、会话不存在 `404`、游标
  越界 `422` 的语义与单进程完全一致，失败同样不落盘。
- 锁文件随会话保留（不删除，避免 unlink/reopen 竞态），数量与会话数
  相同；读取路径仅在需要为升级前会话建立历史起点时持锁落盘，其余时候
  靠原子替换保证不会读到写了一半的文件。

注意：文件锁依赖本地 POSIX 文件系统（命名卷、本地磁盘均可）；把存储目录
放到 NFS 等网络文件系统上前请自行评估锁语义。用 `--scale` 水平扩容时请
移除固定的 `8000:8000` 端口映射（或置于反向代理之后），并为所有副本挂载
同一个 `session-data` 卷。

### 可执行示例

创建会话（修订号 0）并记下 `session_id`：

```bash
SID=$(curl -s -X POST localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"document": {"title": "draft", "items": ["x"]}, "version": {}}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['session_id'])")
```

第一批：`alice` 的两条操作，修订号推进到 1：

```bash
curl -s -X POST localhost:8000/sessions/$SID/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 0,
    "operations": [
      {"actor":"alice","seq":1,"version":{"alice":1},"path":"/title","action":"set","value":"alice-title"},
      {"actor":"alice","seq":2,"version":{"alice":2},"path":"/items","action":"insert","value":"A","index":0}
    ]
  }'
# => {"session_id":"...","revision":1,"accepted":2,"duplicates":0}
```

第二批：`bob:1` 与 `alice:1` **并发**（晚到仍参与原有裁决），`bob:2`
的因果引用指向上一批已存的 `alice:2`：

```bash
curl -s -X POST localhost:8000/sessions/$SID/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 1,
    "operations": [
      {"actor":"bob","seq":1,"version":{"bob":1},"path":"/title","action":"set","value":"bob-title"},
      {"actor":"bob","seq":2,"version":{"alice":2,"bob":2},"path":"/done","action":"set","value":true}
    ]
  }'
# => {"session_id":"...","revision":2,"accepted":2,"duplicates":0}
```

读取会话：从初始文档重算后的最新状态（`("bob",1)` 赢得 `/title` 的
并发裁决，冲突照常统计）：

```bash
curl -s localhost:8000/sessions/$SID
```

```json
{
  "session_id": "...",
  "revision": 2,
  "document": {"title": "bob-title", "items": ["A", "x"], "done": true},
  "version": {"alice": 2, "bob": 2},
  "conflicts": [
    {
      "path": "/title",
      "reason": "concurrent writes; deterministic arbitration by (actor, seq)",
      "winner": {"actor": "bob", "seq": 1, "action": "set", "value": "bob-title"},
      "losers": [{"actor": "alice", "seq": 1, "action": "set", "value": "alice-title"}]
    }
  ],
  "tombstones": []
}
```

重传第一批（内容一致）：不重复保存，修订号不变：

```bash
curl -s -X POST localhost:8000/sessions/$SID/batches \
  -H 'Content-Type: application/json' \
  -d '{
    "expected_revision": 2,
    "operations": [
      {"actor":"alice","seq":1,"version":{"alice":1},"path":"/title","action":"set","value":"alice-title"},
      {"actor":"alice","seq":2,"version":{"alice":2},"path":"/items","action":"insert","value":"A","index":0}
    ]
  }'
# => {"session_id":"...","revision":2,"accepted":0,"duplicates":2}
```

过期修订号返回 `409`，同 ID 不同内容返回 `422`，二者都不写入任何操作：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/sessions/$SID/batches \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1,
       "operations": [{"actor":"carol","seq":1,"version":{"carol":1},"path":"/z","action":"set","value":1}]}'
# => 409

curl -s -X POST localhost:8000/sessions/$SID/batches \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 2,
       "operations": [{"actor":"alice","seq":1,"version":{"alice":1},"path":"/title","action":"set","value":"CHANGED"}]}'
# => {"detail":"operation alice:1 appears twice with different payloads"}
```

### POST /sessions/{id}/protected-paths —— 受保护 JSON Pointer 示例

创建会话时用 `protected_paths` 固定最多 20 条互不重复的 JSON Pointer。
下例保护根下 `locked` 字段与数组 `items` 的第 0 个元素：

```bash
SID=$(curl -s -X POST localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"document": {"locked": {"v": 1}, "items": [10, 20], "note": "draft"},
       "version": {},
       "protected_paths": ["/locked", "/items/0"]}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['session_id'])")
# => 200 {"session_id":"...","revision":0,
#         "protected_paths":["/locked","/items/0"]}
```

读取会话时规则原样返回（旧版本创建、没有规则的会话返回 `[]`）：

```bash
curl -s localhost:8000/sessions/$SID
# => {"session_id":"...","revision":0,
#     "document":{"locked":{"v":1},"items":[10,20],"note":"draft"},
#     "version":{},"conflicts":[],"tombstones":[],
#     "protected_paths":["/locked","/items/0"]}
```

非法规则在创建时直接 `422`（不以 `/` 开头、`~` 转义非法、重复路径、
超过 20 条、元素不是字符串），不会创建会话：

```bash
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"document": {}, "protected_paths": ["/a", "/a"]}'
# => {"detail":"protected JSON Pointer '/a' is listed more than once"}
# => HTTP 422

curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"document": {}, "protected_paths": ["/bad~2"]}'
# => 422
```

**被拒批次**：直接改受保护字段、整体替换祖先对象、在数组头部插入导致
受保护下标位移，整次提交都是 `422`，日志、修订与变更历史都不写入：

```bash
# 直接写受保护位置
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8000/sessions/$SID/batches \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0,
       "operations": [
         {"actor":"alice","seq":1,"version":{"alice":1},"path":"/note","action":"set","value":"ok"},
         {"actor":"alice","seq":2,"version":{"alice":2},"path":"/locked","action":"set","value":{"v":2}}
       ]}'
# => {"detail":"the operation changes protected JSON Pointer /locked"}
# => HTTP 422   (同批对 /note 的合法写入也一并回滚,修订仍是 0)

# 祖先整体替换:/locked 子树的值发生变化
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/sessions/$SID/batches \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0,
       "operations": [{"actor":"bob","seq":1,"version":{"bob":1},"path":"/locked","action":"set","value":{"v":9}}]}'
# => 422

# 在 /items 头部插入:受保护的 /items/0 从 10 变成新元素
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/sessions/$SID/batches \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0,
       "operations": [{"actor":"bob","seq":1,"version":{"bob":1},"path":"/items","action":"insert","value":9,"index":0}]}'
# => 422
```

不受保护的位置照常提交；受保护位置写回**相同的值**也照常生成新操作、
推进修订（可见文档未变，`/changes` 中该修订为空事件）；在受保护下标
之后追加元素同样允许：

```bash
curl -s -X POST localhost:8000/sessions/$SID/batches \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0,
       "operations": [
         {"actor":"alice","seq":1,"version":{"alice":1},"path":"/note","action":"set","value":"ok"},
         {"actor":"alice","seq":2,"version":{"alice":2},"path":"/items","action":"insert","value":30}
       ]}'
# => {"session_id":"...","revision":1,"accepted":2,"duplicates":0}
```

**被拒替换**：JSONPath 批量替换同样在持锁提交中检查每条受保护路径。
通配命中受保护下标、替换整个祖先数组都会被拦下：

```bash
# $.items[*] 命中 /items/0(受保护)和 /items/1 -> 422,不生成任何操作
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8000/sessions/$SID/replace \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "actor": "zoe", "path": "$.items[*]", "value": 0}'
# => {"detail":"the operation changes protected JSON Pointer /items/0"}
# => HTTP 422

# 整体替换 /items 数组,受保护下标随之消失 -> 422
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/sessions/$SID/replace \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "actor": "zoe", "path": "$.items", "value": [99]}'
# => 422

# 只命中未受保护的 /note -> 正常提交
curl -s -X POST localhost:8000/sessions/$SID/replace \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "actor": "zoe", "path": "$.note", "value": "done"}'
# => {"session_id":"...","revision":2,"matched_paths":["/note"],
#     "operations":[{"actor":"zoe","seq":1,"path":"/note"}]}
```

其余行为不变：修订号已被别的提交推进时，哪怕请求会改动受保护路径也
仍返回 `409`；零命中 JSONPath 仍是不推进修订的成功响应；`docker compose
restart`（或多进程共享卷）后规则随会话文件继续生效。

### POST /sessions/{id}/replace —— 一条 JSONPath 批量替换多个位置

服务端在**同一个提交**里完成「在当前修订合并文档上按 JSONPath 选词 →
按文档先序生成该 actor 连续编号的 `set` 操作 → 校验期望修订 → 按原有合并
规则重算并持久化」。路径语法与 `POST /query` 完全一致（点号/括号成员、
数组下标、`[*]`、`..name`/`..*` 递归下降、`[?(@.f op literal)]` 过滤）。

新建一个会话，用过滤表达式一次把数组里 `n >= 2` 的两个元素改成 `0`：

```bash
SID=$(curl -s -X POST localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"document": {"items": [{"n":1,"tag":"x"},{"n":2},{"n":3,"tag":"y"}]},
       "version": {}}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['session_id'])")

curl -s -X POST localhost:8000/sessions/$SID/replace \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0, "actor": "alice",
       "path": "$.items[?(@.n >= 2)].n", "value": 0}'
```

```json
{
  "session_id": "...",
  "revision": 1,
  "matched_paths": ["/items/1/n", "/items/2/n"],
  "operations": [
    {"actor": "alice", "seq": 1, "path": "/items/1/n"},
    {"actor": "alice", "seq": 2, "path": "/items/2/n"}
  ]
}
```

生成操作的 `version` 覆盖已有日志与本批先前操作（等价于
`{"alice":1}`、`{"alice":2}`；若此前已有别的 actor，则他们的序号也在
向量中），可直接用 `GET /sessions/$SID` 看到合并结果，或用
`POST /sessions/$SID/changes` 补读到该修订的事件。`path` 也可写作
`"query"`，替换值缺省（字段缺省）时为 JSON `null`。

零命中是成功响应，但**不递增修订、不产生操作**；接着上例（当前修订 1）：

```bash
curl -s -X POST localhost:8000/sessions/$SID/replace \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "actor": "bob",
       "path": "$.nowhere", "value": null}'
# => {"session_id":"...","revision":1,"matched_paths":[],"operations":[]}
```

命中超过 20 个、或命中路径之间存在父子包含关系时**整批拒绝**（`422`），
不写入任何操作或历史；过期 `expected_revision` 返回 `409`：

```bash
# 21 个命中 -> 422
SID2=$(curl -s -X POST localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"document": {"items": [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]},
       "version": {}}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['session_id'])")
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8000/sessions/$SID2/replace \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0, "actor": "alice", "path": "$.items[*]", "value": 0}'
# => {"detail":"JSONPath matched 21 nodes; at most 20 may be replaced in one request"}
# => HTTP 422

# 递归下降同时命中祖先 /items 和后代 /items/0 -> 父子包含,422
curl -s -w '\nHTTP %{http_code}\n' -X POST localhost:8000/sessions/$SID/replace \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "actor": "alice", "path": "$..*", "value": 0}'
# => {"detail":"matched paths '/items' and '/items/0' are in a parent/child relationship; ..."}
# => HTTP 422

# 修订号已被别的提交推进 -> 409,旧选词不会写到新文档
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/sessions/$SID/replace \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 0, "actor": "carol", "path": "$.items", "value": []}'
# => 409
```

### POST /sessions/{id}/changes —— 按修订号增量补读路由事件

调用方分批提交后，用已读修订号作为游标补读每次实际生效的文档变更。
请求体包含 `after_revision` 和 1～100 条订阅，订阅的 include/exclude
模式、通配段、`unrouted` 等规则与 `POST /route-changes` 完全相同。
按修订顺序返回游标之后**最多 20 次**已提交修订，每条修订的事件是该修订
文档相对于**前一修订**文档的 diff；`next_cursor` 是本次最后一条修订号，
下次请求原样带回即可继续翻页。

接着上例（当前修订号为 2），从修订 0 补读：

```bash
curl -s -X POST localhost:8000/sessions/$SID/changes \
  -H 'Content-Type: application/json' \
  -d '{
    "after_revision": 0,
    "subscriptions": [
      {"subscriber_id": "title-svc", "include": ["/title"]},
      {"subscriber_id": "items-svc", "include": ["/items"]}
    ]
  }'
```

```json
{
  "session_id": "...",
  "current_revision": 2,
  "after_revision": 0,
  "next_cursor": 2,
  "has_more": false,
  "changes": [
    {
      "revision": 1,
      "routes": [
        {"subscriber_id": "items-svc", "events": [
          {"type": "replace", "path": "/items", "old": ["x"], "new": ["A", "x"]}]},
        {"subscriber_id": "title-svc", "events": [
          {"type": "replace", "path": "/title", "old": "draft", "new": "alice-title"}]}
      ],
      "unrouted": []
    },
    {
      "revision": 2,
      "routes": [
        {"subscriber_id": "items-svc", "events": []},
        {"subscriber_id": "title-svc", "events": [
          {"type": "replace", "path": "/title", "old": "alice-title", "new": "bob-title"}]}
      ],
      "unrouted": []
    }
  ]
}
```

要点：

- **逐修订 diff**：修订 2 的 `old` 是修订 1 的文档（`alice-title`），
  不是初始文档。修订 1 的事件在修订 2 提交后也不会变化——尽管当前合并
  结果里 `/title` 已被晚到的 `bob:1` 改成 `bob-title`，历史边界不可变。
- **没有新修订时游标原样返回**：用 `next_cursor` 轮询，`changes` 为空、
  `next_cursor` 等于传入的 `after_revision`：

```bash
curl -s -X POST localhost:8000/sessions/$SID/changes \
  -H 'Content-Type: application/json' \
  -d '{"after_revision": 2,
       "subscriptions": [{"subscriber_id": "title-svc", "include": ["/title"]}]}'
# => {"session_id":"...","current_revision":2,"after_revision":2,
#     "next_cursor":2,"has_more":false,"changes":[]}
```

- **文档未变化也保留修订**：一次提交若把字段设为它已有的值，修订号
  照常递增、历史照常保留，该修订以空事件返回（游标仍能逐修订推进）。
- **错误码**：游标早于历史起点或超过当前修订返回 `422`；会话不存在
  返回 `404`；非法模式、重复 `subscriber_id`、订阅数不在 1～100 返回
  `422`：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/sessions/$SID/changes \
  -H 'Content-Type: application/json' \
  -d '{"after_revision": 9, "subscriptions":[{"subscriber_id":"s","include":["/**"]}]}'
# => 422  ({"detail":"after_revision 9 is outside the known history ..."})
```

#### 分页补读循环（超过 20 次修订）

每次响应最多 20 条修订；`has_more` 为 `true` 时把 `next_cursor` 作为
下次的 `after_revision` 继续请求，直到它追上 `current_revision`：

```bash
# 连续提交 25 个批次（每个批次推进一次修订号）
SID=$(curl -s -X POST localhost:8000/sessions \
  -H 'Content-Type: application/json' \
  -d '{"document": {"n": 0}, "version": {}}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['session_id'])")
for i in $(seq 1 25); do
  curl -s -o /dev/null -X POST localhost:8000/sessions/$SID/batches \
    -H 'Content-Type: application/json' \
    -d "{\"expected_revision\": $((i-1)),
         \"operations\": [{\"actor\":\"alice\",\"seq\":$i,\"version\":{\"alice\":$i},\"path\":\"/n\",\"action\":\"set\",\"value\":$i}]}"
done

# 用 next_cursor 循环翻页，直到 has_more=false（SID 从环境变量传入）
SID="$SID" python3 - <<'PY'
import json, os, urllib.request

sid = os.environ["SID"]
cursor = 0
while True:
    req = urllib.request.Request(
        f"http://localhost:8000/sessions/{sid}/changes",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps({
            "after_revision": cursor,
            "subscriptions": [{"subscriber_id": "n-svc", "include": ["/n"]}],
        }).encode(),
    )
    page = json.load(urllib.request.urlopen(req))
    for change in page["changes"]:
        event = change["routes"][0]["events"][0]
        print(f"revision {change['revision']}: {event['old']} -> {event['new']}")
    print(f"-- next_cursor={page['next_cursor']} "
          f"current={page['current_revision']} has_more={page['has_more']}")
    if not page["has_more"]:
        break
    cursor = page["next_cursor"]
PY
```

输出（第一页 20 条，第二页 5 条；每次 diff 的 `old` 是紧邻的前一修订）：

```text
revision 1: 0 -> 1
...
revision 20: 19 -> 20
-- next_cursor=20 current=25 has_more=True
revision 21: 20 -> 21
...
revision 25: 24 -> 25
-- next_cursor=25 current=25 has_more=False
```

重启服务（`docker compose restart`）后用最后保存的 `next_cursor` 继续
请求即可，历史边界随会话持久化，无需从头补读。

#### 升级前已有会话的历史起点

旧版本创建、本次升级后首次被访问的会话，以首次访问（`GET`、`changes`
或成功提交/整批重传）时可见的当前修订作为历史起点；早于起点的游标
返回 `422`，从起点开始的后续修订正常分页：

```bash
# 假设升级前该会话已有修订 0..3，首次 changes 访问：
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/sessions/$SID/changes \
  -H 'Content-Type: application/json' \
  -d '{"after_revision": 2, "subscriptions":[{"subscriber_id":"s","include":["/**"]}]}'
# => 422  (2 早于首次访问时建立的历史起点 3)
curl -s -X POST localhost:8000/sessions/$SID/changes \
  -H 'Content-Type: application/json' \
  -d '{"after_revision": 3, "subscriptions":[{"subscriber_id":"s","include":["/**"]}]}'
# => {..."next_cursor":3,"changes":[]}  -- 起点之后提交的新批次才会出现
```

## 项目结构

```
app/
  main.py      # FastAPI 路由：POST /merge、POST /apply、POST /route-changes、POST /query、POST /resolve、
               # POST /sessions、POST /sessions/{id}/batches、GET /sessions/{id}、
               # POST /sessions/{id}/replace、POST /sessions/{id}/changes、GET /healthz
  models.py    # Pydantic 请求/响应模型
  engine.py    # 合并引擎：向量时钟、校验、因果排序、冲突检测、应用器
  routing.py   # 快照递归 diff、JSON Pointer 通配模式匹配、订阅分发
  query.py     # 受限 JSONPath 解析与求值（只读、单查询 10000 节点预算）
  resolve.py   # 跨文档 $ref 物化：覆盖层合并、引用链循环检测、叶子来源映射
  sessions.py  # 分批会话：累计日志、不可变历史边界、跨进程原子提交（文件锁内校验+重算+边界+修订）、
               # 受保护 JSON Pointer 规则（创建时持久化、持锁提交前后逐路径比对、变化即 422 不落盘）、
               # JSONPath 批量替换（锁内选词+生成连续 set 操作+同一临界区提交）、
               # 增量修订补读（每次最多 20 条、游标分页）、旧会话历史起点迁移、JSON 文件持久化
tests/
  test_api.py              # 22 个 merge/apply 端到端测试
  test_exclude.py          # 29 个 merge/apply 排除裁剪端到端测试
  test_route_changes.py    # 30 个 route-changes 端到端测试
  test_query.py            # 73 个 /query 端到端测试
  test_resolve.py          # 37 个 /resolve 端到端测试
  test_sessions.py         # 22 个分批会话端到端测试
  test_session_changes.py  # 27 个增量修订补读端到端测试（分页、不可变边界、旧会话迁移、持久化）
  test_session_replace.py  # 35 个 JSONPath 批量替换端到端测试（选词、连续编号、父子/超限 422、原子并发、历史）
  test_session_protected.py # 46 个受保护 JSON Pointer 端到端测试（规则校验/持久化、批次与替换拦截、
                            #   缺失/null/类型差异、祖先替换与数组位移、空可见变更仍提交、重启与多进程、旧会话空规则）
conftest.py
Dockerfile
docker-compose.yml
requirements.txt
```
