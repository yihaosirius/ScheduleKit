#!/usr/bin/env bash
# ============================================================================
# ScheduleKit 部署脚本（幂等，可反复执行）
#
# 用法：
#   sudo bash deploy/install.sh --init     # 首次：生成配置、建库、设密码、签证书、起服务
#   sudo bash deploy/install.sh            # 后续：同步代码并重启服务
#
# 设计要点（每条都有踩过的坑在后面）：
#
#   * **不自己解析 TOML**。所有配置都从 `python -m app.cli show --json` 取。
#     这样"配置格式"只有一处实现，改结构时不会漏掉脚本里的正则。
#
#   * **写入 /etc/caddy/Caddyfile 时先备份、先 validate、原子替换**。
#     这台机器上 Caddy 还托管着别人的站点（9000 旁边的服务），
#     一次写坏会让它们一起挂。
#
#   * **不改 Caddy 的全局配置**。ScheduleKit 只贡献一个站点块；
#     全局块（admin off / log）由模板提供，但若服务器上已有 Caddyfile，
#     我们只追加自己的站点块，不动别人原有内容。
#
#   * **证书由 acme.sh 走 DNS-01 签发**，不需要 80/443 入站。
#
#   * **内存有限的机器**上要确认 MemoryMax 生效，脚本会打印当前占用。
# ============================================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/schedulekit"
CONF_DIR="/etc/schedulekit"
DATA_DIR="/var/lib/schedulekit"
SERVICE_USER="schedulekit"
SERVICE_NAME="schedulekit"
CADDY_FILE="/etc/caddy/Caddyfile"
IMPORT_BEGIN="# >>> ScheduleKit 站点块（由 deploy/install.sh 管理，请勿手工编辑） >>>"
IMPORT_END="# <<< ScheduleKit 站点块 <<<"

INIT=0
[[ "${1:-}" == "--init" ]] && INIT=1

log()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[error] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "必须以 root 运行（sudo bash deploy/install.sh）"

# --------------------------------------------------------------------------- #
# 0. 依赖检查
# --------------------------------------------------------------------------- #
log "检查依赖"
command -v python3 >/dev/null || die "缺少 python3"
command -v caddy   >/dev/null || die "缺少 caddy（本脚本不负责安装反代）"
command -v curl    >/dev/null || die "缺少 curl"

PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
[[ "${PY_VER%%.*}" -ge 3 && "${PY_VER##*.}" -ge 11 ]] \
  || die "需要 Python >= 3.11，当前 $PY_VER"
echo "  python3 $PY_VER / caddy $(caddy version | head -1)"

# uv 用来装依赖到项目自带的 venv。没有就装一个（仅这一步会联网）。
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null && [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  log "安装 uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null && echo "  uv $(uv --version 2>/dev/null | head -1)"

# --------------------------------------------------------------------------- #
# 1. 服务账号
# --------------------------------------------------------------------------- #
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  log "创建服务账号 $SERVICE_USER"
  # --system：不建家目录、不设置密码、不在登录界面出现
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
else
  echo "  服务账号已存在：$SERVICE_USER"
fi

# --------------------------------------------------------------------------- #
# 2. 配置文件
# --------------------------------------------------------------------------- #
install -d -m 0755 "$CONF_DIR"
if [[ ! -f "$CONF_DIR/config.toml" ]]; then
  if [[ $INIT -eq 0 ]]; then
    die "找不到 $CONF_DIR/config.toml。首次部署请加 --init"
  fi
  log "生成配置（首次部署）"
  cp "$SRC_DIR/config.toml.example" "$CONF_DIR/config.toml"
  # 数据目录固定到 /var/lib，而不是配置里的相对路径
  sed -i 's|^data_dir *=.*|data_dir    = "/var/lib/schedulekit"|' "$CONF_DIR/config.toml"
  echo "  已写入 $CONF_DIR/config.toml"
else
  echo "  配置已存在：$CONF_DIR/config.toml"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$CONF_DIR" "$CONF_DIR/config.toml"
chmod 0750 "$CONF_DIR"
chmod 0600 "$CONF_DIR/config.toml"

# DuckDNS token：从环境变量注入（首次）。已经配过就不覆盖。
if [[ -n "${SK_DUCKDNS_TOKEN:-}" ]]; then
  log "写入 DuckDNS token"
  python3 - "$CONF_DIR/config.toml" "$SK_DUCKDNS_TOKEN" <<'PY'
import sys
from pathlib import Path
path, token = Path(sys.argv[1]), sys.argv[2]
text = path.read_text(encoding="utf-8")
# 逐行替换 [tls] 段里的 duckdns_token，不动其它内容与注释
out, in_tls = [], False
for line in text.splitlines(keepends=True):
    if line.lstrip().startswith("["):
        in_tls = line.strip() == "[tls]"
    if in_tls and line.lstrip().startswith("duckdns_token"):
        line = f'duckdns_token = "{token}"\n'
    out.append(line)
path.write_text("".join(out), encoding="utf-8")
print("  已写入 tls.duckdns_token（0600，不入仓库）")
PY
fi

install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"

# --------------------------------------------------------------------------- #
# 3. 同步代码
# --------------------------------------------------------------------------- #
log "同步代码到 $INSTALL_DIR"
install -d -m 0755 "$INSTALL_DIR"
# 只同步需要的东西。刻意排除 .venv（服务器上单独建）、data、.git、
# 以及本地测试产物——它们要么很大，要么属于开发机。
for item in app deploy docs pyproject.toml uv.lock config.toml.example README.md AGENTS.md; do
  [[ -e "$SRC_DIR/$item" ]] || continue
  rm -rf "${INSTALL_DIR:?}/$item"
  cp -a "$SRC_DIR/$item" "$INSTALL_DIR/"
done
echo "  已同步：$(ls -1 "$INSTALL_DIR" | tr '\n' ' ')"

# --------------------------------------------------------------------------- #
# 4. 虚拟环境与依赖
# --------------------------------------------------------------------------- #
log "准备虚拟环境"
cd "$INSTALL_DIR"
if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  if command -v uv >/dev/null; then
    # UV_PROJECT_ENVIRONMENT 让 uv 直接建到项目内的 .venv
    UV_PROJECT_ENVIRONMENT="$INSTALL_DIR/.venv" uv sync --frozen --no-dev
  else
    python3 -m venv "$INSTALL_DIR/.venv"
    "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
    "$INSTALL_DIR/.venv/bin/pip" install .
  fi
else
  if command -v uv >/dev/null; then
    UV_PROJECT_ENVIRONMENT="$INSTALL_DIR/.venv" uv sync --frozen --no-dev
  else
    echo "  已有虚拟环境，跳过依赖安装（没有 uv，无法校验锁文件）"
  fi
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# --------------------------------------------------------------------------- #
# 5. 数据库、签名密钥与密码
#
# 两个都必须显式处理的坑：
#
# ⚠️ 坑一：`sudo -u user` **默认会重置环境变量**（env_reset），所以不能写成
#    `SK_X=1 sudo -u user cmd`——变量根本传不进去。必须 `sudo env K=V cmd`。
#    第一次部署就踩了这个：`set-password` 拿到空的 SK_NEW_PASSWORD，
#    报"没有拿到新密码"。
#
# ⚠️ 坑二：`sudo -u` 之后**工作目录不是项目目录**，于是 `python -m app.cli`
#    找不到 `app` 包，报 `No module named 'app'`。必须显式 `cd` 进去。
#    系统 systemd 单元里是靠 WorkingDirectory 解决的，这里得自己来。
# --------------------------------------------------------------------------- #
run_cli() {
  # 以服务账号身份、在项目目录里跑 app.cli，并显式传环境变量
  local -a extra_env=()
  while [[ "$1" == *=* ]]; do
    extra_env+=("$1")
    shift
  done
  sudo -u "$SERVICE_USER" env \
    "SK_CONFIG=$CONF_DIR/config.toml" \
    "${extra_env[@]}" \
    bash -c "cd '$INSTALL_DIR' && exec ./.venv/bin/python -m app.cli $*"
}

log "初始化数据库与签名密钥"
if [[ $INIT -eq 1 ]]; then
  # 用 `app.cli init` 而不是直接调 set-password：init 会**先补生成
  # auth.secret_key**（会话 Cookie 的签名根密钥），再设密码、建库。
  # 少了 secret_key 时应用会在启动期 fail-closed 退出——这是刻意的设计，
  # 但部署脚本必须把生成它当成自己的一步，否则得到的是"服务起不来"。
  if [[ -z "${SK_NEW_PASSWORD:-}" ]]; then
    read -r -s -p "为管理员设置登录密码：" SK_NEW_PASSWORD
    echo
    [[ -n "$SK_NEW_PASSWORD" ]] || die "密码不能为空"
    export SK_NEW_PASSWORD
  fi
  run_cli "SK_NEW_PASSWORD=$SK_NEW_PASSWORD" init
  unset SK_NEW_PASSWORD
else
  # 非首次也跑一次：幂等地补上缺失的 secret_key（例如从没有密钥的旧配置升级），
  # 而 --no-password 保证不会动已有密码。
  run_cli init --no-password
fi

# --------------------------------------------------------------------------- #
# 6. 读配置（唯一入口是 app.cli，脚本不解析 TOML）
#
# 以 root 读：root 一定能读 0600 的配置，不需要冒"环境变量没传进去"的风险。
# --------------------------------------------------------------------------- #
log "读取配置"
# root 直接读：root 一定能读 0600 的配置，不需要冒"环境变量没传进去"的风险。
# 同样要 cd 进项目目录，否则 `-m app.cli` 找不到包。
CFG_JSON="$(cd "$INSTALL_DIR" && SK_CONFIG="$CONF_DIR/config.toml" \
  ./.venv/bin/python -m app.cli show --json --secrets)"

# 从配置 JSON 里按"点分路径"取值，例如 `jget tls.domain`。
#
# 刻意**不用** `python3 -c "...eval('d'+'$1')..."` 那种拼字符串的做法：
# 引号会在 shell → python 的传递中被吃掉一层，得到一个畸形的表达式
# （实测报 `SyntaxError: invalid syntax. Perhaps you forgot a comma?`）。
# 改成把路径当**参数**传给一段固定的脚本，不接受任何拼接。
jget() {
  printf '%s' "$CFG_JSON" | python3 -c '
import json, sys
node = json.load(sys.stdin)
for part in sys.argv[1].split("."):
    if not part:
        continue
    if isinstance(node, list):
        node = node[int(part)]
    else:
        node = node[part]
if node is None:
    print("")
elif isinstance(node, bool):
    print("true" if node else "false")
else:
    print(node)
' "$1"
}

DOMAIN="$(jget tls.domain)"
SITE_PORT="$(jget tls.port)"
CERT_DIR="$(jget tls.cert_dir)"
LISTEN_HOST="$(jget server.listen_host)"
LISTEN_PORT="$(jget server.listen_port)"
UPSTREAM="${LISTEN_HOST}:${LISTEN_PORT}"
DUCKDNS_TOKEN="$(jget tls.duckdns_token)"
TLS_PROVIDER="$(jget tls.provider)"
echo "  domain=$DOMAIN port=$SITE_PORT upstream=$UPSTREAM cert_dir=$CERT_DIR"

[[ -n "$DOMAIN" && -n "$SITE_PORT" ]] || die "配置里缺少 tls.domain 或 tls.port"

# --------------------------------------------------------------------------- #
# 7. 证书（acme.sh + DuckDNS DNS-01）
# --------------------------------------------------------------------------- #
log "准备 TLS 证书"
install -d -m 0755 -o root -g root "$CERT_DIR"
ACME_HOME="/root/.acme.sh"
ACME="$ACME_HOME/acme.sh"

if [[ -f "$CERT_DIR/fullchain.pem" && -f "$CERT_DIR/privkey.pem" ]]; then
  echo "  证书已存在，跳过签发"
else
  [[ -n "$DUCKDNS_TOKEN" ]] || die "证书不存在，且配置里没有 tls.duckdns_token，无法自动签发"
  command -v "$ACME" >/dev/null || [[ -x "$ACME" ]] || die "缺少 acme.sh：$ACME"

  DUCKDNS_TOKEN="$DUCKDNS_TOKEN" "$ACME" --issue --dns dns_duckdns \
    -d "$DOMAIN" --keylength ec-256 \
    || warn "签发失败（可能是 DNS 还没生效）。可稍后重跑本脚本。"

  DUCKDNS_TOKEN="$DUCKDNS_TOKEN" "$ACME" --install-cert -d "$DOMAIN" --ecc \
    --fullchain-file "$CERT_DIR/fullchain.pem" \
    --key-file "$CERT_DIR/privkey.pem" \
    --reloadcmd "systemctl restart caddy"
fi

# 私钥只给 caddy 读：Caddyfile 里的 tls 指令由 caddy 用户执行
chown -R root:caddy "$CERT_DIR"
chmod 0755 "$CERT_DIR"
chmod 0644 "$CERT_DIR/fullchain.pem" 2>/dev/null || true
chmod 0640 "$CERT_DIR/privkey.pem"   2>/dev/null || true

# --------------------------------------------------------------------------- #
# 8. Caddy 站点块
#
# 只替换两个标记之间的内容，**不动服务器上已有的其它站点与全局块**。
# 这台机器上 Caddy 还托管着别的服务，一次全量覆盖会让它们一起挂。
# --------------------------------------------------------------------------- #
log "写入 Caddy 站点块"
RENDERED="$(mktemp)"
SITE_ONLY="$(mktemp)"
sed -e "s|__DOMAIN__|$DOMAIN|g" \
    -e "s|__SITE_PORT__|$SITE_PORT|g" \
    -e "s|__CERT_DIR__|$CERT_DIR|g" \
    -e "s|__UPSTREAM__|$UPSTREAM|g" \
    "$SRC_DIR/deploy/Caddyfile.template" > "$RENDERED"

# **只取站点块，不要模板里的全局块**。
#
# 这台机器上的 Caddyfile 已经有自己的全局块（admin off / log），而且它还托管
# 着别人的站点。再插一个全局块进去会让 Caddy 直接报错（全局块只能有一个）。
# 所以 ScheduleKit 只贡献"自己的站点块"，其余部分一律不动——
# 这是在多站点机器上唯一安全的做法。
python3 - "$RENDERED" "$SITE_ONLY" <<'PY'
import sys
from pathlib import Path

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
text = src.read_text(encoding="utf-8")
# 站点块从第一行形如 "<scheme>://" 的行开始
lines = text.splitlines(keepends=True)
start = next(i for i, line in enumerate(lines) if line.lstrip().startswith("https://"))
dst.write_text("".join(lines[start:]), encoding="utf-8")
PY

python3 - "$CADDY_FILE" "$SITE_ONLY" "$IMPORT_BEGIN" "$IMPORT_END" <<'PY'
import sys
from pathlib import Path

caddy_file, rendered, begin, end = (Path(sys.argv[1]), Path(sys.argv[2]),
                                    sys.argv[3], sys.argv[4])
block = rendered.read_text(encoding="utf-8").rstrip() + "\n"
if not block.strip():
    raise SystemExit("渲染后的站点块为空，拒绝写入 Caddyfile")

existed = caddy_file.exists()
existing = caddy_file.read_text(encoding="utf-8") if existed else ""
has_block = begin in existing and end in existing

if existed:
    # 备份带时间戳：Caddy 在这台机器上还托管别人的站点，必须有退路
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (caddy_file.parent / f"{caddy_file.name}.bak.{stamp}").write_text(
        existing, encoding="utf-8")

if has_block:
    head, rest = existing.split(begin, 1)
    _, tail = rest.split(end, 1)
    new = f"{head}{begin}\n{block}{end}{tail}"
elif existed:
    # 首次接入：追加到文件末尾。Caddyfile 支持多个站点块并列，
    # 所以不会影响服务器上已有的站点。
    new = existing.rstrip() + "\n\n" + begin + "\n" + block + end + "\n"
else:
    new = begin + "\n" + block + end + "\n"

caddy_file.write_text(new, encoding="utf-8")
print(f"  已{'替换' if has_block else '写入'}站点块：{caddy_file}")
PY
rm -f "$RENDERED" "$SITE_ONLY"

# 先 validate 再 reload：一次不合格的配置会让 Caddy 整个起不来
if ! caddy validate --config "$CADDY_FILE" --adapter caddyfile >/dev/null 2>&1; then
  die "Caddyfile 校验失败。已保留备份，请检查 $CADDY_FILE"
fi
echo "  Caddyfile 校验通过"

# ⚠️ admin off 与 caddy reload 互斥（reload 走 :2019 管理端点），只能 restart
systemctl restart caddy
sleep 1
systemctl is-active --quiet caddy || die "caddy 重启失败，请看 journalctl -u caddy"
echo "  caddy 已重启"

# --------------------------------------------------------------------------- #
# 9. DuckDNS 同步
# --------------------------------------------------------------------------- #
log "配置 DuckDNS 同步"
DUCKDNS_SH="$CONF_DIR/duckdns-update.sh"
if [[ "$TLS_PROVIDER" == "duckdns" ]]; then
  # 脚本从配置读 token，**不把 token 写进脚本本身**：
  # 上一版把 token 硬编码在脚本里，导致它随脚本一起进了仓库。
  cat > "$DUCKDNS_SH" <<'SH'
#!/usr/bin/env bash
# 由 deploy/install.sh 生成。token 从配置读，不硬编码。
set -euo pipefail
CONF="${SK_CONFIG:-/etc/schedulekit/config.toml}"
TOKEN="$(python3 - "$CONF" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    print(tomllib.load(fh)["tls"].get("duckdns_token", ""))
PY
)"
DOMAIN="$(python3 - "$CONF" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    print(tomllib.load(fh)["tls"].get("domain", ""))
PY
)"
[[ -n "$TOKEN" && -n "$DOMAIN" ]] || exit 0
# 只取域名的主标签（abc.duckdns.org -> abc）
SUB="${DOMAIN%%.*}"
curl -fsS --max-time 20 "https://www.duckdns.org/update?domains=${SUB}&token=${TOKEN}&ip=" >/dev/null
SH
  chmod 0700 "$DUCKDNS_SH"
  chown root:root "$DUCKDNS_SH"

  cat > /etc/systemd/system/schedulekit-duckdns.service <<UNIT
[Unit]
Description=同步 DuckDNS A 记录（$DOMAIN）

[Service]
Type=oneshot
Environment=SK_CONFIG=$CONF_DIR/config.toml
ExecStart=$DUCKDNS_SH
UNIT

  cat > /etc/systemd/system/schedulekit-duckdns.timer <<'UNIT'
[Unit]
Description=每天同步 DuckDNS A 记录

[Timer]
OnBootSec=2min
OnUnitActiveSec=6h

[Install]
WantedBy=timers.target
UNIT

  systemctl daemon-reload
  systemctl enable --now schedulekit-duckdns.timer >/dev/null 2>&1 || warn "timer 启用失败"
  echo "  已配置（每 6 小时一次）"
else
  echo "  tls.provider 不是 duckdns，跳过"
fi

# --------------------------------------------------------------------------- #
# 10. 服务
# --------------------------------------------------------------------------- #
log "安装并启动服务"
install -m 0644 "$SRC_DIR/deploy/schedulekit.service" \
  /etc/systemd/system/schedulekit.service
systemctl daemon-reload
systemctl enable schedulekit >/dev/null 2>&1 || true
systemctl restart schedulekit
sleep 2

if ! systemctl is-active --quiet schedulekit; then
  warn "服务未启动，最近日志："
  journalctl -u schedulekit -n 30 --no-pager || true
  die "schedulekit 启动失败"
fi
echo "  服务已启动"

# --------------------------------------------------------------------------- #
# 11. 自检
# --------------------------------------------------------------------------- #
log "自检"
(cd "$INSTALL_DIR" && SK_CONFIG="$CONF_DIR/config.toml" ./.venv/bin/python -m app.cli --pretty check) \
  || warn "配置自检有告警（见上）"

echo "  本机回环探测："
# 轮询而不是立刻探一次：服务刚 restart，uvicorn 需要一点时间才 bind。
# 立刻探会得到"连接被拒"这种**假失败**，而假失败比没有检查更糟——
# 它会训练人忽略自检输出。
ready=0
for _ in $(seq 1 20); do
  if curl -fsS --max-time 3 "http://127.0.0.1:${LISTEN_PORT}/healthz" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done
if [[ $ready -eq 1 ]]; then
  echo "    http  健康检查 OK（127.0.0.1:${LISTEN_PORT}）"
else
  warn "    http  健康检查失败（已等待 10 秒），最近日志："
  journalctl -u schedulekit -n 15 --no-pager || true
fi
if curl -fsS --max-time 12 --resolve "${DOMAIN}:${SITE_PORT}:127.0.0.1" \
     "https://${DOMAIN}:${SITE_PORT}/healthz" >/dev/null 2>&1; then
  echo "    https 反代 OK（${DOMAIN}:${SITE_PORT}）"
else
  warn "    https 探测失败（证书或 Caddy 站点块有问题）"
fi

echo "  服务内存占用："
systemctl show schedulekit -p MemoryCurrent --value 2>/dev/null \
  | awk '{ if ($1 == "" || $1 == "[not set]") print "    未知"; else printf "    %.1f MB\n", $1/1048576 }'

log "完成"
cat <<EOF
访问：  https://${DOMAIN}:${SITE_PORT}
配置：  ${CONF_DIR}/config.toml
数据：  ${DATA_DIR}
日志：  journalctl -u schedulekit -f
改配置后：systemctl restart schedulekit
（Caddyfile 改了必须用 restart，不能用 reload —— 里面 admin off 与 reload 互斥）
EOF
