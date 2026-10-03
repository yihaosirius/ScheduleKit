#!/usr/bin/env bash
# ============================================================================
# 拆除服务器上的旧 ScheduleKit（上一版）实例。
#
# 为什么是一份脚本而不是一串手工命令：
#   * 拆除是不可逆操作，必须**先归档再删除**，而且要能复核归档是否完整。
#   * 有几处容易误伤：证书目录属主是 schedulekit、但正在被 caddy 读取；
#     直接删 /etc/schedulekit 会让旧 Caddy 站点块在 reload 时失去证书。
#     所以证书先搬到 /etc/schedulekit-tls，其余才删。
#
# 明确**不动**的东西（属于别的项目）：
#   docker: cloudreve / postgresql / redis / portainer
#   caddy-webdav.service（Cloudreve WebDAV 反代，:8079）
#   /etc/caddy/Caddyfile（只由后续部署改写 ScheduleKit 站点块）
#   acme.sh 配置与 cron（证书续期继续用）
#
# 用法：
#   bash deploy/legacy-teardown.sh --archive-only   # 只归档，不删
#   bash deploy/legacy-teardown.sh                  # 归档 + 拆除
# ============================================================================
set -euo pipefail

ARCHIVE_DIR="/root/legacy-schedulekit"
ARCHIVE_ONLY=0
[[ "${1:-}" == "--archive-only" ]] && ARCHIVE_ONLY=1

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "必须以 root 运行"; exit 1; }

STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$ARCHIVE_DIR"
chmod 700 "$ARCHIVE_DIR"

# --------------------------------------------------------------------------- #
# 1. 归档
# --------------------------------------------------------------------------- #
log "归档到 $ARCHIVE_DIR"

# 1.1 数据库：既留可读的 SQL 文本，也留原始文件（图片路径引用需要原库）
if [[ -f /var/lib/schedulekit/schedulekit.db ]]; then
  cp -a /var/lib/schedulekit/schedulekit.db "$ARCHIVE_DIR/schedulekit.db.$STAMP"

  # 用 Python 的可读 dump，而不是 .dump：表结构可读、便于日后翻查
  # 旧数据到底长什么样（决定要不要恢复某几条）。
  /usr/bin/python3 - "$ARCHIVE_DIR/schedulekit.db.$STAMP" \
                     "$ARCHIVE_DIR/data-dump.$STAMP.txt" <<'PY'
import sqlite3, sys, json
db, out = sys.argv[1], sys.argv[2]
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
tables = [r[0] for r in con.execute(
    "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
with open(out, "w", encoding="utf-8") as fh:
    for t in tables:
        rows = con.execute(f"select * from {t}").fetchall()
        fh.write(f"\n===== {t} ({len(rows)} 行) =====\n")
        for r in rows:
            fh.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
    fh.write("\n===== schema =====\n")
    for r in con.execute("select sql from sqlite_master where sql is not null order by name"):
        fh.write(r[0] + ";\n")
print(f"dump 写出：{out}")
PY
else
  warn "未找到 /var/lib/schedulekit/schedulekit.db"
fi

# 1.2 上传的原图（旧库里的草稿按路径引用它们）
if [[ -d /var/lib/schedulekit/uploads ]]; then
  tar -czf "$ARCHIVE_DIR/uploads.$STAMP.tar.gz" -C /var/lib/schedulekit uploads
  rm -rf "$ARCHIVE_DIR/uploads.extracted"
  mkdir -p "$ARCHIVE_DIR/uploads.extracted"
  tar -xzf "$ARCHIVE_DIR/uploads.$STAMP.tar.gz" -C "$ARCHIVE_DIR/uploads.extracted"
fi

# 1.3 配置与部署产物
[[ -f /etc/schedulekit/config.toml ]] \
  && cp -a /etc/schedulekit/config.toml "$ARCHIVE_DIR/config.toml.$STAMP"
[[ -f /etc/systemd/system/schedulekit.service ]] \
  && cp -a /etc/systemd/system/schedulekit.service "$ARCHIVE_DIR/schedulekit.service.$STAMP"

# 1.4 DuckDNS 脚本：里面藏着 token，归档时顺手记下它用过的域名，
#     但不要把 token 明文留在归档里（归档是 0600，仍然按最小暴露处理）。
if [[ -f /etc/schedulekit/duckdns-update.sh ]]; then
  grep -o 'domains=[A-Za-z0-9]*' /etc/schedulekit/duckdns-update.sh \
    > "$ARCHIVE_DIR/duckdns-domain.txt" 2>/dev/null || true
fi

# 1.5 旧源码里的设计文档（唯一有长期价值的部分：PLAN.md 与 docs/）
if [[ -d /root/schedule_kit ]]; then
  mkdir -p "$ARCHIVE_DIR/design-notes"
  cp -a /root/schedule_kit/PLAN.md "$ARCHIVE_DIR/design-notes/" 2>/dev/null || true
  cp -a /root/schedule_kit/docs/. "$ARCHIVE_DIR/design-notes/" 2>/dev/null || true
  cp -a /root/schedule_kit/README.md "$ARCHIVE_DIR/design-notes/README.legacy.md" 2>/dev/null || true
fi

log "归档完成，清单："
ls -la "$ARCHIVE_DIR"
du -sh "$ARCHIVE_DIR"

if [[ $ARCHIVE_ONLY -eq 1 ]]; then
  log "只归档模式，未做任何删除。"
  exit 0
fi

# --------------------------------------------------------------------------- #
# 2. 停服务（先停应用再停 timer，避免 timer 把刚删的东西又建回来）
# --------------------------------------------------------------------------- #
log "停止旧服务"
systemctl disable --now schedulekit.service 2>/dev/null || warn "schedulekit.service 未在运行"
systemctl disable --now schedulekit-duckdns.timer 2>/dev/null || true
rm -f /etc/systemd/system/schedulekit.service \
      /etc/systemd/system/schedulekit-duckdns.service \
      /etc/systemd/system/schedulekit-duckdns.timer
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

# 确认 8000 端口已释放（这是新服务要用的端口）
if ss -lntp 2>/dev/null | grep -q '127.0.0.1:8000'; then
  warn "127.0.0.1:8000 仍被占用，请检查："
  ss -lntp | grep '127.0.0.1:8000'
  exit 1
fi

# --------------------------------------------------------------------------- #
# 3. 证书搬家
#
# 顺序很关键：证书目录现在的属主是 schedulekit，而 caddy 进程正在读它。
# 先建新目录并把文件搬过去，再改 Caddyfile；否则中间任何一次 reload
# 都会因为证书不存在而整个 Caddy 起不来（会连带影响别的站点）。
# --------------------------------------------------------------------------- #
log "迁移 TLS 证书到 /etc/schedulekit-tls"
install -d -m 0755 -o root -g root /etc/schedulekit-tls
if [[ -d /etc/schedulekit/certs ]]; then
  cp -a /etc/schedulekit/certs/. /etc/schedulekit-tls/
fi
# 私钥只给 caddy 读；Caddyfile 里的 tls 指令由 caddy 用户读取，
# 因此这里必须让 caddy 能读私钥，否则站点起不来。
chown -R root:caddy /etc/schedulekit-tls
chmod 0755 /etc/schedulekit-tls
chmod 0644 /etc/schedulekit-tls/fullchain.pem 2>/dev/null || true
chmod 0640 /etc/schedulekit-tls/privkey.pem   2>/dev/null || true
ls -la /etc/schedulekit-tls

# --------------------------------------------------------------------------- #
# 4. 删除应用与数据
# --------------------------------------------------------------------------- #
log "删除旧应用与数据"
rm -rf /opt/schedulekit
rm -rf /var/lib/schedulekit
rm -rf /etc/schedulekit
rm -rf /root/schedule_kit

if id schedulekit >/dev/null 2>&1; then
  userdel schedulekit 2>/dev/null || warn "userdel 失败（进程可能仍在占用）"
fi

# --------------------------------------------------------------------------- #
# 5. 校验
# --------------------------------------------------------------------------- #
log "拆除后校验"
for p in /opt/schedulekit /var/lib/schedulekit /etc/schedulekit /root/schedule_kit; do
  [[ -e $p ]] && warn "仍存在：$p" || echo "已删除：$p"
done
id schedulekit >/dev/null 2>&1 && warn "用户 schedulekit 仍存在" || echo "已删除用户：schedulekit"
echo "--- 监听端口 ---"
ss -lntp | grep -E ':(80|443|8000|8078|8079|8443|8001|6888)\b' || true
echo "--- 归档 ---"
ls -la "$ARCHIVE_DIR"
echo
echo "注意：Caddyfile 尚未改写，ScheduleKit 站点块现在指向不存在的证书路径。"
echo "      下一步部署会重写该站点块并 reload；在那之前不要执行 caddy reload。"
