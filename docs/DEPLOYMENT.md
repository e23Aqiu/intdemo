# IntDemo v0.2 部署手册

## 1. 前置条件

- 腾讯轻量应用服务器 Ubuntu 24.04，建议至少 2 核 2 GB。
- 腾讯防火墙允许全部 IPv4 来源访问 TCP 80、TCP/UDP 443；数据库和 API
  容器端口不对宿主机发布。
- 域名方案先配置 A/AAAA；中国大陆实例正式以域名提供服务前完成 ICP 备案。
- 服务器只需 Docker Engine 与 Compose 插件，不在宿主机安装 Python 或
  PostgreSQL。

安装 Docker：

```bash
sudo bash ./deploy/scripts/install-docker-ubuntu.sh
```

该脚本使用 Docker 官方 Ubuntu apt 仓库。安装后仍需同时配置腾讯防火墙和
Docker 的 `DOCKER-USER` 链：

```bash
sudo bash ./deploy/scripts/configure-docker-firewall.sh public allow-http
```

该脚本只过滤外部网卡进入 Docker 发布端口的流量，不影响容器访问外网。
`public` 模式允许任意 IPv4 客户端访问 HTTP/HTTPS。若以后需要重新限制
来源，可切换为 `private` 模式：

```bash
sudo bash ./deploy/scripts/configure-docker-firewall.sh \
  private 203.0.113.25/32 198.51.100.0/24 allow-http
```

## 2. 初始化配置和密钥

```bash
cp .env.example .env
editor .env
bash ./deploy/scripts/init-secrets.sh
```

三个机密文件位于 `deploy/secrets/`：

- `postgres_password.txt`
- `jwt_secret.txt`
- `offline_private_key.txt`（Base64 编码的 32 字节 Ed25519 私钥）

这些文件被 Git 忽略。迁移时必须加密复制，丢失离线签名私钥会使现有客户端
离线授权无法续签，丢失 JWT 密钥会使全部在线会话失效。

仓库中的 Python、PostgreSQL 和 Caddy 镜像固定到已验证的多架构 digest。
升级镜像时先在测试环境拉取、运行测试，再更新 tag 与 digest。
中国大陆服务器首次构建较慢时，可在 `.env` 设置
`PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple`；该值只用于
构建 API 镜像。

## 3. 方案 A：域名和公网证书

`.env`：

```dotenv
SERVER_ADDRESS=api.example.com
CADDY_BIND_ADDRESS=0.0.0.0
CADDYFILE_PATH=./deploy/caddy/Caddyfile.domain
```

启动：

```bash
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps
bash ./deploy/scripts/smoke-test.sh https://api.example.com
```

Caddy 自动申请和续期公网证书。客户端 `client-online.json`：

```json
{
  "base_url": "https://api.example.com",
  "ca_bundle": null,
  "channel": "test"
}
```

## 4. 方案 B：只有固定公网 IP

`.env`：

```dotenv
SERVER_ADDRESS=203.0.113.10
CADDY_BIND_ADDRESS=0.0.0.0
CADDYFILE_PATH=./deploy/caddy/Caddyfile.ip
```

启动后导出 Caddy 内部 CA 根证书：

```bash
docker compose up -d --build --wait
bash ./deploy/scripts/export-internal-ca.sh \
  ./deploy/generated/intdemo-caddy-root.crt
```

把根证书放进测试客户端包的 `certs/`，配置：

```json
{
  "base_url": "https://203.0.113.10",
  "ca_bundle": "certs/intdemo-caddy-root.crt",
  "channel": "test"
}
```

API 和 WebSocket 都显式加载该 CA；代码中没有 `verify=False` 或 HTTP 回退。
公网 IP 改变时必须重新签发并重新分发配置，因此正式环境优先域名方案。

## 5. 首次上线顺序

1. 启动阶段可以先使用 `private` 模式，仅允许管理员公网 IP。
2. 启动 Compose；`/api/v1/health/live` 应为 200。
3. `/api/v1/health/ready` 应为 503，原因是管理员尚未改密。
4. 使用 v0.2 客户端以 `admin / 123456` 登录并修改密码。
5. `health/ready` 变为 200 后，确认普通站点账号均处于启用状态。
6. 将腾讯防火墙和 `DOCKER-USER` 同时切换为全部 IPv4 可访问：

```bash
sudo bash ./deploy/scripts/configure-docker-firewall.sh \
  public allow-http
```

如需重新限制为多个测试公网 IP，可用逗号分隔：

```bash
sudo bash ./deploy/scripts/configure-docker-firewall.sh \
  private 203.0.113.25/32 \
  198.51.100.10/32,198.51.100.11/32,198.51.100.12/32
```

腾讯云轻量服务器防火墙和宿主机 `DOCKER-USER` 必须同时放行；只修改其中
一层仍会连接超时。客户端不绑定公网 IP；内置账号默认设备上限为 10000 台，
管理员可在账号管理中按账号调整。达到上限后新电脑会被拒绝登录，需先撤销
旧设备或提高上限。

Compose 默认仅绑定 IPv4 的 `0.0.0.0`。如使用 AAAA，必须调整
`CADDY_BIND_ADDRESS` 并用等价的 `ip6tables` 规则限制 IPv6 来源；未完成前
不要在腾讯防火墙开放公网 IPv6 443。

初始站点账号为 `luogang`、`taiping`、`daojiao`、`baoan`、`nantou`，
密码均为 `123456`，初始启用且要求首次改密。

## 6. 日常命令

```bash
docker compose ps
docker compose logs -f --tail=200 api caddy
docker compose exec -T backup /bin/bash /scripts/backup-now.sh
docker compose exec -T backup /bin/bash /scripts/restore-latest.sh
docker compose exec postgres pg_isready -U intdemo -d intdemo
```

PostgreSQL 没有宿主机端口映射；公网只有 Caddy 的 80/443。API 只有一个
Uvicorn worker，SQLAlchemy 连接池为 `5 + 5`。

## 7. 客户端便携包、安装包与更新发布

先安装 Python 构建依赖和 Inno Setup 6。域名安装包：

```powershell
winget install --id JRSoftware.InnoSetup --exact
.\scripts\build-installer.ps1 `
  -BaseUrl https://api.example.com `
  -Version 0.2.5
```

IP 私有 CA 包：

```powershell
.\scripts\build-installer.ps1 `
  -BaseUrl https://203.0.113.10 `
  -CaBundle .\intdemo-caddy-root.crt `
  -Version 0.2.5
```

脚本生成外置 `client-online.json` 和按当前用户安装的中文安装包。v0.2
不读取或迁移 v0.1 数据。仅重新组装已经验证过的 PyInstaller 目录时可加
`-SkipPyInstaller`。

需要同时生成免安装便携包和安装包时运行：

```powershell
.\scripts\build-releases.ps1 `
  -BaseUrl https://203.0.113.10 `
  -CaBundle .\intdemo-caddy-root.crt `
  -Version 0.2.5 `
  -DeltaFromVersion 0.2.4
```

便携包位于 `dist\portable\IntDemoOnline-Portable-0.2.5.zip`。用户必须
完整解压，不能只复制其中的 EXE，因为同目录的连接配置及 `certs` 私有
CA 证书也是 HTTPS 校验的一部分。安装包位于
`dist\installer\IntDemoOnline-Setup-0.2.5.exe`，从 0.2.4 升级的差异包位于
`dist\installer\IntDemoOnline-Patch-0.2.4-to-0.2.5.exe`。

后续发布测试通道更新：

```powershell
.\scripts\publish-update.ps1 `
  -Installer .\dist\installer\IntDemoOnline-Setup-0.2.5.exe `
  -DeltaInstaller .\dist\installer\IntDemoOnline-Patch-0.2.4-to-0.2.5.exe `
  -DeltaFromVersion 0.2.4 `
  -Version 0.2.5 `
  -Notes "本次更新说明" `
  -RemoteHost intdemo-test `
  -RemotePath /opt/intdemo/deploy/updates
```

Compose 将服务器的 `deploy/updates/` 同时只读挂载给 API 和 Caddy。
`/updates/test.json`、`/updates/stable.json` 由 API 根据
`X-IntDemo-Version` 或 `IntDemoUpdater/<版本>` User-Agent 动态选择包；
其余 `/updates/files/*` 仍由 Caddy 直接下载。只有当前版本精确匹配
`deltas.from_version` 才返回增量包，其余情况返回完整包。

`-LegacyDeltaPrimary` 仅作为旧发布命令的兼容参数保留，已经不会把增量包
写入公共清单顶层，新的发布命令无需使用。

1. 本地完整构建并执行安装、启动、卸载冒烟测试。
2. 生成 SHA-256 和 `test.json`。
3. 把安装包上传到服务器 `.incoming` 并核对大小及 SHA-256。
4. 安装包就位后原子替换 `test.json`，客户端才会看到新版本。

客户端只接受与 API 相同主机、相同 HTTPS 信任链下的更新地址，并在运行
安装包前核对清单声明的大小及 SHA-256。未购买 Windows 代码签名证书前，
其他电脑首次运行安装包可能显示“未知发布者”；这不应通过关闭
SmartScreen 或禁用 TLS 校验来规避，正式发布建议购买组织代码签名证书。

发布时加 `-Mandatory` 会生成强制更新清单。客户端检测到强制更新后不能
关闭提示窗口，必须下载并重启安装；普通更新提供“立即更新”“不再提示”和
“取消”。“不再提示”只对当前版本生效，用户仍可在“系统设置”中手动下载。
下载期间系统设置与弹窗会显示进度，其他业务页面会被锁定。

当前测试安装包使用 Inno Setup 6.7.3。该版本编译器会明确标注仅限
非商业用途；迁入公司正式商用前需购买 Inno Setup 商业许可证，或替换成
公司已经授权的 MSI/安装包工具。

## 8. 官方参考

- [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https)
- [Caddy `tls internal`](https://caddyserver.com/docs/caddyfile/directives/tls)
- [腾讯轻量服务器域名与备案说明](https://cloud.tencent.com/document/product/1207/81332)
