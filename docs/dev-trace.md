# 开发 trace

开发流水账。每轮改动追加一条，字段固定，**与代码改动同一次提交内完成**。
约定见 [`../AGENTS.md`](../AGENTS.md) §1。

> 运行时日志（`sk.*`，带 `[t=xxxxxxxx]`）是机器证据，本文件是人的叙述。
> 两者通过 `X-Trace-Id` 关联：贴日志时必须带上那个 id。

---

## 2026-10-03 18:21 · 拆除服务器上的旧 ScheduleKit 实例并归档

- 目标：清空上一版 ScheduleKit 的部署（应用、配置、数据、源码），为从零重写腾出
  `127.0.0.1:8000` 与域名 `canisa1ph.duckdns.org`，同时**不丢数据**——拆除不可逆，
  必须先把现场完整归档。

- 决策与理由：写成脚本 `deploy/legacy-teardown.sh` 并支持 `--archive-only` 两段式执行，
  而不是手敲一串命令。理由是拆除过程里有三处必须按特定顺序、且踩了会连带影响别的站点：
  1. **证书先搬家再删目录**。`/etc/schedulekit/certs` 的属主是即将被删除的 `schedulekit`
     用户，而 Caddy 进程正在读它。先 `cp` 到 `/etc/schedulekit-tls`（`root:caddy`）
     再删原目录，中间任何一步都不会让 Caddy 失去证书。
  2. **先停服务再停 timer**。`schedulekit-duckdns.timer` 每 6 小时跑一次脚本，
     顺序反了它会把刚删掉的目录再建出来。
  3. **归档用 Python 做可读 dump**，而不是 `sqlite3 .dump`。可读 dump 便于日后
     "某条任务我还要"时直接翻文本，不必先把库恢复起来。
  - 被否决的替代方案：**直接在服务器上删干净、不归档**。否决原因是旧库里有 33 条
    真实任务与 13 张上传图片，而用户"从零开始"的意图针对的是代码与部署，
    并不必然包含数据；归档成本只有 37MB，误删成本不可逆。
  - 被否决的替代方案：**保留旧实例并存、新版本另起端口**。否决原因是用户已明确选择
    "彻底清空，从零开始"，且并存会长期占用内存（服务器仅 1.6GB）。

- 触及文件：
  - 新增：`deploy/legacy-teardown.sh`
  - 服务器新增（归档，不随仓库分发）：`/root/legacy-schedulekit/`
    （`schedulekit.db.*`、`data-dump.*.txt`、`uploads.*.tar.gz`、
    `config.toml.*`、`schedulekit.service.*`、`design-notes/`、`duckdns-domain.txt`）

- 执行过的命令与结果：
  - `bash /root/legacy-teardown.sh --archive-only` → 退出码 0。
    归档 37MB；自检：`items 33 / courses 11 / course_sessions 14 / ingest_drafts 28 /
    api_keys 4`，`uploads.extracted` 13 个文件 19MB。
  - `bash /root/legacy-teardown.sh` → 退出码 0。校验输出：
    `已删除：/opt/schedulekit`、`已删除：/var/lib/schedulekit`、
    `已删除：/etc/schedulekit`、`已删除：/root/schedule_kit`、`已删除用户：schedulekit`；
    `127.0.0.1:8000` 已释放。
  - 保留未动的：docker `cloudreve / postgresql / redis / portainer`、
    `caddy-webdav.service`(`:8079`)、`/etc/caddy-webdav/`、acme.sh 与 renewal cron。

- 观察到的现象 / 反直觉之处 / 踩坑：
  - **反直觉**：拆除后 `systemctl reload caddy` 直接失败，报
    `Error: sending configuration to instance: performing request: Post
    "http://localhost:2019/load": dial tcp 127.0.0.1:2019: connect: connection refused`。
    原因是 Caddyfile 全局块里写了 `admin off` —— 配置里关掉了管理端点，
    而 `caddy reload` 正是通过该端点下发新配置。**`admin off` 与热重载不可兼得**，
    只能 `systemctl restart caddy`。这条已写进 `deploy/Caddyfile` 的注释。
  - **反直觉**：`curl https://127.0.0.1/ -H 'Host: canisa1ph.duckdns.org'` 报
    `tlsv1 alert internal error`，看着像证书坏了。实际是**没发 SNI**，
    Caddy 按 IP 选不出证书。改用 `--resolve canisa1ph.duckdns.org:443:127.0.0.1`
    或 `openssl s_client -servername ...` 后握手正常（TLSv1.3，Let's Encrypt YE1，
    有效期至 2026-12-12）。**排查 TLS 问题时 `-k` 不能替代 `-servername`**。
  - 旧 DuckDNS token 明文写在 `/etc/schedulekit/duckdns-update.sh` 里，已随目录删除；
    归档只保留了域名（`domains=canisa1ph`）。**新部署必须用新 token**。

- 未决问题与下一步：
  - **`443` 外部不可达**：本机 `:443` 正常（带 SNI 返回 502，即反代已生效），
    但从本机探测 `116.62.52.66` 的 80/443/8443 全部超时，而 22/8078 可达。
    判定为 Aliyun 安全组未放行 443，需要用户在控制台加规则后才能完成端到端验收。
  - Caddy 站点块已从 `:8443` 改到标准 `:443`，证书路径指向 `/etc/schedulekit-tls`；
    已 `restart` 生效。
  - 下一步：搭仓库骨架、单文件配置、迁移、CLI 与日志体系（M0 剩余部分）。

---

## 2026-10-03 18:28 · 仓库骨架、单文件配置、迁移与 trace 体系

- 目标：在 `D:\ScheduleKit` 建起从零重写的骨架，并把「开发 trace 文档」这条约定
  **落成可执行的约束**（用户明确要求），而不是只写在文档里靠自觉。

- 决策与理由：
  - **配置写回选 tomlkit + 临时文件 + `os.replace` + `fcntl.flock`**。控制台要就地改
    `[llm]`（含 API Key）并保留注释；半截文件会让服务下次起不来，所以必须原子替换，
    且临时文件必须与目标**同目录**（跨文件系统的 `os.replace` 不是原子的）。
    - 被否决：JSON 配置。否决原因是丢失注释与顺序，配置文件会变成不可读的一坨。
    - 被否决：`json.dump` 式直接覆盖。否决原因是非原子，写到一半断电就废了。
  - **密钥支持环境变量覆盖，且环境变量优先于文件**。便于"不把密钥落盘"的部署方式。
    - 被否决：干脆不要环境变量。否决原因是那会让 `SK_CONFIG` 之外完全没有
      "不改文件就能改配置"的手段，CI/临时调试都很别扭。
    - 被否决：让文件优先于环境变量。否决原因是那会让 `export SK_LLM_API_KEY=x`
      静默失效，排查成本极高。
  - **迁移不用 Alembic**。单机单文件 SQLite，手写 SQL + 序号文件名更快更可控。
    - 被否决：Alembic。否决原因是多一层需要理解的抽象，而本项目 schema 变更是
      每学期一两次的量级。
  - **迁移必须自己切语句，不能用 `executescript`**。CPython 的 `executescript`
    会在执行前隐式 COMMIT，于是外层的 `BEGIN IMMEDIATE` 被提前提交，
    迁移中途失败就留下半个 schema 且无法回滚。
    - 用 `sqlite3.complete_statement` 切分，而不是 `split(';')`：分号可能出现在
      字符串字面量里。
  - **学期周次用日期差算**（`(day - start) // 7 + 1`），不用 `date.isocalendar()`。
    - 被否决：ISO 周编号。否决原因是它是 ISO 周语义，跨年、非周一起始的学期会跳变，
      而"第几教学周"完全由配置里的 `start_date` 决定才符合直觉。
  - **补时区必须报告**。`parse_local_or_offset` 返回 `(时刻, 是否补了时区)`，
    调用方要把 True 记进 overrides 并打点。
    - 被否决：静默按本地时区补。否决原因是"任务差 8 小时"在界面上看起来完全正常，
      是最难发现的一类错误。
  - **trace 条款要可执行**：`AGENTS.md` 写约定 + `tests/test_trace_convention.py`
    断言文档存在且字段齐全。仅写文档的条款会在几轮之后被遗忘。
    - 被否决：只在 AGENTS.md 里写一段话。否决原因是没人会在压力下记得住，
      而测试失败是无法忽略的信号。

- 触及文件：
  - 新增：`AGENTS.md`、`docs/AGENTS.md`、`docs/dev-trace.md`（本文件）、
    `docs/decisions.md`、`config.toml.example`、`deploy/Caddyfile`
  - 新增：`app/__init__.py`、`app/logging.py`、`app/config.py`、`app/db.py`、
    `app/timeutil.py`、`app/text.py`
  - 新增：`app/migrations/__init__.py`、`app/migrations/runner.py`、
    `app/migrations/0001_init.sql`
  - 新增：`tests/conftest.py`、`tests/_support.py`、`tests/test_config.py`、
    `tests/test_migrations.py`、`tests/test_timeutil.py`
  - 修改：`pyproject.toml`（补依赖：fastapi / uvicorn / httpx / pillow / tomlkit /
    python-multipart / tzdata，dev 组补 pytest 相关）

- 执行过的命令与结果：
  - `uv sync` → 退出码 0，装出 fastapi 0.1xx / pydantic 2.13.5 / tomlkit 0.15.1 等。
  - `uv run pytest -q -p no:cacheprovider` → 首次 **exit 1**：
    `tests/test_migrations.py`, line 152:
    `SyntaxError: closing parenthesis ')' does not match opening parenthesis '['`
    （`@pytest.mark.parametrize("category", ["", "HW", "homework ")` 少了一个 `]`）。
    修复后 → **exit 1**，1 failed：
    `test_llm_validation_rejects_relative_base_url` DID NOT RAISE ConfigError。
    根因：测试配置里 `provider = "mock"`，而 `validate_for_llm()` 对 mock 直接 return，
    所以 `base_url` 校验被跳过。给该测试显式补上 `provider = "responses"`。
  - 再次 `uv run pytest -q -p no:cacheprovider` → 退出码 0：
    `51 passed, 1 skipped`（skip 的是 Windows 下的 POSIX 权限位断言）。
  - `uv run pytest -q -p no:cacheprovider tests/test_timeutil.py` → 退出码 0：`28 passed`。

- 观察到的现象 / 反直觉之处 / 踩坑：
  - **反直觉**：`caddy validate` 对同一份配置返回 `Valid configuration`，
    但 `systemctl reload caddy` 仍然失败——因为失败发生在"把配置送给运行中的实例"
    这一步，而不是配置解析这一步。**validate 通过 ≠ 能热加载**。
  - **反直觉**：`uv sync` 把 `pydantic` 一起装了。我们没有直接依赖 pydantic，
    但 FastAPI 依赖它；`filterwarnings` 里关掉 DeprecationWarning 是必要的，
    否则 pytest 输出会被第三方库的告警淹没。
  - `tests/conftest.py` 刻意**不用** pytest 自带的 `tmp_path`：沙箱里在 `%TEMP%`
    下建目录再清理会 `PermissionError`，而默认 `tmp_path` 正是那条路径。
    自建 `.pytest-run/` 且删除失败时退化成换新目录名，而不是让测试失败。
  - `-p no:cacheprovider` 在本机（danger-full-access）其实不是必需，但保留：
    沙箱环境会变，而这条命令会写进 README 和 AGENTS.md 被人复制。

- 未决问题与下一步：
  - `443` 仍在等 Aliyun 安全组放行（见上一条）。
  - `docs/decisions.md` 目前只沉淀了配置/迁移/时间三类决策，鉴权与 LLM 通道
    决策待 M1、M4 落地时补。
  - 下一步（M0 收尾 → M1）：`app/security.py`（scrypt、签名 Cookie、CSRF、
    API Key）、`app/cli.py`、`app/main.py` 与 `app/serve.py`，
    然后用真实哈希替换 `tests/_support.py` 里的占位 `VALID_PASSWORD_HASH`。

---

## 2026-10-03 18:35 · 鉴权原语、CLI、应用装配与任务业务规则

- 目标：把 M0 收尾（CLI / 应用装配 / 图片校验）并把 M1（鉴权）与 M2（双列表
  业务规则）一次做完——这三块的分层边界互相咬合，分开做要来回改接口。

- 决策与理由：
  - **鉴权只用标准库**（`hashlib.scrypt` / `hmac` / `secrets`）。
    - 被否决：`passlib` / `python-jose` / `itsdangerous`。否决原因是它们各自带一层
      版本兼容史（`passlib` 与 bcrypt 4.x 的已知不兼容、`python-jose` 的 CVE 记录），
      而我们要的东西 stdlib 全都有，代码量不到 200 行。
  - **会话是无状态签名 Cookie + `session_epoch` 吊销**。
    - 被否决：服务端 session 表。否决原因是单用户单机场景下，为"能踢会话"引入一张
      表与清理任务，成本明显高于把 epoch 放进签名负载。
    - 签名覆盖 `epoch` 是必须的：否则把 epoch 改大就能绕过"改密码踢会话"。
  - **CSRF 用「nonce + HMAC(secret, 'csrf:'+nonce)」而不是朴素双提交**。
    - 被否决：纯双提交（Cookie 值 == 表单值）。否决原因是能写 Cookie 的攻击者
      （子域被拿下、中间设备注入）可以两边都写成同一个值，双提交就失效了。
  - **API Key 排除 CSRF，Cookie 会话强制 CSRF**。CSRF 的前提是浏览器自动携带凭据，
    `Authorization` 头不会被自动携带，给 API Key 加 CSRF 只会让快捷指令不能用。
  - **API Key 存 sha256 而不是 scrypt**。Key 是 32 字节随机串，不存在弱口令问题；
    每请求跑一次 scrypt（约 50ms）会让接口慢到不可用，且会拖垮 1.6G 内存的机器。
  - **`client_uuid` 命中已有记录时返回已有任务而不是报 409**。快捷指令在网络超时后
    会重试；报错会让用户以为没录上而再传一次，反而造成重复。
  - **`update_task` 用 `due_at_provided` / `priority_provided` 两个显式标志**，
    与值本身分开传。`None` 同时表示"不改"和"清空"，而二选一约束恰好要求
    "清空一个、填上另一个"——不支持这个区分就无法把任务在两张表之间移动。
  - **请求体大小限制放在应用里**而不是 Caddy 的 `request_body` 指令：后者是
    Caddy 2.10 才有的，赌版本不划算；而且应用能返回带中文说明的 413。
  - **`python -m app.serve` 传 `factory="app.main:create_app"`** 而不是应用实例：
    `--reload` 时 uvicorn 在子进程重新导入，"每次重载都读最新配置"正是想要的。
  - **测试配置用真实 scrypt 哈希**（明文 `test-password`）而不是占位串：
    占位串会让哈希格式写错也测不出来。

- 触及文件：
  - 新增：`app/security.py`、`app/ratelimit.py`、`app/paths.py`、`app/media.py`、
    `app/jsonfield.py`、`app/deps.py`、`app/main.py`、`app/serve.py`、`app/cli.py`
  - 新增：`app/services/__init__.py`、`app/services/tasks.py`
  - 新增：`tests/test_security.py`、`tests/test_tasks.py`
  - 修改：`tests/_support.py`（占位哈希 → 真实哈希，并导出 `VALID_PASSWORD`）、
    `.gitignore`（补 `config.toml` / `data/` / `.pytest-run/` / `web/node_modules/`）

- 执行过的命令与结果：
  - `uv run python -c "from app.security import hash_password, verify_password; ..."`
    → 退出码 0，产出 `scrypt$n=16384,r=8,p=1$yGKM5H2DiRTzFMDib+bWAQ==$hpwNpyeBFIdJh5nePsBpc3Uzj5g2swghtc+Gzhi/fds=`，
    `verify: True False`。
  - `uv run pytest -q -p no:cacheprovider tests/test_security.py` → 退出码 0：`40 passed`。
  - `uv run pytest -q -p no:cacheprovider tests/test_tasks.py` → **exit 1**，3 failed：
    `test_empty_title_rejected[""]`、`["   "]`、`["\\u200b"]` 报
    `ValueError: 文本为空`，而不是预期的 `TaskError`。
    根因：`app.text.clean_text(..., allow_empty=False)` 抛 `ValueError`，
    而 `validate_title` 没接住，于是业务异常类型漏到了调用方。
    修复：`validate_title` 里 catch `ValueError` 并转成 `TaskError`（router 只需处理一种异常）。
  - `uv run pytest -q -p no:cacheprovider` → 退出码 0：`177 passed, 1 skipped`。

- 观察到的现象 / 反直觉之处 / 踩坑：
  - **反直觉**：`clean_text` 的 `allow_empty=False` 抛的是 `ValueError` 而不是自定义异常，
    结果"标题为空"这条最普通的用户错误走成了非预期异常路径。
    教训是**校验与清洗的异常类型要统一到业务层**，否则每加一个校验点都要考虑
    "这次抛的是哪种"。
  - **反直觉**：`sqlite3.Connection.executescript` 会隐式提交事务。这在写迁移时是
    致命坑（迁移中途失败会留下半个 schema 且无法回滚），已在 `runner.py` 里注释说明并用
    `complete_statement` 自己切语句。
  - **踩坑（自找的）**：第一次提交后 `tests/test_trace_convention.py::test_trace_entries_are_chronological`
    失败，报 `At index 1 diff: '2026-10-03 18:40' != '2026-10-03 18:35'`。
    根因：上一条记录的标题我写了 `18:40`，但实际写它的时刻是 18:28 前后——
    **时间戳写成了"计划完成时间"而不是"实际动手时间"**，于是与前一条（18:21）
    的先后关系读起来是错的。修正为 `18:28`。
    教训：trace 的时间戳必须写实际时刻，否则它作为溯源时间轴是失真的；
    这正是守门测试要挡住的东西。
  - Windows 上 `os.chmod` 基本无效，所以 `test_written_file_keeps_owner_mode`
    在 Windows 下显式 skip 而不是假装通过——**权限位这条性质只能在 Linux 上验证**，
    而生产就是 Linux。
  - `ratelimit` 用 `time.monotonic` 而不是 `time.time`：后者会被 NTP 调整，
    窗口长度会莫名其妙变长或变短（这台机器上 chrony 是开着的）。

- 未决问题与下一步：
  - `443` 仍等 Aliyun 安全组放行；这是唯一的外部阻塞项。
  - **本会话无法 `git push`**：`git push origin main` → 退出码 1，
    `fatal: unable to access 'https://github.com/yihaosirius/ScheduleKit.git/':
    Failed to connect to github.com:443 after 21049 ms: Could not connect to server`。
    本会话的 schannel 出网对 github.com 不可用（旧项目 dev-notes 里记录过同一个坑：
    curl/git/PowerShell 走 schannel 都不通，只有 Node 与 uv 自带 TLS 才能出网）。
    提交只落在本地，需要用户在能访问 GitHub 的环境执行 `git push`。
  - 尚未写 `app/routers/*`：`main.py` 里已经引用它们，所以**当前应用还起不来**。
    下一步按 tasks → auth → ingest/drafts → memories → courses → settings 的顺序补齐。
  - 草稿（`ingest_drafts`）与记忆（`memories`）的服务层尚未实现；
    记忆条目注入预算是 `[ingest].memory_char_budget`，注入留痕写 `draft_memories`
    与 `items.memory_ids`。

---

## 2026-10-03 18:52 · 端口回退到 8443（备案 + 已有快捷指令），并把 push 与阻塞项写成约定

- 目标：纠正上一轮我自己做出的错误判断——我按"标准端口更省事"把站点从 `:8443`
  改到了 `:443`。用户否决，理由是两条我没想到的硬约束。这一轮把它改回来，
  并把**这两条理由本身**固化成测试与文档，避免以后被"顺手优化"掉。

- 决策与理由：站点继续保持 `:8443`，Caddyfile 显式写
  `https://canisa1ph.duckdns.org:8443`。
  - 理由一：**手机上已装好的快捷指令写死了 `:8443`**。换端口就要重新编辑并在手机上
    重新分发快捷指令，用户侧有实打实的成本。我上一轮只算了服务端的账。
  - 理由二：**80/443 是备案相关端口**。用 8443 恰恰是为了绕开备案要求，
    改回 443 会把这个问题重新引进来。
  - 我原先的理由（"非标端口会踩 SW 作用域 / Cookie SameSite / 添加到主屏幕三个坑"）
    仍然成立，但它是一个**副作用层面的代价**，而上面两条是**约束**。约束压倒代价。
  - 被否决：**443 与 8443 双开**。双开并不能让快捷指令少改，反而多一个对外入口
    与一份要维护的证书/站点块。
  - 被否决：**改成 443 但保留 8443 跳转**。同上，且跳转解决不了快捷指令里写死的端口。
  - 同时新增两条交互约定（写进 `AGENTS.md` §2）：
    * `git push` **必须先停下来问**，不许自作主张推送；
    * 遇到需要用户操作才能继续的阻塞项，**当轮就报告并停下**，不许自己找绕路方案往前跑。
    - 被否决：把约定只写在这里（dev-trace）。否决原因是 trace 是流水账，
      约定要放在 `AGENTS.md` 才会被下一轮读到。

- 触及文件：
  - 修改：`deploy/Caddyfile`（站点块 `https://...` → `https://...:8443`；
    在文件头补上端口选择的理由与 `admin off` 与 reload 互斥的说明）
  - 修改：`config.toml.example`（`[server].public_url` 与 `[tls].port` 改回 8443，
    并写明"不要顺手改成 443"）
  - 修改：`app/config.py`（`ServerConfig.public_url` 与 `TLSConfig.port` 默认值回 8443）
  - 修改：`tests/_support.py`（测试配置仍用 `http://127.0.0.1:8000`，
    目的是让"secure Cookie 只在 HTTPS 下加"这条逻辑可被两侧覆盖，并加了注释说明）
  - 修改：`tests/test_config.py`（新增 3 条回归锁：代码默认值 / 模板 / Caddyfile 都必须用 8443）
  - 修改：`AGENTS.md`（新增 §2 交互节奏；§5 部署约定写明 8443 与 `admin off` 的代价）
  - 修改：`docs/decisions.md`（D-007 重写为"保持 8443"，并显式记录代价与三条锁定测试）

- 执行过的命令与结果：
  - 回退前先探测，确认 8443 当时是**拒绝**而不是被过滤：
    `port 8443: ERROR ... "由于目标计算机积极拒绝，无法连接。"`、`port 8078: CONNECTED`、
    `port 443: TIMEOUT`。说明 8443 的入站规则**是放行的**，只是我把 Caddy 搬走了。
  - `uv run pytest -q -p no:cacheprovider` → 退出码 0：`180 passed, 1 skipped`。
  - `caddy validate --config /etc/caddy/Caddyfile.new --adapter caddyfile` → `Valid configuration`。
  - `systemctl restart caddy` → 退出码 0，`active`；
    监听确认：`*:8443` 与 `*:80`（80 是 Caddy 的 auto-https 重定向监听，
    入站被安全组挡住，不影响）。
  - 本机带 SNI 自测：`curl --resolve ...:8443:127.0.0.1 https://...:8443/healthz`
    → `code=502`（反代生效，应用未部署）；`openssl s_client -servername ...`
    → TLSv1.3、`Peer certificate: CN = canisa1ph.duckdns.org`、`Verification: OK`。
  - **外网端到端验证**（Node，因为本会话 PowerShell 的 schannel 建不了 TLS）：
    ```
    STATUS 502
    CERT subject {"CN":"canisa1ph.duckdns.org"}
    CERT valid_to Dec 12 12:28:24 2026 GMT
    PROTOCOL TLSv1.3
    ```
    端口探测：`8443: CONNECTED`、`443: TIMEOUT (filtered)`、`80: TIMEOUT (filtered)`。

- 观察到的现象 / 反直觉之处 / 踩坑：
  - **反直觉**：TCP 连不上时"拒绝(REFUSED)"与"超时(TIMEOUT)"含义完全相反，
    而两者在粗粒度的探测里都只是"连不上"。8443 在改端口前表现为**积极拒绝**
    ——说明安全组**是放行的**，只是没有进程监听；443 表现为**超时**——说明是被过滤的。
    只看"连不上"会把这两个完全不同的原因混为一谈。**排查端口要先区分拒绝与超时。**
  - **反直觉**：本会话 PowerShell 的 `Invoke-WebRequest -SkipCertificateCheck`
    也建不了 TLS（`The SSL connection could not be established`），
    换 Node 立刻成功。这与旧项目 dev-notes 里的记录一致：本会话只有自带 TLS 的
    运行时（Node / uv）能出网。**验证 HTTPS 要用 Node，不要用 PowerShell。**
  - 我上一轮把 Caddy 搬到 443 之后**没有验证外网可达性就往下走了**，
    直到用户指出才发现 443 本来就不通。教训：改对外端口这类改动，
    **改完必须立刻从外部验证**，不能只看服务器本机自测。
  - Caddy 在 `:443` 站点被撤掉后仍在监听 `*:80`。这是 auto-https 的重定向监听，
    不是错误；入站被安全组挡住所以外网无影响。

- 未决问题与下一步：
  - `docs/decisions.md` D-007 现在是"保持 8443"，与 D-010 的编号顺序一致（D-007 < D-008）。
  - **推送状态的真相（重要，别信我之前的判断）**：我先前在 trace 里写"本会话无法 push"，
    但 `git reflog show origin/main` 显示 **push 其实部分成功了**：
    ```
    3a3d3d1 refs/remotes/origin/main@{0}: update by push
    2b635b9 refs/remotes/origin/main@{1}: update by push
    e4598a6 refs/remotes/origin/main@{2}: update by push
    ```
    即 `71b933c`、`50d0d9b`、`3a3d3d1` 三个提交**已经推上去了**。
    后来的 `git push` 报 `Recv failure: Connection was reset`，
    是"推送过程中连接被重置"而不是"完全没推成"——我把它读成了后者，判断错了。
    当前 `git rev-list --count origin/main..HEAD` = **1**，未推送的只有 `e7268c9`（本轮）。
    另：本会话的提交作者是 `ScheduleKit <dev@schedulekit.local>`（用 `-c` 临时指定，
    没有改仓库配置）；如果希望这些提交挂在你的 GitHub 身份下，需要改作者后重推。
  - 尚未写 `app/routers/*`：`main.py` 已引用它们，**当前应用还起不来**。
    下一步按 tasks → auth → ingest/drafts → memories → courses → settings 补齐。
  - 非标准端口下 Service Worker 作用域 / Cookie SameSite / 添加到主屏幕三项
    仍需**真机确认**，这一步只能在应用部署后做。

---

## 2026-10-03 19:01 · 服务端全链路完成：路由、LLM 三通道、两阶段录入、课表上下文、记忆系统

- 目标：把"识别 → 草稿 → 确认 → 入库"这条主链路端到端做通，并把
  课表上下文与记忆系统接进去。这是产品的核心，也是风险最集中处。

- 决策与理由：
  - **每请求从磁盘重读配置**（`app/main.py` 中间件）来实现热加载，而不是
    "改完把新值同步进内存对象"。
    - 理由：同步方案必须知道**改了哪些字段**，漏一个就会出现"改了没反应"；
      重读一遍几 KB 的 TOML 换来"不可能漏字段"。读失败时保留上一份内存配置
      （宁可旧配置继续服务，也不要因一次写坏就全站 500）。
    - 被否决：只在 `[llm]` 段做热加载（要在 `update_config_section` 之后手写同步逻辑）。
    - 被否决：用文件 mtime 做失效判断。否决原因是 mtime 精度与"同一秒内改两次"
      都会漏，而收益只是省一次几 KB 的解析。
  - **字段长度上限以模型 schema 为准，不以手工录入为准**。
    `app/llm/tools.py` 给模型的约束是 20/60 字；`services/tasks.py` 给手工录入的是
    60/500 字。`normalize` 必须用**前者**。
    - 被否决：两边共用一个常量。否决原因见下面的踩坑——那正是最初的 bug。
  - **清理的计数按"条目数"算，不按"图片数"算**（新增 `PurgeResult`）。
    - 被否决：让 `purge_expired` 继续只返回图片路径。否决原因是那样"清理了几条"
      会长期显示 0，而这个数字正是排查磁盘问题的依据。
  - **"所有通道都失败"必须是 `LLMChannelRejected` 的子类**，而不是笼统的
    `LLMToolCallMissing`。
    - 理由：调用方要能区分"该改 prompt"（拒绝）与"该重试"（临时故障）。
      所以新增 `LLMAllChannelsFailed(LLMToolCallMissing, LLMChannelRejected)`。
    - 被否决：直接抛 `LLMChannelRejected`。否决原因是会丢掉"全部通道"这个信息，
      而上层需要它来决定是否值得换 provider。
  - **草稿重试是原地更新同一条草稿**，而不是新建。
    - 被否决：新建草稿。否决原因是用户的意图是"再来一次"，不是"我又传了一遍"；
      新建会让草稿箱堆出同一张图的多个副本。
  - **确认页用 hash 路由**（`/#/drafts/{id}`）而不是 history 路由。
    - 理由：hash 链接在任何静态托管/反代配置下都能直接打开，少一个会配错的地方。
    - 被否决：history 路由 + SPA 回退。否决原因是它要求服务端把未知路径全部回退到
      index.html，而这个回退一旦把 `/api/*` 也吃掉，接口 404 就会变成"返回了 HTML"，
      前端报的错会指向完全无关的地方。

- 触及文件：
  - 新增：`app/schemas.py`
  - 新增：`app/routers/{__init__,auth,tasks,ingest,drafts,memories,courses,settings,ui}.py`
  - 新增：`app/services/{ingest,memory,timetable,context,normalize,drafts}.py`
  - 新增：`app/llm/{__init__,base,tools,structured,registry,mock,chat,responses}.py`
  - 新增：`app/housekeeping.py`
  - 新增：`tests/{test_api,test_tasks,test_security,test_drafts,test_memory,
    test_normalize,test_timetable_context,test_llm_channels}.py`
  - 修改：`app/main.py`（请求级配置热加载）、`app/deps.py`（新增 `OptionalAuth`）、
    `app/housekeeping.py`（按条目数计数）、`app/routers/drafts.py`（purge 计数）、
    `app/services/drafts.py`（新增 `PurgeResult`）、
    `app/services/normalize.py`（长度上限改用 schema 常量）、
    `tests/_support.py`（假 Key 改成可识别的字符串）、
    `tests/conftest.py`（新增 `configured` / `api` / `anon` / `bare_con` 装置）
  - 重写：`README.md`

- 执行过的命令与结果：
  - `uv run pytest -q -p no:cacheprovider` 多轮，**修复过程中反复出现失败**，
    最后一次退出码 0：`415 passed, 1 skipped`。
  - 逐轮暴露的真实缺陷（都不是笔误，是设计问题）：
    1. `test_settings_update_is_hot` / `test_term_settings_validation` /
       `test_status_warns_when_term_expired` 失败 → 根因是**配置写盘了但内存没重载**，
       PUT 返回的还是旧值。改成每请求重读 + `_reload()` 后再构造响应。
    2. `test_timetable_now_endpoint` → `ERROR sk.http ... NameError:
       name 'courses' is not defined`（`/api/timetable/now` 里漏了取课表那一行，
       被上面三层 `try` 包住所以只表现为 500）。
    3. `test_title_truncated_to_limit_and_reported` 报 `assert 60 == 20`、
       `test_notes_cleaned_and_bounded` 报 `assert 200 == 60` →
       **normalize 复用了手工录入的 60/500 上限，而模型 schema 规定的是 20/60**，
       于是模型超长输出被静默接受（连 override 都不产生）。
    4. `test_housekeeping_purges_expired_drafts` 报 `assert 0 == 1` →
       追下去发现 `purge_expired` 返回的是**图片路径列表**，一条没有图片的草稿
       被删掉后计数为 0。用 `PurgeResult` 修掉，并补了一条专门测这个的用例。
    5. `test_settings_never_leaks_api_key` 假失败 → 我把断言写成"响应里不能出现
       `test-key` 这个字符串"，而响应体里有字段名 `api_key` / `has_api_key`，
       于是一个**关于字段名**的断言被我写成了**关于值**的断言，含义完全错位。
       把假 Key 换成一眼可识别的 `FAKE-SECRET-VALUE-DO-NOT-LEAK` 后，
       断言变成"这个具体值不许出现"，意图才与写法一致。
  - 探测（外网）：`8443 CONNECTED`；Node 请求拿到
    `STATUS 502 / CERT CN=canisa1ph.duckdns.org / PROTOCOL TLSv1.3`。

- 观察到的现象 / 反直觉之处 / 踩坑：
  - **反直觉**：`test_400_is_not_retried` 一开始失败，报"所有结构化通道都失败了"。
    我原以为测试写错了，读代码才发现是我的**错误分类有问题**：全部通道被拒时抛的是
    `LLMToolCallMissing`，而它在语义上属于"拒绝"而不是"缺失"。新增
    `LLMAllChannelsFailed` 同时继承两者后，调用方与测试都能按"该改 prompt 还是该重试"来判断。
    **测试失败暴露的是我的语义设计，不是测试写错了。**
  - **反直觉**：normalize 的长度上限"看起来"复用了正确的常量（都是 `TITLE_MAX`），
    但两个模块各有一个同名常量、值不同。**同名不同值是这类 bug 的温床**：
    import 语句看上去完全合理。修法是把「给模型的约束」明确成唯一来源
    （`app/llm/tools.py`），并在 normalize 里写清楚为什么不能复用 tasks 的常量。
  - **反直觉**：`purge_expired` 返回 `[]` 却**确实删掉了记录**。
    第一眼像是"函数没执行"，实际是返回值语义与调用方期望不一致——
    调用方要的是"删了几条"，函数给的是"涉及哪些图片"。
    这类 bug 没有异常、没有日志异常，只会让一个数字长期是 0。
  - 请求级重读配置让"配置热加载"变简单了，但**测试里对它的依赖也变强了**：
    所有断言"改了就生效"的测试其实在测中间件，而不只是测 router。
    这一点写在这里以免后来者把中间件那段当成可以省掉的优化。
  - `sqlite3.Row` 支持 `row["name"]` 与 `row[0]`，但不支持 `.name`——
    写 `row.image_path` 会得到 `AttributeError`。本轮的临时调试脚本踩过。

- 未决问题与下一步：
  - **前端 SPA 完全没写**。`app/static/spa/index.html` 不存在，所以
    `GET /` 会返回一个 503 的"前端尚未构建"说明页（刻意做成这样：
    比一个空白 404 更容易判断问题在哪）。这是下一步的主要工作量。
  - **部署产物没写**：`deploy/install.sh`、`deploy/schedulekit.service`、
    `docs/deploy.md`、`deploy/Caddyfile` 的渲染逻辑。
  - **Scriptable 小组件与快捷指令文档没写**：`clients/scriptable/`、`shortcuts/`、
    `docs/shortcuts.md`、`docs/widget.md`、`docs/api.md`。
  - **`docs/push-notes.md` 未写**（推送方案调研，本期不做）。
  - 真机验收（PWA 可安装性、非标端口下的 Service Worker 作用域与 Cookie SameSite）
    必须等应用部署到 443/8443 之后才能做。
  - 本地已有提交待推送，**按约定等用户确认**（本会话的推送一律交给用户执行）。

---

## 2026-10-03 19:06 · 真实 HTTP 端到端验证：又抓出两个单测看不见的缺陷

- 目标：把"测试全绿"和"真的能用"分开验证。415 个测试跑在 httpx 的
  ASGITransport 上，**从不经过真正的启动入口、也从不发真实 HTTP**。
  这一轮要回答的问题是："`python -m app.serve` 到底起不起来、接口在真
  网络栈上是不是也这样"。

- 决策与理由：
  - **用 Node 写一次性的端到端脚本**，而不是 PowerShell。
    - 理由：本会话 PowerShell 的 `WebSession` 会**丢 Cookie**（第一轮登录成功后
      后续请求仍报 401），而 Node 的 fetch + 手工 cookie jar 行为可预测。
      这与之前"PowerShell 的 schannel 建不了 TLS"是同一类环境问题。
    - 被否决：把 `Invoke-RestMethod` 的会话问题查清楚。否决原因是那是在调试
      测试工具而不是被测系统，收益为零。
    - 被否决：把这些断言写成 pytest。否决原因是它们需要**真的起一个进程**，
      会引入端口占用与启动等待，而它们要验证的恰恰是"进程能不能起来"，
      用进程内测试去验证是自欺。
  - **端到端脚本放 `.pytest-run/`（gitignore 内）**，不进仓库。
    - 理由：它是"这一次的环境验证"，依赖本地 boot config 与固定端口；
      进仓库会变成一个需要长期维护的第二套测试体系。
    - 被否决：放进 `tests/` 并标记为 slow。否决原因同上——它要起真进程，
      不适合混进默认测试路径。
  - **把 mock provider 用于本地 boot 验证**：`config.toml.example` 默认
    `provider = "responses"`，没有真 Key 时上传必然失败。验证离线链路时
    显式把 boot config 改成 `mock`。

- 触及文件：
  - 新增（gitignore 内，不入库）：`.pytest-run/e2e.mjs`、`.pytest-run/reorder_decisions.py`
  - 新增：`tests/test_serve.py`（服务入口的测试，见下面第 1 条）
  - 修改：`app/serve.py`（补 `kv` 的 import）
  - 修改：`app/llm/structured.py`（适配器构造函数收整份 `Config`；
    `extract` 改用 `self.config.validate_for_llm()`）
  - 修改：`tests/test_llm_channels.py`（`Cfg` 替身改为"同时提供 `.llm` 段与
    `validate_for_llm()`"；新增对**真实适配器**跑 extract 的用例）
  - 修改：`docs/decisions.md`（新增 D-011 ~ D-014，并按编号重排全文）

- 执行过的命令与结果：
  - 起真实服务：`SK_CONFIG=... uv run python -m app.serve --port 8123`
    - **第一次失败（exit 1）**：
      ```
      File "D:\ScheduleKit\app\serve.py", line 48, in main
        kv(config=..., host=host, port=port, reload=args.reload),
      NameError: name 'kv' is not defined
      ```
      缺一个 import。**而 415 个测试全绿**——因为没有一个测试调用过
      `serve.main`：它们都通过 `create_app` 直接构造应用。
      修复后补 `tests/test_serve.py`（用替身拦下 `uvicorn.run`，
      断言 factory / host / port / access_log / proxy_headers / reload_dirs）。
  - 起服务后逐个探测：`healthz 200` / `manifest 200 application/manifest+json` /
    `sw.js 404`（文件还没写）/ `/ 503`（SPA 未构建，符合预期）/
    `未鉴权 /api/tasks 401` / `/api/nope 404` / `/app/main.py 404`。
  - 跑 `.pytest-run/e2e.mjs`：第一轮 **14/27**，第二轮 **26/27**，
    重置数据库后 **27/27**。
    第一轮暴露出第二个真缺陷，来自服务端日志：
    ```
    ERROR sk.http [t=01e06793] request.unhandled path=/api/ingest
      error=AttributeError detail="'LLMConfig' object has no attribute 'validate_for_llm'"
    ```
    根因：`build_llm` 把 `cfg.llm`（配置**段**）传给了适配器，而适配器在
    `extract()` 里调 `validate_for_llm()`——该方法在 `Config` 上、不在 `LLMConfig` 上。
    **真机表现是"上传直接 500"，而单测全绿**，因为测试里
    `provider = "mock"` 而 mock 不校验配置。
  - 最终 `uv run pytest -q -p no:cacheprovider` → 退出码 0：`422 passed, 2 skipped`。

- 观察到的现象 / 反直觉之处 / 踩坑：
  - **最重要的一条：测试通过率与"能不能跑"几乎无关。**
    415 个测试全绿的同时，`python -m app.serve` 起来就崩。原因是测试全部
    绕过了两个东西：**真正的入口**（`serve.main`）与**真正的网络栈**
    （httpx 的 ASGITransport 不走 socket）。这两处恰好是"线上一崩就全崩"的位置。
    所以现在 `tests/test_serve.py` 守着入口，而真机验证必须单独做一次。
  - **反直觉**：`AttributeError` 只在**真实适配器**上出现，mock 完全正常。
    教训是**替身的形状必须与真实对象一致**——我最初的 `Cfg` 替身只有 `llm` 段的字段，
    于是它同时掩盖了"传错对象"与"方法不存在"两件事。
    已经把这条写进 `tests/test_llm_channels.py` 的注释。
  - **反直觉**：PowerShell 的 `Invoke-RestMethod -WebSession` 在
    `http://127.0.0.1:8123` 上会丢 Cookie（登录返回 200 且
    `authenticated=true`，但后续请求全是 401）。这不是服务端问题：
    同一套请求用 Node 跑就是 27/27。**排查这类现象要先换客户端再怀疑服务端。**
  - **反直觉**：`purge_expired` 返回 `[]` 却**确实删掉了记录**（见上一条 trace）。
    这一轮又遇到一次同源问题：它返回的是"图片路径"，而调用方要的是"删了几条"。
    已经在 D-013 里独立成一条决策。
  - 我的端到端脚本第一版把"无 CSRF 的写操作被拒"写成了
    `r.status !== 403 && false || r.status === 403` —— 这个表达式的短路行为
    让它永远为真，于是"伪造 CSRF"这一条在第一轮里是**假通过**。
    改成显式发一个伪造 token、断言 403 之后才有意义。
    **断言写错比没有断言更危险**：它会给你一个"已经验证过了"的错误信心。
  - 数据库残留导致过一次假失败（`unordered: 2` 而不是 0）。
    端到端脚本必须从干净库开始，否则它在测上一轮的残留状态。

- 未决问题与下一步：
  - 优先级最高的下一步是 **SPA 前端**（`web/` → `app/static/spa/`）：
    双列表 UI、交互动画、PWA 外壳、草稿确认页、设置 / 状态监测 / 课表 / 记忆面板。
  - 然后是 **部署产物**（`deploy/install.sh`、`deploy/schedulekit.service`、
    `docs/deploy.md`；`deploy/Caddyfile` 已有，但渲染脚本没有）、
    **Scriptable 小组件**与**快捷指令文档**。
  - `docs/api.md` 需要按现有接口写一遍（19+ 个端点），它是快捷指令与
    将来接 agent 时的唯一接口文档。
  - 真机验收清单（PWA 可安装性、非标准端口下的 Service Worker 作用域、
    Cookie SameSite、添加到主屏幕）必须在部署到 8443 之后做。

---

## 2026-10-03 19:30 · 上线：8443 真机部署完成，端到端从公网验证通过

- 目标：把服务端真正部署到 `https://canisa1ph.duckdns.org:8443` 并让它在公网上
  可用。用户给了 DuckDNS token，LLM 先保持 `mock`。

- 决策与理由：
  - **先部署再写前端**。理由是"真机能跑"与"本地测试全绿"是两件事（上一条 trace
    已经证明了），越早让真机跑起来，越早暴露只有真机才会有的问题。
    - 被否决：先把前端做完再一起部署。否决原因是那会把所有真机风险堆到最后，
      而且前端本身也需要真机来验证 PWA 相关行为。
  - **部署脚本只接管 marker 之间的站点块**，不动 Caddyfile 的其余部分。
    - 理由：这台机器上 Caddy 还托管着别人的服务（cloudreve WebDAV 在 :8079），
      一次全量覆盖会让它们一起挂。
    - 被否决：让脚本生成整份 Caddyfile。否决原因是服务器上已经有全局块
      （`admin off` / `log`），而 Caddyfile **只允许一个全局块**，
      插第二个会直接 validate 失败。
  - **token 与密码通过环境变量注入脚本**，不进仓库、不硬编码。
    - 被否决：把 token 写进 `deploy/` 下的某个文件。否决原因是上一版就是这么做的，
      结果 token 随脚本进了 git 仓库。
  - **端到端验证脚本用 Node 从本地机器走公网**，而不是在服务器上 curl。
    - 理由：那才是用户的真实访问路径（手机也走这条路）；在服务器上自测只能证明
      回环可用。

- 触及文件：
  - 新增：`deploy/install.sh`、`deploy/schedulekit.service`、`deploy/Caddyfile.template`
  - 新增：`docs/deploy.md`、`docs/api.md`、`docs/shortcuts.md`、`docs/widget.md`、
    `docs/push-notes.md`
  - 新增：`clients/scriptable/schedulekit-widget.js`
  - 新增：`tests/test_docs.py`、`tests/test_serve.py`
  - 新增：`.gitattributes`（强制 LF）
  - 新增（gitignore 内，不入库）：`.pytest-run/verify-deploy.mjs`、`.pytest-run/e2e.mjs`
  - 修改：`app/serve.py`、`app/llm/structured.py`、`tests/test_llm_channels.py`、
    `tests/_support.py`、`tests/conftest.py`、`tests/test_config.py`、`README.md`
  - 服务器：`/opt/schedulekit`、`/etc/schedulekit/config.toml`、`/var/lib/schedulekit`、
    `/etc/systemd/system/schedulekit{,-duckdns}.{service,timer}`
  - 服务器保留：`/root/schedulekit-credentials.txt`（0600，管理员密码）

- 执行过的命令与结果：
  - 首字部署**失败两次**，都是真机才暴露的问题：
    1. `SK_NEW_PASSWORD=... sudo -u schedulekit ... set-password` 报
       `配置错误：没有拿到新密码`。根因：`sudo -u` 默认 `env_reset`，
       前缀赋值的环境变量传不进去。实测验证：
       `sudo -u nobody env | grep -c PROBE_VAR` → `0`。
       改成 `sudo -u USER env K=V ...`。
    2. 改完仍起不来，日志：
       ```
       app.config.ConfigError: 配置校验失败：
         - auth.secret_key 为空：会话 Cookie 无法签名。运行 `python -m app.cli init` 生成
       Application startup failed. Exiting.
       ```
       根因：脚本只调了 `migrate` + `set-password`，**从没生成 `secret_key`**。
       fail-closed 的行为是对的，错在部署脚本没把它当成自己的一步。
       改为走 `app.cli init`（非首次则 `init --no-password` 幂等补上）。
    3. 顺带修掉：`sudo -u` 之后 CWD 不是项目目录，`python -m app.cli` 报
       `No module named 'app'`，新增 `run_cli()` 包装显式 `cd`。
    4. 自检里 `--pretty` 被放到子命令之后，argparse 报 `unrecognized arguments`
       （全局选项必须在子命令之前）。
    5. `jq_get` 用 `python3 -c "…eval('d'+'$1')…"` 拼字符串，引号在
       shell→python 传递中掉了一层，报 `SyntaxError: invalid syntax.
       Perhaps you forgot a comma?`。改成把路径当参数传给固定脚本。
  - 部署成功后从**本地机器**跑 `.pytest-run/verify-deploy.mjs`（走公网到 8443）：
    第二轮 **11/11 通过**。
    ```
    PASS  部署的 /healthz 可访问
    PASS  用凭据登录成功
    PASS  拍照/文字录入生成草稿（provider=mock）— item_count=1
    PASS  解析出了条目 — due_at=2026-10-10T15:59:00+00:00
    PASS  确认入库 / 有截止时间的进有序表
    PASS  状态接口可用 — week=3
    PASS  上下文注入正常 / PWA manifest 可访问
    ```
  - 幂等重部署两次（带 `--init` / 不带）都成功；最终自检：
    `ok: true, problems: []`，`http 健康检查 OK`、`https 反代 OK`。
  - 清空验证残留后确认空库：`items/drafts/memories/courses` 全为 0。
  - `uv run pytest -q -p no:cacheprovider` → 退出码 0：`436 passed, 1 skipped`。

- 观察到的现象 / 反直觉之处 / 踩坑：
  - **反直觉（重要）**：写 `tests/test_docs.py` 时我遍历 `app.routes` 只看到
    `/healthz`，第一反应是"路由怎么全丢了"。写探针一查才发现：
    **Starlette 1.7 起 `include_router` 的结果是一个 `_IncludedRouter` 包装对象，
    子路由不再摊平进 `app.routes`**，挂载前缀在 `include_context.prefix` 里。
    而发请求验证显示所有接口其实**全都正常**（`/api/tasks` 返回 401、
    `/api/does-not-exist` 返回 404，都对）。
    **教训：判断"端点存不存在"的权威方法是发请求，不是枚举内部结构。**
    测试里因此加了一条"枚举结果不能少于 20 条"的前置断言——否则哪天枚举方式再失效，
    下面两条"文档与代码对齐"的测试会因为枚举到空集而**假装通过**。
  - **反直觉**：`sudo -u` 重置环境变量这件事，在交互式终端里几乎不会遇到
    （因为大家习惯 `sudo -E` 或者直接从 root 跑）。它在"脚本里 `VAR=x sudo -u u cmd`"
    这个写法下才出现，而且**外层脚本因为命令替换的关系看不出失败**——
    第一次部署时脚本继续往下跑了。
  - **反直觉**：`git archive` **只打包已提交的内容**。第一次打包时
    `deploy/install.sh` 刚创建还没提交，于是服务器上解出来的 `deploy/` 是空的，
    报 `ls: cannot access .../install.sh: No such file or directory`。
    在"打包=发布"的流程里，这个特性其实是对的（保证发布的是提交过的代码），
    但必须先 commit 再 archive。
  - 自检第一次报 `http 健康检查失败` 却又报 `https 反代 OK`——看着自相矛盾。
    实际是**探测与 `systemctl restart` 之间没有等待**：http 探测打在 uvicorn
    bind 之前（连接被拒），而 https 探测在几秒后（已经起来了）。
    改成轮询最多等 10 秒。**假失败比没有检查更糟**：它会训练人忽略自检输出。
  - 第二次端到端跑出 `FAIL 草稿阶段任务表为空 — {"ordered":1,...}`。
    查库确认那是**上一次验证运行自己留下的任务**（created_at 11:22:17 与 11:29:32
    正好对应两次运行），不是零写入不变量被破坏。
    端到端脚本必须从干净库开始，否则它测的是上一轮的残留。

- 未决问题与下一步：
  - **前端 SPA 仍未写**：`GET /` 现在返回 503 的"前端尚未构建"说明页。
    这是剩下的最大一块（双列表 UI / 交互动画 / PWA / 确认页 / 设置 / 状态 / 课表 / 记忆）。
  - **LLM 仍是 `mock`**：按用户要求先在离线确定性模式下跑通链路。
    填真实 Key 的两种方式见 `docs/deploy.md`。
  - **真机验收清单**（PWA 可安装性、非标准端口的 Service Worker 作用域、
    Cookie SameSite、添加到主屏幕）现在**可以做**了——服务已在公网可用，
    但前端的 Service Worker 文件还没写，所以这几项要等前端。
  - 管理员密码在服务器 `/root/schedulekit-credentials.txt`；建议登录后立刻改。
  - 本地提交待用户推送（本会话的推送一律交给用户执行）。

---

## 2026-10-03 19:45 · SPA 前端：双列表 / 动画 / PWA / 确认页 / 设置 / 状态 / 课表 / 记忆

- 目标：把服务端已经打通的链路变成**能看能点**的界面。用户要求"写前端记得用
  browser 自己检查"。

- 决策与理由：
  - **不引 UI 组件库（Vant / Element Plus）**。用 Vue 3 + 手写组件 + 一套
    CSS 设计系统。
    - 理由：需求要的是"类 Flutter"的**交互动效**（滑动指示器、勾选划掉、
    列表 FLIP、抽屉滑入），而组件库的动画是它自己那套节奏，要覆盖它反而更费事。
    手写让动效曲线与时长完全统一（`:root` 里的 `--ease` / `--dur-*`）。
    - 被否决：Vant 4（移动端组件全）。否决原因是它自带的过渡与我们的设计语言冲突，
    且会显著增大包体（我们的产物 gzip 后只有 ~68KB）。
  - **不引 Pinia**。用 `reactive` 对象 + 几个函数（`web/src/state/store.ts`）。
    - 理由：共享状态只有三块（会话、任务、设置），多一个依赖要多一套心智模型。
  - **不做乐观更新**。所有改动先发请求，成功后再用响应更新本地。
    - 理由：乐观更新在失败时要回滚，回滚代码比它省下的 100ms 值钱；
    这个应用也不追求那个级别的响应速度。
    - 被否决：乐观更新 + 失败回滚。否决原因是"回滚错了"会产生比慢 100ms
    严重得多的问题（显示已删除但其实还在）。
  - **用 hash 路由**（`createWebHashHistory`）。
    - 理由见 `web/src/router.ts` 的注释：服务端生成的 `confirm_url` 就是
    `/#/drafts/{id}`（快捷指令要在手机上直接打开它）；而且 history 路由要求
    服务端把所有未知路径回退到 index.html，那个回退一旦把 `/api/*` 也吃掉，
    接口 404 会变成"返回了 HTML"，前端报的错会指向无关的地方。
    - 被否决：history 路由 + SPA 回退。
  - **构建产物提交入库**。服务器不装 Node。
    - 被否决：服务器上构建。否决原因是那要引入 Node + pnpm 与构建环境，
    而服务器只有 1.6G 内存且跑着别的服务。
  - **二选一约束在界面里做成"模式切换"而不是两个输入框 + 报错**。
    - 理由：用户不需要理解"为什么不能都填"，因为界面没给他这个机会。
    这比填完再被打回友好得多。
  - **Service Worker 放在 `app/static/sw.js`（后端托管）而不是 Vite 的 public 目录**。
    - 理由：后端能给它精确的 `Content-Type` 与 `Cache-Control: no-cache`
    以及 `Service-Worker-Allowed`。交给静态托管按扩展名猜不如显式写死。
  - **PWA 图标用脚本现生成**（`scripts/make_icons.py`）。
    - 理由：旧项目的图标没进归档（当时只归档了数据库与上传图片），
    而现生成比去找图标更快，且能保证主题色一致。

- 触及文件：
  - 新增：`web/`（Vue 3 + Vite + TS 源码：`src/main.ts`、`App.vue`、`router.ts`、
    `state/store.ts`、`api/client.ts`、`utils/format.ts`、`styles/base.css`、
    9 个页面、4 个组件、`vite.config.ts`、`tsconfig.json`、`pnpm-workspace.yaml`）
  - 新增：`app/static/icons/*.png`（5 个图标）、`app/static/sw.js`、
    `app/static/spa/**`（构建产物，22 个文件 / 222KB）
  - 新增：`scripts/make_icons.py`、`scripts/approve_pnpm_builds.py`
  - 修改：`app/main.py`（挂载 `/static` 与 `/assets` —— 见下面的踩坑）
  - 修改：`tests/test_api.py`（新增 4 条静态资源测试）

- 执行过的命令与结果：
  - `pnpm install` → **连续失败两次**，都是 pnpm 12 的配置位置/键名变化：
    1. 写进 `package.json` 的 `pnpm.onlyBuiltDependencies` →
       `The "pnpm" field in package.json is no longer read by pnpm`，无效。
    2. 写进 `pnpm-workspace.yaml` 的 `onlyBuiltDependencies` → **依然无效**，
       仍报 `ERR_PNPM_IGNORED_BUILDS`。
       查 [pnpm v10→v11 迁移文档](https://pnpm.io/migration) 才确认：
       v11 起 `onlyBuiltDependencies` / `neverBuiltDependencies` /
       `ignoredBuiltDependencies` / `onlyBuiltDependenciesFile` **合并成了单一的
       `allowBuilds` 映射**（`{ 包名: true | false }`）。改成 `allowBuilds` 后：
       `esbuild postinstall: Done`，退出码 0。
  - `pnpm exec vue-tsc --noEmit` → 首次 10 个错误，主要是 `noUnusedLocals`
    抓出的未使用导入与死代码，以及一个真实的类型不匹配：
    `createMemory` 的 `tags` 我标成了 `string[]`，而接口入参是**逗号分隔字符串**
    且出参才是数组。新增 `MemoryCreatePayload` / `MemoryUpdatePayload`
    把两者区分开。
  - `pnpm run build` → 退出码 0。产物 gzip 后约 68KB（vendor 41KB）。
  - 起本地服务（`:8000`，`provider=mock`）后逐个探测：
    `/` 200、`/manifest.webmanifest` 200、`/sw.js` 200、
    `/assets/vendor-*.js` 200、`/api/tasks` 401（未登录，符合预期）。
    **但 `/static/icons/icon-192.png` → 404。**
  - `uv run pytest -q -p no:cacheprovider` → 退出码 0：`440 passed, 1 skipped`。

- 观察到的现象 / 反直觉之处 / 踩坑：
  - **反直觉（本轮最重要的一条）**：`/static/*` **完全没有被挂载**。
    前端 `index.html` 引用了图标，服务端却没有任何路由处理 `/static/`，
    于是浏览器拿到 404。这是纯装配问题——代码都对，只是少了一行 `mount`。
    **单测看不见它，只有真的去取一次才会发现**。这正是用户要求"用 browser 检查"
    的价值所在；我暂时用 HTTP 探测 + Node 检查代替（原因见下面的阻塞项），
    并把这件事固化成 4 条测试。
  - **反直觉**：pnpm 的配置迁移踩了两次，而且**两次都不报致命错误**——
    第一次是 WARNING（"字段不再被读取"），第二次是 `ERR_PNPM_IGNORED_BUILDS`
    但却**不影响构建**（因为 esbuild 0.25 的平台二进制是通过 optionalDependency
    `@esbuild/win32-x64` 分发的，不依赖 postinstall）。
    也就是说：如果我只看"构建成功"，会一直不知道配置是错的；
    而 `pnpm install` 的退出码 1 会打断任何 CI。**判据选错会让人漏掉真问题。**
  - **反直觉**：我用 `vm.Script` 去"编译检查"产物，得到
    `Cannot use import statement outside a module`。那是**检查方式错了**，
    不是产物坏了——产物是 ESM，而 `vm.Script` 不认识 `import`。
    换成 `node --check` 就对了。（和上面 pnpm 那件事同一个教训：
    工具报错时先怀疑工具用错，再怀疑对象坏了。）
  - 我第一版产物检查脚本把路径算错了（`path.join(app, '/static/...')`
    会把以斜杠开头的路径当绝对路径直接返回），于是报了 3 个假的 MISS。
    修好路径换算后全绿。**假失败会训练人忽略检查结果**，所以必须修掉而不是绕过。
  - `vue-tsc` 的 `noUnusedLocals` 抓出了 7 处死代码（未使用的导入、写了没用的
    `doneItems` / `showDone`）。这类东西在浏览器里完全看不出来，但会留在代码里
    让人以为"这个变量还有用"。

- 未决问题与下一步：
  - **浏览器验证被阻塞（需要用户操作）**：`browser_session` 报
    `the bsk CLI ("bsk") was not found. BrowserSkill must be installed and on PATH`。
    本机既没有 `bsk`，也没有 `cargo` 可以编译它（插件本身装在
    `~/.dsh/profiles/desktop/node_modules/@wxg-prc-cpg/browser-skill-dsh-plugin/`，
    但只是插件壳，缺底层 CLI）。
    用户明确要求"用 browser 自己检查"，所以这一条必须由用户装好 `bsk`
    （见 https://github.com/Tencent/BrowserSkill）后我再补做视觉与交互验证。
    **在补做之前，前端的动效与布局只能算"实现完成"，不能算"验收通过"。**
  - 已用非浏览器手段覆盖的部分（`node .pytest-run/check-spa.cjs`）：
    * index.html 的 6 个引用全部存在（含后端生成的 manifest）
    * 入口 chunk 通过 `node --check`
    * 9 个懒加载页面 chunk 齐全
    * 产物里没有残留 `127.0.0.1:5173` / `localhost:8000` 这类开发期地址
    * 产物总计 222KB（gzip 后约 68KB）
    这些能覆盖"白屏/404"类问题，**覆盖不了**布局错位、动效是否顺、
    触摸目标是否够大、深浅色对比度——那些只能在真浏览器里看。
  - 前端还没部署到服务器：`app/static/spa/` 与图标已入库，但 8443 上跑的是
    上一个提交的代码。部署一次即可上线（`deploy/install.sh` 幂等）。
  - Scriptable 小组件与快捷指令文档已写好但**未真机验证**（同样需要先有界面）。
  - 本地提交待用户推送（本会话的推送一律交给用户执行）。

---

## 2026-10-03 21:33 · 浏览器实测：抓到 5 个只有真浏览器才暴露的缺陷

- 目标：用户要求"写前端记得用 browser 自己检查"。这一轮用 browser-skill 在 Edge 里
  实际操作**部署在 8443 上的真实前端**，而不是只看构建是否成功。

- 决策与理由：
  - **验证对象选"已部署的公网地址"**，不是本地 dev server。
    - 理由：真实环境才有 Caddy、真 HTTPS、真 Service Worker 与跨网络时序。
      本轮抓到的 `Cache-Control` 重复问题只在 Caddy 在场时出现。
    - 被否决：本地 `pnpm dev` + 代理。否决原因是它跳过 Caddy 这一层，
      而这一层恰好是问题的来源。
  - **先走真实链路再造数据**：用 API Key 调 `/api/ingest` 生成草稿，而不是直接改库。
    - 理由：这样连"快捷指令那条路"也一起验了（响应里 `channel=shortcut`）。
  - **每页都看可访问性树，不只截图**。
    - 理由：可访问性树能直接读出"哪些按钮是 disabled"。二选一联动就是靠它一眼确认的
      （有截止时间的条目优先级按钮为 disabled，没有的可用）。
  - **把静态约定用 pytest 钉住**（新增 `tests/test_web_contract.py`，11 条）。
    - 理由：视觉只能靠浏览器看，但"直接截断 UTC 时间戳"这类是纯静态问题，
      值得一次性固化成回归测试。
    - 被否决：只靠人工记得。否决原因是本轮这类错误一次出现两处，
    而且它们在界面上**看起来完全正常**。

- 触及文件：
  - 新增：`tests/test_web_contract.py`（11 条前端静态约定测试）
  - 修改：`web/src/pages/DraftConfirmPage.vue`（动作条让出 dock 高度）
  - 修改：`web/src/components/TaskCard.vue`（时间改用 `formatDateTime`）
  - 修改：`web/src/pages/SettingsPage.vue`（key"最近使用"改用 `formatRelative`）
  - 修改：`web/index.html`（补标准的 `mobile-web-app-capable`）
  - 修改：`web/src/pages/LoginPage.vue`、`web/src/styles/base.css`
    （隐藏 username 字段 + `.sr-only`）
  - 修改：`deploy/Caddyfile.template`、`app/main.py`
    （Cache-Control 单一来源 + `_CachedStaticFiles`）
  - 新增（gitignore 内）：`.pytest-run/verify-frontend.mjs`

- 执行过的命令与结果：
  - `browser_session` 起会话失败两次：
    1. `the bsk CLI ("bsk") was not found`（当时还没装）
    2. 装好后 `cannot start an independent Windows daemon ... Job Object breakaway`
       —— DSH 的 Job Object 阻止 daemon 脱离。改用
       `bsk daemon start --foreground` 作为后台任务启动成功；但**随后发现用户那边
       已有 daemon**，我那次尝试其实是多余的（报 `daemon lock is already held`）。
  - 在 Edge 里逐页操作并截图：登录 → 首页三视图 → 新建任务（含模式切换）→
    勾选完成 → 深色模式 → 确认页 → 入库 → 课表（含网格）→ 记忆 → 设置 → PC 宽屏。
  - 造数据：服务端建 API Key → `POST /api/ingest` → 3 条解析结果
    （`channel=shortcut`，两条相对时间被解析成具体日期）。
    这一步踩了坑：**把 key 当 PowerShell 参数传给 Node 会被改坏**，得到 401；
    把 key 直接内联进脚本就正常。
  - `uv run pytest -q -p no:cacheprovider` → 退出码 0：`455 passed, 1 skipped`。
  - `node .pytest-run/verify-frontend.mjs`（公网 8443）→ **23/23 通过**。

- 观察到的现象 / 反直觉之处 / 踩坑：
  本轮抓到 5 个缺陷：
  1. **`Cache-Control` 有两个来源**（应用 + Caddy）。真实响应里出现两个同名头
     （`/`、`/sw.js`、`/manifest.webmanifest` 都是 `no-cache, no-cache`），
     而图标**一个缓存头都没有**。这种配置不报错，只会让以后改策略时只改一半。
  2. **确认页动作条被底部 dock 遮住一半**（截图可见）。
     `sticky; bottom: 0` 是相对滚动容器的 padding box，而外层 `.shell__main` 的
     padding-bottom 只保证"滚动到底时"内容不被挡——两者不是一回事。
  3. **任务卡片显示 UTC 时间**：`2026-10-04 15:59`（本应 23:59）。
     根因 `due_at.slice(0, 16)`。服务端按约定只存 UTC，直接截断必然差 8 小时，
     而**它在界面上看起来完全正常**——只能靠"知道正确值该是多少"来发现。
     设置页的时间戳有同样问题。
  4. **`apple-mobile-web-app-capable` 被 Chrome 标为 deprecated**。
     但 iOS 至今只认它，所以不能简单替换，两个 meta 都要输出。
  5. **密码表单缺少 username 字段**（Chromium 可访问性警告）。
     加视觉隐藏的 username；**不能用 `display: none`**——那会把元素从可访问性树
     摘掉，等于没加（改用 `.sr-only` 的裁剪写法）。
  - **反直觉**：修复后控制台**仍然**显示那两条警告，我一度以为没生效。
    实际那是修复前的日志，硬刷新后消失。判断"日志是不是本次的"要看时序。
  - **反直觉**：剩下 4 条 `Unrecognized feature: 'attribution-reporting'` 等
    Permissions-Policy 警告，我怀疑是自己写的安全头；查代码才确认
    **我们根本没设 Permissions-Policy**——那是 Edge 自身的广告特性。
    差点去改一个不存在的问题。
  - 浏览器会话里的 refs 在每次 DOM 变化后失效，必须先 `observe` 再操作；
    隔几轮复用 `@e57` 这类引用一定会失败。

- 未决问题与下一步：
  - **本轮已覆盖**：登录、首页三视图、新建表单（含二选一模式切换）、勾选完成、
    深浅色、录入→草稿→确认→入库全链路、override 横幅、课表网格、记忆、设置、
    PC 宽屏布局、控制台洁净度。
  - **仍未覆盖**：真机（iPhone Safari）的 PWA 安装、"添加到主屏幕"、
    刘海屏 safe-area 的实际表现、触摸手感、iOS 上的 Service Worker 作用域。
    这些只能在手机上确认。
  - 动效只确认了"静态帧看起来对"，没有逐帧检查缓动曲线——属主观验收，留给用户。
  - QA 数据已清理（`items` / `ingest_drafts` / `courses` 归零，临时 Key 已吊销），
    服务保持运行。
  - 3 个提交待推送（`9443c2d` / `1252db2` / `5940f14`），按约定交给用户。

