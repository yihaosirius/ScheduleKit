# ScheduleKit

围绕学业构建的个人任务管理套件：**双列表 WebUI**（PC / 移动端 / PWA）+ **iPhone 快捷指令拍照录入** + **LLM 结构化解析** + **课表上下文** + **记忆系统** + **Scriptable 小组件**。

服务端部署在 Ubuntu 24.04，通过 Caddy 反代到 `https://canisa1ph.duckdns.org:8443`。

---

## 它解决什么问题

普通待办应用的痛点是"录入太贵"：拍一张作业群聊的截图，要手动输入标题、科目、
截止时间、备注，五步操作换来一条任务。本项目把这个过程压缩成**拍一张照**：

1. iPhone 快捷指令拍照（可加一句文字备注）→ 上传
2. 服务端把「现在几点 / 第几教学周 / 正在上什么课 / 你的长期记忆」拼进提示词
3. LLM 以**强制 function calling**返回结构化条目
4. 进**草稿箱**——此时任务表**零写入**
5. 你在确认页改完再点确认，才真正入库

第 4 步是关键：识别结果一定有错的时候，而"错了但已经入库"比"没识别出来"更烦人。

---

## 核心特征

### 双列表：任务按「截止时间 vs 优先级」二选一

| 列表 | 条件 | 排序 |
|---|---|---|
| **有序表** | 有 `due_at` | 按截止时间升序，卡片权重高，带倒计时 |
| **无序表** | 有 `priority`（Ⅰ–Ⅴ） | 按档位分组，组内按创建时间，卡片权重明显更小 |
| 已完成 | `status='done'` | 按完成时间倒序 |

"恰好一个非空"这条约束在**库层用 `CHECK` 强制**，服务层再给中文错误，
单测覆盖每一侧。两列并存而不是"一个 type + 一个 value"，是为了让排序与索引天然可用。

### 三通道结构化输出（不可信前提的落地）

主通道是**强制 function calling**（`strict: true`，约束解码）；拿不到时就降级，
但**降级必须响亮**——落库 `llm_path`、打 WARNING、在确认页标出来。

```
1. tool_call    强制 function calling（关掉 thinking + 具名 tool_choice + strict）
2. json_schema  Responses 协议的结构化输出
3. json_object  Chat 协议的 JSON 模式
```

为什么必须有这条例外（两条都是文档/实测确认的，不是想当然）：

- **DeepSeek 在 thinking 模式下拒绝 `tool_choice` 传 `required` 或具名工具**，直接 400。
  也就是说"强制工具调用"与"开思考"目前不可兼得。所以主通道必须显式关掉 thinking。
- "模型没产出 function call"是 200 响应下的真实失败模式，无法事前探测。

两套协议的**请求形状不同**（Responses 的工具定义与 `tool_choice` 是扁平的，
Chat 的是嵌套的），混了会得到 400 而不告诉你抄错了形状。
所以 `tests/test_llm_channels.py` 用 MockTransport 逐字段断言两套形状。

### 服务端校验是唯一权威

模型输出一律当脏数据过 `services/normalize.py`。并且：

> **凡代码推翻了模型输出的地方，必须打点。**

否则事后无法区分"模型错了"还是"后处理改的"——这两种情况的修法完全相反
（前者改 prompt，后者改代码）。所以 `NormalizeResult` 除了 `items` 还返回
`overrides`（人话说明）与 `dropped`，一路带到确认页。

### 课表上下文：让「下节课交」能被解析

服务端在每次识别时注入一段上下文：现在几点、第几教学周、**正在上 / 刚下课 /
即将开始哪门课**、今天还有什么课，以及几个"可以直接照抄的截止时刻"。

后者比反复叮嘱模型"请仔细计算日期"有效得多——它只要挑一个就行。

上下文与指令**分离**：指令（system prompt）保持静态以便命中 prompt 缓存，
上下文每次重新拼进用户消息。上下文里还显式声明自己的边界
（"不得作为待办事项的主语""不得影响优先级判定"），否则模型会把
"正在上电子电路"理解成"这件事很急"。

### 记忆系统

记忆是**给模型的参考素材，不是待办事项**。它解决消歧：
"电子电路基础的作业周三交"这条约定，能让只写着"这次作业"的截图补出课程名与日期。

注入优先级：`pin_single`（用户显式单条注入）> `scope='global'` >
`scope='course'`（只在对应课程出现在上下文里时注入）。
`pin_single` 是**一次性**的意图，用掉即清——否则用户会以为开关失灵。

### 其他

- **鉴权两条路径**：Web 用签名 Cookie（`HttpOnly`）+ HMAC-CSRF；
  快捷指令/小组件用 API Key（`Authorization: Bearer`，库里只有 sha256）。
  API Key **不走 CSRF**——`Authorization` 头不会被浏览器自动携带，加 CSRF 只会让快捷指令不能用。
- **单文件配置**：全服务器只有 `/etc/schedulekit/config.toml`。控制台可改且**保留注释**
  （tomlkit），密钥可用环境变量覆盖。
- **低内存**：uvicorn + Caddy 合计目标 ≤ 200MB（服务器只有 1.6G 内存）。

---

## 目录结构

```
AGENTS.md              项目级开发约定（含 trace 条款，由测试守住）
app/                   服务端（唯一 Python 包）
  config.py            tomlkit 原子写回 + 保留注释 + env 覆盖
  security.py          scrypt / 签名 Cookie / HMAC-CSRF / API Key
  deps.py              鉴权依赖（会话与 API Key 两条路径）
  main.py              应用装配：trace id、请求级配置热加载、错误映射
  media.py             图片校验（真解一次，不信 content-type）
  migrations/          手写序号迁移（在一个事务内执行）
  routers/             只做鉴权与参数校验
  services/            业务规则（可脱离 HTTP 单测）
    tasks.py           二选一约束、三视图排序、幂等
    drafts.py          草稿状态机（草稿阶段零写入）
    normalize.py       模型输出的强制后处理
    context.py         时间上下文构建（纯函数）
    timetable.py       课表解析与"此刻上什么课"
    memory.py          记忆 CRUD 与注入选择
  llm/                 统一适配器
    tools.py           submit_tasks schema + 三套协议形状（唯一真相）
    structured.py      两个协议的传输层 + 重试 + 三通道降级
  static/              图标 / manifest / sw.js / SPA 产物
deploy/                install.sh / Caddyfile / systemd / 拆除脚本
clients/scriptable/    iPhone 小组件
shortcuts/             快捷指令搭建说明
docs/                  接口、决策、trace、部署、快捷指令、小组件
tests/                 pytest（全程不访问网络）
config.toml.example    唯一配置的模板（无密钥）
```

---

## 本地开发

需要 [uv](https://docs.astral.sh/uv/)。

```bash
cp config.toml.example config.toml
uv sync
uv run python -m app.cli init          # 生成 secret_key、设置管理员密码、建库
uv run python -m app.serve --reload    # 默认 127.0.0.1:8000
```

打开 http://127.0.0.1:8000（前端未构建时会看到一个说明页，接口仍可用）。

> LLM 未配置时会用 `provider = "mock"`，**离线确定性抽取**，方便先把链路跑通。
> 配 `[llm].api_key` 或 `export SK_LLM_API_KEY=...` 后换成 `responses`。

### 测试

```bash
uv run pytest -q -p no:cacheprovider
```

`-p no:cacheprovider` 是**必须**的（沙箱里 `.pytest_cache` 建不出来）。
测试**不访问网络**：LLM 用 `app/llm/mock.py`，协议形状用 httpx `MockTransport`。

### 前端（SPA）

```bash
cd web && pnpm install && pnpm build    # 产物写到 app/static/spa/
```

产物**提交入库**，所以服务器不需要 Node。

---

## CLI

所有命令输出 JSON（部署脚本不自己解析 TOML）。

| 命令 | 作用 |
|---|---|
| `python -m app.cli init` | 生成 `secret_key`、交互式设置密码、建库 |
| `python -m app.cli set-password` | 改密码（`session_epoch` 自增，旧会话全部失效） |
| `python -m app.cli migrate` | 应用数据库迁移 |
| `python -m app.cli show --json` | 输出配置 JSON（密钥已脱敏） |
| `python -m app.cli show --json --secrets` | 含密钥，**仅供 root 部署脚本** |
| `python -m app.cli new-key --name X` | 创建 API Key（`--read-only` 建只读密钥） |
| `python -m app.cli revoke-key ID` | 吊销 |
| `python -m app.cli backup` | 一致性备份（sqlite backup API，不是拷文件） |
| `python -m app.cli check` | 启动前自检 |

---

## 部署

服务器上**只**动 ScheduleKit 自己的资源；不得触碰同机的
cloudreve / postgres / redis / portainer / `caddy-webdav.service`
以及它们的端口（8078 / 8001 / 6888 / 8079）。

**端口是 8443，不是 443。** 两条硬约束：已有快捷指令写死了这个端口；
80/443 是备案相关端口。有测试锁住这个选择，别"顺手改成 443"。

```bash
sudo bash deploy/install.sh --init    # 交互式生成 config.toml 并部署
sudo bash deploy/install.sh           # 改完配置后幂等重部署
```

细节与排障见 [`docs/deploy.md`](docs/deploy.md)。

> Caddyfile 里有 `admin off`，它与 `caddy reload` **互斥**（reload 走 :2019 管理端点），
> 所以改配置后必须 `systemctl restart caddy`。

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | **开发约定**：trace 条款、交互节奏、代码约定 |
| [`docs/dev-trace.md`](docs/dev-trace.md) | 开发流水账（每轮改动一条，含决策的被否决方案） |
| [`docs/decisions.md`](docs/decisions.md) | 决策记录（ADR 式，D-001 … ） |
| [`docs/api.md`](docs/api.md) | HTTP 接口规格（快捷指令 / agent 对接看这个） |
| [`docs/deploy.md`](docs/deploy.md) | 部署与排障 |
| [`docs/shortcuts.md`](docs/shortcuts.md) | Apple 快捷指令逐步搭建 + 必踩的坑 |
| [`docs/widget.md`](docs/widget.md) | Scriptable 小组件 |
| [`docs/push-notes.md`](docs/push-notes.md) | 推送方案调研（本期未做） |
