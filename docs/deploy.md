# 部署

目标机器：Aliyun ECS，Ubuntu 24.04，公网 `116.62.52.66`。
对外地址：`https://canisa1ph.duckdns.org:8443`。

---

## 为什么是 8443 而不是 443

两条**硬约束**，不是偏好：

1. **手机上已装好的快捷指令写的就是这个端口。** 换端口要重新编辑并在手机上重新
   分发快捷指令，个别情况下还要重新授权相机权限。
2. **80/443 是备案相关端口。** 用 8443 正是为了绕开备案要求；改回 443 会把这个问题
   重新引进来。**没备案就不能碰 443。**

`tests/test_config.py` 里有三条测试锁住这个选择（代码默认值 / `config.toml.example` /
`deploy/Caddyfile`），所以"顺手改成 443"会让 CI 红。详见 `docs/decisions.md` D-007。

代价（显式记录）：非标准端口下 Service Worker 作用域、Cookie `SameSite`、
"添加到主屏幕"的行为都与 443 不同，**必须真机确认**。

---

## 这台机器上还跑着什么（不要碰）

| 服务 | 端口 | 说明 |
|---|---|---|
| cloudreve（docker） | 8078、6888 | 网盘 + WebDAV |
| portainer（docker） | 8001 | 容器管理 |
| postgresql / redis（docker） | 内部 | cloudreve 依赖 |
| `caddy-webdav.service` | 8079 | Cloudreve WebDAV 反代 |

ScheduleKit 只贡献 **一个 Caddy 站点块**（用 marker 包起来，见下），
**不改全局块、不动别人的站点**。`install.sh` 写 Caddyfile 前会先备份、
先 `caddy validate`、再用 marker 精确替换。

---

## 首次部署

```bash
# 1. 在开发机打包（只含已跟踪文件，不含 .venv / data / .pytest-run）
git archive --format=tar.gz -o /tmp/schedulekit.tar.gz HEAD

# 2. 上传
scp /tmp/schedulekit.tar.gz root@116.62.52.66:/root/

# 3. 在服务器上解包并部署
ssh root@116.62.52.66
mkdir -p /root/schedulekit-src && tar -xzf /root/schedulekit-release.tar.gz -C /root/schedulekit-src
export SK_DUCKDNS_TOKEN='<你的 DuckDNS token>'
export SK_NEW_PASSWORD='<管理员密码>'
bash /root/schedulekit-src/deploy/install.sh --init
```

`install.sh` 会做这些事（全部幂等）：

1. 检查 python3 ≥ 3.11 与 caddy；没有 `uv` 就装一个
2. 创建系统账号 `schedulekit`（无家目录、无密码、nologin）
3. 生成 `/etc/schedulekit/config.toml`（0600）并写入 DuckDNS token
4. 同步代码到 `/opt/schedulekit`
5. 建虚拟环境并装依赖（`uv sync --frozen --no-dev`）
6. `app.cli init`：生成 `auth.secret_key`、设置密码、建库、应用迁移
7. 用 `app.cli show --json` 读配置（**脚本不自己解析 TOML**）
8. acme.sh 走 DuckDNS DNS-01 签证书到 `/etc/schedulekit-tls`
9. 渲染并写入 Caddy 站点块（marker 之间），`validate` 后 `restart`
10. 安装并启动 `schedulekit.service`，配置 DuckDNS 同步 timer
11. 自检：配置检查 + 回环 http + 带 SNI 的 https + 内存占用

### 首次部署后

```bash
# 管理员密码（仅 root 可读）
cat /root/schedulekit-credentials.txt
```

登录后在控制台改密码。改密码会让所有既有会话立即失效
（`auth.session_epoch` 自增——无状态 Cookie 唯一的吊销手段）。

---

## 后续部署（改完代码）

```bash
git archive --format=tar.gz -o /tmp/schedulekit.tar.gz HEAD
scp /tmp/schedulekit.tar.gz root@116.62.52.66:/root/
ssh root@116.62.52.66
rm -rf /root/schedulekit-src && mkdir -p /root/schedulekit-src
tar -xzf /root/schedulekit-release.tar.gz -C /root/schedulekit-src
bash /root/schedulekit-src/deploy/install.sh      # 不带 --init
```

不带 `--init` 时**不会**碰密码，但仍然会跑一次 `app.cli init --no-password`——
那是为了幂等地补上缺失的 `secret_key`。

---

## 配置

唯一配置文件：`/etc/schedulekit/config.toml`（属 `schedulekit`，0600）。

控制台的"设置"改 `[llm]` 与 `[term]` 会**就地改写这个文件并保留全部注释**
（tomlkit）。应用在每个请求开始时重读它，所以改完立即生效，**不需要重启**。

密钥可以用环境变量覆盖（优先级高于文件，空字符串不算覆盖）：
`SK_SECRET_KEY` / `SK_PASSWORD_HASH` / `SK_LLM_API_KEY` / `SK_DUCKDNS_TOKEN`。

### 填入真实 LLM 凭据

首次部署用 `provider = "mock"`（离线确定性抽取），所以没有 Key 也能把链路跑通。
要接真实模型，二选一：

```bash
# 方式一：控制台 → 设置 → LLM，填 base_url / model / api_key（保存即生效）
# 方式二：直接改文件后重启
sudo -u schedulekit vim /etc/schedulekit/config.toml   # provider = "responses"，填 api_key
systemctl restart schedulekit
```

> **DeepSeek 的一个硬约束**：thinking 模式下 `tool_choice` 传 `required` 或具名工具
> 会被直接 400 拒绝。所以主通道（强制 function calling）必须
> `thinking = false`。详见 `app/llm/structured.py` 与 `docs/decisions.md` D-009。

---

## 换 DuckDNS token

```bash
sudo -u schedulekit python3 - <<'PY'
from pathlib import Path
p = Path('/etc/schedulekit/config.toml')
text = p.read_text(encoding='utf-8')
out, in_tls = [], False
for line in text.splitlines(keepends=True):
    if line.lstrip().startswith('['):
        in_tls = line.strip() == '[tls]'
    if in_tls and line.lstrip().startswith('duckdns_token'):
        line = 'duckdns_token = "<新 token>"\n'
    out.append(line)
p.write_text(''.join(out), encoding='utf-8')
PY
systemctl restart schedulekit-duckdns.service   # 立刻同步一次
systemctl restart schedulekit                    # 让应用读到新配置
```

---

## 排障

### 服务起不来

```bash
systemctl status schedulekit
journalctl -u schedulekit -n 50 --no-pager
```

最常见的原因是**配置校验失败（fail-closed）**，日志会明确说是哪一项，例如：

```
ConfigError: 配置校验失败：
  - auth.secret_key 为空：会话 Cookie 无法签名。
```

这类"启动期就拒绝"是刻意的：宁可服务不启动，也不要带着不能签名的会话起来。
修法是重跑 `install.sh`（它会补上 `secret_key`）。

### 改完 Caddyfile 用 restart 而不是 reload

`Caddyfile` 里有 `admin off`，而 `caddy reload` 正是通过 `:2019` 管理端点下发配置，
所以：

```
Job for caddy.service failed.
Error: sending configuration to instance: ... dial tcp 127.0.0.1:2019: connect: connection refused
```

**用 `systemctl restart caddy`。** 改配置前先 `caddy validate --config /etc/caddy/Caddyfile
--adapter caddyfile`——这台机器上 Caddy 还托管别人的站点，一次坏配置会让它们一起挂。

### https 探测失败但 http 正常

先区分"拒绝"与"超时"，两者含义相反：

```bash
# 本机带 SNI 自测（--resolve 把域名指到回环）
curl -v --resolve canisa1ph.duckdns.org:8443:127.0.0.1 https://canisa1ph.duckdns.org:8443/healthz
```

- **TLS 握手报 `tlsv1 alert internal error`** → 多半是**没发 SNI**。
  用 `--resolve` 或 `openssl s_client -servername ...`；
  `curl -k https://127.0.0.1/` 是不行的（按 IP 选不出证书）。
- **连接超时（TIMEOUT）** → 被安全组过滤。
- **连接被拒绝（REFUSED）** → 安全组放行了，但没有进程监听。

外网不可达时，检查 Aliyun 安全组是否放行 8443（TCP）。

### 端口连通性对照（实测）

| 端口 | 外网 | 说明 |
|---|---|---|
| 22 | 可达 | SSH |
| 8443 | 可达 | ScheduleKit（Caddy TLS） |
| 8078 | 可达 | cloudreve |
| 80 / 443 | **被过滤** | 备案相关端口，刻意不开 |

### 内存

```bash
systemctl show schedulekit -p MemoryCurrent --value | awk '{printf "%.1f MB\n", $1/1048576}'
```

实测常驻约 **50MB**（单元里 `MemoryMax=256M` 是上限，不是占用）。

### 日志

```bash
journalctl -u schedulekit -f                    # 实时
journalctl -u schedulekit | grep 't=8f3a2b1c'   # 只看某个请求（X-Trace-Id）
journalctl -u schedulekit | grep 'llm.response' # 只看 LLM 调用
journalctl -u schedulekit | grep 'priority_dropped'  # 只看"代码推翻了模型输出"的地方
```

最后一条尤其有用：它是判断"识别不对"到底该改 prompt 还是该改代码的唯一依据。

---

## 旧实例归档

上一版 ScheduleKit 拆除前完整归档在 `/root/legacy-schedulekit/`（0600）：
可读 SQL dump、原始数据库、上传图片 tar、旧配置、旧设计文档。
**只作保险，不提供自动恢复。**
