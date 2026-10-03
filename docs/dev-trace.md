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

