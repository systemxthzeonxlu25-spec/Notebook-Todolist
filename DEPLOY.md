# VPS 部署清单（执行前逐项检查）

本文仅提供部署文件与待执行命令。本项目开发过程中没有连接 VPS，也没有执行以下远程操作。示例面向 Ubuntu 24.04、Python 3.12、systemd；未知发行版先确认兼容性，不替换系统 Python。

应用使用一个 Gunicorn worker、两个线程，只监听 `127.0.0.1:18765`。独立的 `notebook-caddy` 服务提供 HTTPS，独立备份定时器每天备份 SQLite。任何人均可注册；如需临时关闭新注册，可将环境文件中的 `REGISTER_ENABLED` 改为 `false`，再重启本项目的应用服务。

**约束：不修改已有代理、已有 Caddy 或防火墙规则；不停止、重启已有服务。若域名、端口、内存或服务归属无法确认，就停止上线步骤。不要整段盲目粘贴。** 所有 `notes.example.com`、邮箱和版本号均需自行替换。

带有 `set -euo pipefail` 的括号代码块会在任一步失败时停止该块。出现错误后不要继续下一块，先查明失败原因；不要删除失败检查来强行继续。本文的恢复命令对应下面约定的默认数据库路径，修改路径后必须同步调整恢复清单。

## 1. 第一件事：定位并备份现有代理、服务和防火墙配置

先保持当前 SSH 连接，确认有第二条 SSH 会话或 VPS 控制台可用。以下检查只读取状态：

```bash
sudo systemctl list-units --type=service --state=running --no-pager
sudo ss -ltnup
```

从列表确认代理服务的真实名字，例如 `xray.service` 或 `sing-box.service`；不要按例子猜。设置变量后查看配置文件的位置：

如果代理由容器或管理面板启动，先按其实际管理方式定位并备份配置与挂载数据；不能把 Docker 守护进程配置当成代理配置。无法确认配置位置时，暂停部署。

```bash
proxy_unit='请填实际代理服务名.service'
sudo systemctl show "$proxy_unit" -p FragmentPath -p DropInPaths -p ActiveState -p SubState -p MainPID
```

创建仅 root 可读的备份目录，并把服务定义保存在该目录；服务定义可能含有凭据，**不要把内容发到聊天、公开 issue 或 Git 仓库**。

```bash
deploy_backup_dir="$(sudo mktemp -d "/root/notebook-before-deploy-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
(
set -euo pipefail
sudo sh -c 'umask 077; systemctl cat "$1" > "$2/proxy-unit.txt"' sh "$proxy_unit" "$deploy_backup_dir"
sudo less "$deploy_backup_dir/proxy-unit.txt"
)
```

根据服务的 `ExecStart` 与实际配置，确认代理配置目录。若目录包含引用的证书、密钥或其他配置，连同被引用文件一并备份。下面目录必须替换成确认过的真实路径：

```bash
proxy_config_dir='/请填确认过的代理配置目录'
(
set -euo pipefail
sudo test -d "$proxy_config_dir"
sudo tar --acls --xattrs -czf "$deploy_backup_dir/proxy-config.tar.gz" -C / "${proxy_config_dir#/}"
sudo tar --acls --xattrs -czf "$deploy_backup_dir/systemd-config.tar.gz" -C / etc/systemd/system
sudo sh -c '
  set -eu
  umask 077
  if command -v nft >/dev/null 2>&1; then nft list ruleset > "$1/nft-ruleset.conf"; fi
  if command -v iptables-save >/dev/null 2>&1; then iptables-save > "$1/iptables-v4.rules"; fi
  if command -v ip6tables-save >/dev/null 2>&1; then ip6tables-save > "$1/iptables-v6.rules"; fi
  if [ -d /etc/ufw ]; then cp -a /etc/ufw "$1/ufw-config"; fi
  if [ -d /etc/firewalld ]; then cp -a /etc/firewalld "$1/firewalld-config"; fi
  mkdir -m 0700 "$1/firewall-files"
  for config in /etc/nftables.conf /etc/iptables /etc/default/ufw /etc/default/netfilter-persistent; do
    if [ -e "$config" ]; then cp -a --parents "$config" "$1/firewall-files/"; fi
  done
  ss -ltnup > "$1/listeners-before.txt"
  systemctl show "$2" -p ActiveState -p SubState -p MainPID > "$1/proxy-before.txt"
' sh "$deploy_backup_dir" "$proxy_unit"
sudo ls -lh "$deploy_backup_dir"
)
```

备份失败时不要继续；保留备份目录的路径。先用当前代理客户端访问一个日常可用的网站，确认代理正常，并记录结果。只检查 `systemctl active` 不能代替真实代理连接测试。本文不会自动恢复或覆盖旧配置。

## 2. 检查资源、端口、已有 Caddy 与 DNS

```bash
cat /etc/os-release
python3.12 --version
free -m
df -h / /var/lib /var/backups
sudo ss -ltnup
sudo systemctl list-unit-files --no-legend | awk 'tolower($1) ~ /caddy/ {print}'
pgrep -x caddy || true
getent ahosts notes.example.com
```

上线前必须满足：

- 已记录代理的服务名、配置备份和真实连接测试结果。
- 有足够剩余磁盘空间，包含数据库、上传文件与至少 14 天备份。建议至少 1 GiB 可用空间并持续监测增长。
- `free -m` 的 `available` 建议至少 512 MiB；本项目的应用、Caddy、备份内存上限分别为 256/128/64 MiB。若不足，先评估，不能通过关闭代理或修改其限制腾空间。
- `127.0.0.1:18765` 没有被占用。
- 80/TCP、443/TCP、443/UDP 没有任何现有监听；不存在已有 Caddy 服务、进程或由容器运行的 Caddy。若存在就停在这里，另行规划域名与现有代理集成，不能替换其配置。
- 域名 A 记录指向 VPS；若有 AAAA 记录，IPv6 也必须可达并指向同一 VPS。没有域名时可以完成本地检查，暂不启动 HTTPS 服务。

### 需要你确认后手动开放的端口

| 端口 | 用途 | 要求 |
| --- | --- | --- |
| 80/TCP | HTTP 跳转、ACME 证书验证 | 域名部署需要，先确认现有代理未使用 |
| 443/TCP | HTTPS、ACME TLS 验证 | 必需，先确认现有代理未使用 |
| 443/UDP | HTTP/3 | 可选；不开放仍可使用 HTTPS |
| 18765/TCP | Flask 内部上游 | **禁止对公网开放**，只监听 127.0.0.1 |

这里只列需求，不提供自动修改防火墙、云安全组、路由或端口转发的命令。现有 SSH 和代理端口必须保持原状。证书签发还需要允许 DNS 解析与出站 HTTPS。

## 3. 准备运行时与独立账号

若 Python 3.12、venv、Git、curl、CA 证书已具备，跳过安装。Ubuntu 24.04 缺包时先模拟，确认不会删除或升级已有代理依赖：

```bash
sudo apt-get update
sudo apt-get --simulate install --no-upgrade --no-install-recommends python3.12 python3.12-venv git curl ca-certificates
```

只有确认模拟结果没有升级/移除已有软件、没有影响代理依赖后，才运行：

```bash
sudo env NEEDRESTART_MODE=l apt-get install --no-upgrade --no-install-recommends python3.12 python3.12-venv git curl ca-certificates
```

`NEEDRESTART_MODE=l` 仅列出需要重启的服务，不自动重启它们。不要执行全系统升级，不添加第三方 PPA，不安装会自动接管 80/443 的 `caddy` 或 `nginx` 软件包。

检查下列名字和路径是否已存在；已有同名用户、目录或服务时先核实归属，不覆盖：

```bash
getent passwd notebook || true
getent passwd notebook-caddy || true
sudo ls -ld /opt/notebook-todolist /etc/notebook-todolist.env /etc/notebook-caddy /var/lib/notebook-todolist /var/lib/notebook-caddy /var/backups/notebook-todolist 2>/dev/null || true
sudo systemctl list-unit-files 'notebook-*' --no-pager
```

全部确认为本次新建且没有冲突后：

```bash
(
set -euo pipefail
for account in notebook notebook-caddy; do
  if getent passwd "$account" >/dev/null || getent group "$account" >/dev/null; then
    echo '同名用户或用户组已存在，停止并核实归属。' >&2
    exit 1
  fi
done
for path in /opt/notebook-todolist /etc/notebook-todolist.env /etc/notebook-caddy /var/lib/notebook-todolist /var/lib/notebook-caddy /var/backups/notebook-todolist; do
  if sudo test -e "$path" || sudo test -L "$path"; then
    echo '目标路径已经存在，停止并核实归属。' >&2
    exit 1
  fi
done
for unit in notebook-todolist.service notebook-caddy.service notebook-backup.service notebook-backup.timer; do
  state="$(sudo systemctl show "$unit" --property=LoadState --value 2>/dev/null || true)"
  test "$state" = not-found
  for unit_dir in /etc/systemd/system /run/systemd/system /usr/lib/systemd/system; do
    sudo test ! -e "$unit_dir/$unit"
    sudo test ! -L "$unit_dir/$unit"
    sudo test ! -e "$unit_dir/$unit.d"
  done
done
sudo useradd --system --user-group --home-dir /var/lib/notebook-todolist --shell /usr/sbin/nologin notebook
sudo useradd --system --user-group --home-dir /var/lib/notebook-caddy --shell /usr/sbin/nologin notebook-caddy
sudo sh -eu -c '
  umask 022
  git clone --branch main --single-branch https://github.com/systemxthzeonxlu25-spec/Notebook-Todolist.git /opt/notebook-todolist
  python3.12 -m venv /opt/notebook-todolist/.venv
  /opt/notebook-todolist/.venv/bin/python -m pip install -r /opt/notebook-todolist/requirements.txt
'
sudo /opt/notebook-todolist/.venv/bin/python -m pip check
sudo install -d -o notebook -g notebook -m 0700 /var/lib/notebook-todolist /var/lib/notebook-todolist/uploads /var/backups/notebook-todolist
sudo install -d -o notebook-caddy -g notebook-caddy -m 0700 /var/lib/notebook-caddy
)
```

应用代码和虚拟环境由 root 管理、应用账号只读；可写数据全部位于 `/var/lib/notebook-todolist`，不得把数据库放进 Git checkout。

## 4. 创建环境文件，安全生成会话密钥

这条命令使用排他创建；已有环境文件时会报错，不会覆盖密钥。密钥不经命令行参数、终端输出或 shell 历史传递：

```bash
sudo /opt/notebook-todolist/.venv/bin/python - <<'PY'
import os
import secrets

settings = {
    'APP_ENV': 'production',
    'SECRET_KEY': secrets.token_urlsafe(48),
    'DATABASE_PATH': '/var/lib/notebook-todolist/notebook.sqlite3',
    'UPLOAD_FOLDER': '/var/lib/notebook-todolist/uploads',
    'REGISTER_ENABLED': 'true',
    'SESSION_COOKIE_SECURE': 'true',
    'TRUST_PROXY': 'true',
    'TZ': 'Asia/Shanghai',
    'AI_ALLOWED_HOSTS': 'api.deepseek.com,api.openai.com',
}
descriptor = os.open('/etc/notebook-todolist.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w') as stream:
    for name, value in settings.items():
        stream.write(f'{name}={value}\n')
PY
```

systemd 以 root 读取这个 `0600` 文件并注入服务环境。不要把它提交到 Git，不要 `cat` 后截图，不要在其中配置个人 AI Key：AI Key 只由用户浏览器保存并在请求头中临时发送。`TZ` 只作用于应用，不改变服务器或已有代理的时区。`TRUST_PROXY=true` 仅在原始连接来自本机回环地址时信任一层代理的客户端 IP 与协议，用于 Caddy 后的真实 IP 限速；本地开发默认 `false`。`AI_ALLOWED_HOSTS` 只填写管理员信任的 AI 服务域名，不填写密钥、URL 路径或内网地址。

## 5. 安装并检查应用服务

只安装本项目的新 unit；同名文件如果已经存在，先停止部署步骤并核实，不覆盖已有服务：

```bash
(
set -euo pipefail
sudo test ! -e /etc/systemd/system/notebook-todolist.service
sudo test ! -L /etc/systemd/system/notebook-todolist.service
test "$(sudo systemctl show notebook-todolist.service --property=LoadState --value 2>/dev/null || true)" = not-found
sudo install -m 0644 /opt/notebook-todolist/deploy/notebook-todolist.service /etc/systemd/system/notebook-todolist.service
sudo systemd-analyze verify /etc/systemd/system/notebook-todolist.service
sudo systemctl daemon-reload
sudo systemctl enable --now notebook-todolist.service
sudo systemctl status notebook-todolist.service --no-pager
curl --retry 10 --retry-connrefused --retry-delay 1 --max-time 5 --fail --silent --show-error http://127.0.0.1:18765/healthz
sudo ss -ltnp 'sport = :18765'
)
```

健康接口应返回 `{"status":"ok"}`，端口必须显示 `127.0.0.1:18765`。若启动失败，仅查看新服务：`sudo journalctl -u notebook-todolist.service -n 60 --no-pager`。不要输出环境变量或开启请求体、请求头日志。HTTPS 尚未配置时安全 Cookie 不会随 HTTP 发送，先完成健康检查，等 HTTPS 就绪后验证登录。

## 6. 单独安装 Caddy 二进制，不碰已有服务

仅在第 2 步所有检查通过时继续。从 [Caddy 官方 Releases](https://github.com/caddyserver/caddy/releases) 选定当前稳定版本，填入**不带 v 前缀**的版本号；以下命令下载 Linux amd64/arm64 对应文件，并按官方 HTTPS 发布的清单验证。64 位十六进制摘要使用 SHA-256，128 位使用 SHA-512；缺少目标条目、格式异常或校验失败都会停止安装。不要关闭 TLS 校验、跳过校验和或执行不明安装脚本。

```bash
(
set -euo pipefail
caddy_version='请填官方稳定版本号'
[[ "$caddy_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
sudo test ! -e /usr/local/bin/notebook-caddy
sudo test ! -L /usr/local/bin/notebook-caddy
sudo test ! -e /etc/notebook-caddy
sudo test ! -L /etc/notebook-caddy
caddy_units="$(sudo systemctl list-unit-files --no-legend | awk 'tolower($1) ~ /caddy/ {print}')"
test -z "$caddy_units"
if pgrep -x caddy >/dev/null || pgrep -x notebook-caddy >/dev/null; then
  echo '已存在 Caddy 进程，停止并核实，不修改已有服务。' >&2
  exit 1
fi
case "$(uname -m)" in
  x86_64) caddy_arch=amd64 ;;
  aarch64) caddy_arch=arm64 ;;
  *) echo '请先确认此架构的官方二进制支持'; false ;;
esac
caddy_download_dir="$(mktemp -d)"
caddy_archive="caddy_${caddy_version}_linux_${caddy_arch}.tar.gz"
caddy_checksums="caddy_${caddy_version}_checksums.txt"
curl --fail --show-error --location --proto '=https' --tlsv1.2 "https://github.com/caddyserver/caddy/releases/download/v${caddy_version}/${caddy_archive}" -o "$caddy_download_dir/$caddy_archive"
curl --fail --show-error --location --proto '=https' --tlsv1.2 "https://github.com/caddyserver/caddy/releases/download/v${caddy_version}/${caddy_checksums}" -o "$caddy_download_dir/$caddy_checksums"
python3.12 - "$caddy_download_dir" "$caddy_archive" "$caddy_checksums" <<'PY'
import hashlib
from pathlib import Path
import re
import sys

directory, archive, checksums = sys.argv[1:]
root = Path(directory)
entries = {}
for line in (root / checksums).read_text().splitlines():
    parts = line.split()
    if len(parts) == 2:
        entries[parts[1].lstrip('*')] = parts[0]
expected = entries.get(archive, '')
if not re.fullmatch(r'(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{128})', expected):
    raise SystemExit('官方清单缺少目标文件或摘要格式未知，停止安装')
digest = hashlib.sha256() if len(expected) == 64 else hashlib.sha512()
with (root / archive).open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
if digest.hexdigest() != expected.lower():
    raise SystemExit('校验失败，禁止安装或启动该文件')
print(f'Caddy {digest.name.upper()} 校验通过')
PY
tar -xzf "$caddy_download_dir/$caddy_archive" -C "$caddy_download_dir" caddy
sudo install -o root -g root -m 0755 "$caddy_download_dir/caddy" /usr/local/bin/notebook-caddy
sudo install -d -o root -g notebook-caddy -m 0750 /etc/notebook-caddy
sudo install -o root -g notebook-caddy -m 0640 /opt/notebook-todolist/deploy/Caddyfile.example /etc/notebook-caddy/Caddyfile
)
```

上面整块成功后再编辑本项目的独立配置：

```bash
sudoedit /etc/notebook-caddy/Caddyfile
```

在独立配置中替换 `YOUR_EMAIL@example.com` 与 `notes.example.com`，保留 `admin off` 避免占用 Caddy 管理端口。不要编辑 `/etc/caddy/Caddyfile`。先验证配置，再次检查端口：

```bash
sudo -u notebook-caddy env XDG_DATA_HOME=/var/lib/notebook-caddy XDG_CONFIG_HOME=/var/lib/notebook-caddy /usr/local/bin/notebook-caddy validate --config /etc/notebook-caddy/Caddyfile --adapter caddyfile
sudo ss -H -ltn '( sport = :80 or sport = :443 )'
sudo ss -H -lun 'sport = :443'
```

后两条命令必须均无输出。配置校验成功、域名正确、入站端口已由你确认后手动放行，并且代理仍正常时，安装专用服务：

```bash
(
set -euo pipefail
sudo -u notebook-caddy env XDG_DATA_HOME=/var/lib/notebook-caddy XDG_CONFIG_HOME=/var/lib/notebook-caddy /usr/local/bin/notebook-caddy validate --config /etc/notebook-caddy/Caddyfile --adapter caddyfile
test -z "$(sudo ss -H -ltn '( sport = :80 or sport = :443 )')"
test -z "$(sudo ss -H -lun 'sport = :443')"
sudo test ! -e /etc/systemd/system/notebook-caddy.service
sudo test ! -L /etc/systemd/system/notebook-caddy.service
test "$(sudo systemctl show notebook-caddy.service --property=LoadState --value 2>/dev/null || true)" = not-found
sudo install -m 0644 /opt/notebook-todolist/deploy/notebook-caddy.service /etc/systemd/system/notebook-caddy.service
sudo systemd-analyze verify /etc/systemd/system/notebook-caddy.service
sudo systemctl daemon-reload
sudo systemctl enable --now notebook-caddy.service
sudo systemctl status notebook-caddy.service --no-pager
curl --fail --silent --show-error https://notes.example.com/healthz
)
```

证书签发可能需要短暂等待；错误只查 `sudo journalctl -u notebook-caddy.service -n 60 --no-pager`。不要通过停止代理、修改防火墙默认策略、改用不验证证书等方式绕过问题。

## 7. 开启每日 SQLite 备份

脚本使用 SQLite 的在线备份 API，即使源库处于 WAL 模式也会生成可独立恢复的数据库文件；完成完整性检查后再原子重命名。失败不会删除旧备份，重复运行通过文件锁互斥。只清理本脚本命名的、超过 14 天的快照，目录权限 `0700`、文件权限 `0600`。

备份连接以 `mode=ro` 打开数据库，但 systemd 必须允许数据目录写入，因为 SQLite 读取 WAL 数据库时可能需要创建 WAL/SHM 辅助文件；不能把这个目录重新改为只读挂载。

```bash
(
set -euo pipefail
sudo test ! -e /etc/systemd/system/notebook-backup.service
sudo test ! -e /etc/systemd/system/notebook-backup.timer
sudo test ! -L /etc/systemd/system/notebook-backup.service
sudo test ! -L /etc/systemd/system/notebook-backup.timer
test "$(sudo systemctl show notebook-backup.service --property=LoadState --value 2>/dev/null || true)" = not-found
test "$(sudo systemctl show notebook-backup.timer --property=LoadState --value 2>/dev/null || true)" = not-found
sudo install -m 0644 /opt/notebook-todolist/deploy/notebook-backup.service /etc/systemd/system/notebook-backup.service
sudo install -m 0644 /opt/notebook-todolist/deploy/notebook-backup.timer /etc/systemd/system/notebook-backup.timer
sudo systemd-analyze verify /etc/systemd/system/notebook-backup.service /etc/systemd/system/notebook-backup.timer
sudo systemctl daemon-reload
sudo systemctl start notebook-backup.service
sudo journalctl -u notebook-backup.service -n 10 --no-pager
sudo ls -lh /var/backups/notebook-todolist
sudo systemctl enable --now notebook-backup.timer
sudo systemctl list-timers notebook-backup.timer --no-pager
)
```

计划时间为**服务器已有时区**的每天 03:20，最多随机延后 15 分钟；服务器错过运行时间后会补跑。没有修改服务器时区。

这些快照只包含 SQLite；`/var/lib/notebook-todolist/uploads` 原始附件及 `/etc/notebook-todolist.env` 需另外加密备份。建议将备份复制到异机，并定期试恢复；同机备份不能防 VPS 或磁盘丢失。

### 恢复数据库

先确认目标快照；不要将公开下载链接用于包含私人笔记的备份。以下整块先在数据目录暂存并验证快照，验证成功后才暂停本项目的应用和备份任务。任一步失败都会停止，旧数据库不会因复制失败而被空库代替；其他服务保持运行：

```bash
chosen_snapshot='/var/backups/notebook-todolist/请填快照文件.sqlite3'
sudo bash -euo pipefail -s -- "$chosen_snapshot" <<'RESTORE'
snapshot="$1"
case "$snapshot" in
  /var/backups/notebook-todolist/notebook-*.sqlite3) ;;
  *) echo '请选择备份目录中由本项目生成的快照。' >&2; exit 1 ;;
esac
test -f "$snapshot"
test ! -L "$snapshot"
staged="$(mktemp /var/lib/notebook-todolist/.restore-stage.XXXXXX)"
trap 'if [ -n "$staged" ]; then rm -f -- "$staged"; fi' EXIT
install -o notebook -g notebook -m 0600 "$snapshot" "$staged"
/opt/notebook-todolist/.venv/bin/python - "$staged" <<'PY'
from pathlib import Path
import sqlite3
import sys

snapshot = Path(sys.argv[1]).resolve(strict=True)
# 本脚本的备份是独立单文件，immutable 用于检查这份暂存快照，不用于活动数据库。
connection = sqlite3.connect(snapshot.as_uri() + '?mode=ro&immutable=1', uri=True)
try:
    if connection.execute('PRAGMA quick_check').fetchone() != ('ok',):
        raise SystemExit('快照完整性检查失败；尚未停服或改动现有数据')
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'users', 'notes', 'courses', 'cards', 'todos'} <= tables:
        raise SystemExit('快照缺少应用数据表；尚未停服或改动现有数据')
finally:
    connection.close()
PY
systemctl stop notebook-backup.timer notebook-backup.service notebook-todolist.service
for unit in notebook-backup.service notebook-todolist.service; do
  if systemctl is-active --quiet "$unit"; then
    echo '新应用或备份仍在运行，不能替换数据库。' >&2
    exit 1
  fi
done
recovery_dir="$(mktemp -d /var/lib/notebook-todolist/before-restore.XXXXXX)"
chown notebook:notebook "$recovery_dir"
for path in /var/lib/notebook-todolist/notebook.sqlite3 /var/lib/notebook-todolist/notebook.sqlite3-wal /var/lib/notebook-todolist/notebook.sqlite3-shm; do
  if [ -f "$path" ]; then mv -- "$path" "$recovery_dir/"; fi
done
mv -- "$staged" /var/lib/notebook-todolist/notebook.sqlite3
staged=''
systemctl start notebook-todolist.service
curl --retry 10 --retry-connrefused --retry-delay 1 --max-time 5 --fail --silent --show-error http://127.0.0.1:18765/healthz
echo '已替换数据库。先登录核对账号和笔记，确认后再启动备份 timer。'
RESTORE
```

替换前的数据库和 WAL/SHM 被移入 `before-restore.*` 恢复目录，便于撤销。健康接口只验证应用存活；**必须登录核对账号、笔记和待办内容**，确认恢复正确后再运行：

```bash
sudo systemctl start notebook-backup.timer
```

数据库恢复不会回滚附件文件，恢复旧快照时需配套检查附件备份。如果恢复途中出错，先保留暂存/恢复目录并排查，不要手动启动一个缺失数据库文件的应用。

## 8. 上线验收与代理复查

1. HTTPS 健康接口返回成功；浏览器中注册两个账号，分别创建笔记，确认不能互相访问；测试退出、重新登录及手机添加到主屏幕。
2. 新建带 `- [ ] 作业` 和 `==重点==` 的笔记，检查今日待办、复习卡、课程分类；测试图片上传与访问权限。
3. 配置自己的 AI 服务并做一次小请求；不在服务器环境文件、日志或 Git 中保存 AI Key。
4. 确认后台只监听 `127.0.0.1:18765`，不出现 `0.0.0.0:18765` 或 `[::]:18765`。
5. 再次查看既有代理状态和监听端口，并通过原来的代理客户端实际访问网站，和第 1 步结果比较。若有异常，立即按下文仅停本项目新增服务。
6. 查看 `systemctl status notebook-todolist notebook-caddy`、`free -m` 和备份计时器；若触发内存上限，先暂停新应用并评估资源，不提高到可能挤占代理的无限制配置。

```bash
sudo systemctl show "$proxy_unit" -p ActiveState -p SubState -p MainPID
sudo ss -ltnup
free -m
```

## 9. 仅回滚本项目

若本项目导致资源压力、端口冲突或异常，只停止本项目创建的服务：

```bash
sudo systemctl disable --now notebook-backup.timer notebook-caddy.service notebook-todolist.service
sudo systemctl stop notebook-backup.service
```

这会释放本项目占用的端口和资源。确认原代理仍正常；不重启代理、不清空防火墙、不自动恢复覆盖已有配置。

需要卸载新服务定义时，先确认下列文件就是本次安装的文件，再逐项删除：

```bash
sudo rm /etc/systemd/system/notebook-todolist.service /etc/systemd/system/notebook-caddy.service /etc/systemd/system/notebook-backup.service /etc/systemd/system/notebook-backup.timer
sudo systemctl daemon-reload
```

保留 `/var/lib/notebook-todolist`、`/var/backups/notebook-todolist`、环境文件、代码和 Caddy 证书目录，直到确认已经有完整备份并不再需要。本文不提供自动删除用户数据的命令。

## 本地开发

本地使用仓库 README 的 `.env.example` 和虚拟环境配置，运行 `python app.py`。生产不要运行 Flask 调试服务器，不启用 `DEBUG`，不要把生产 `SESSION_COOKIE_SECURE=true` 的配置原样用于本地 HTTP 登录。
