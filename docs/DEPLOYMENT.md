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
密码均为 `123456`，初始启用并要求首次改密。新安装时这些中心站默认为“未分配”，
由管理员直接管理；从 v1.1.x 经 `0014` 升级的既有中心站会保留迁移时写入的
“广深高速”关联，但该分类只有在创建显示名同为“广深高速”的路段管理员后才会
出现在可分配列表。升级和创建路段管理员都不会重置既有中心站的密码、登录状态、
设备登记或历史统计。`0015_detach_test_accounts` 会把测试账号的 `road_id` 清空，
并把不适用的“本路段”范围改为“本人”；不会改动密码、`token_version`、刷新会话
或设备登记。测试账号始终为“未分配”，只由全局管理员直接管理。

路段管理员的账号显示名就是路段分类名。归档路段管理员时保留分类和中心站关系；
永久删除最后一个路段管理员时，服务端删除该分类并把所属中心站改为“未分配”，
其中原为“本路段”的数据范围收窄为“本人”。之后即使新建同名路段管理员，也必须
由管理员重新分配中心站。v1.1.0/v1.1.1 管理端未携带路段字段时，新建的中心站按
“未分配”处理，旧客户端的登录与同步协议保持兼容。

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
  -WindowsInstaller .\dist\installer\IntDemoOnline-Setup-0.2.5.exe `
  -UosInstaller .\dist\uos-arm64\IntDemo-UOS-arm64-0.2.5.deb `
  -DeltaInstaller .\dist\installer\IntDemoOnline-Patch-0.2.4-to-0.2.5.exe `
  -DeltaFromVersion 0.2.4 `
  -Version 0.2.5 `
  -Notes "本次更新说明" `
  -RemoteHost intdemo-test `
  -RemotePath /opt/intdemo/deploy/updates
```

该兼容脚本的 UOS 参数直接发布完整 DEB。逐文件更新应使用双端打包发布器导入 UOS
真机生成的标准结果 ZIP，避免绕过来源收据、完整回放和 50% 体积门禁。

Compose 将服务器的 `deploy/updates/` 同时只读挂载给 API 和 Caddy。
`/updates/test.json`、`/updates/stable.json` 和 `/updates/capabilities.json` 由
API 处理；部署时必须同步更新 Caddy 配置，不能让能力地址落入静态
`/updates/*` 文件服务。清单接口根据
`X-IntDemo-Platform`、`X-IntDemo-Version` 或 `IntDemoUpdater/<版本>` User-Agent
动态选择 Windows x64/UOS ARM64 包；
其余 `/updates/files/*` 仍由 Caddy 直接下载。只有当前版本精确匹配
`deltas.from_version` 才返回 Windows 增量包；UOS 还要求当前版本精确匹配
`file_updates.from_version` 并声明 `uos-file-update-v2` 能力，其他情况会继续尝试旧
分层兼容包或返回完整 DEB。

打包发布器填写“仅允许更新的当前版本”后，清单会增加
`eligible_client_versions`。API 只向当前版本精确位于列表中的客户端返回更新，
其他版本和未上报版本返回 HTTP 204；字段缺省时行为完全不变。发布器会先验证
`/updates/capabilities.json` 包含 `source-version-targeting-v1`，不满足时拒绝
定向发布。该升级没有数据库迁移，不改变账号或会话，v1.1.0/v1.1.1 客户端无需
升级即可上报版本并把 HTTP 204 作为“暂无更新”。

`-LegacyDeltaPrimary` 仅作为旧发布命令的兼容参数保留，已经不会把增量包
写入公共清单顶层，新的发布命令无需使用。

1. 本地完整构建并执行安装、启动、卸载冒烟测试。
2. 同时生成 Windows `.exe`、UOS ARM64 `.deb` 的 SHA-256 和 `test.json`。
3. 把双端安装包上传到服务器 `.incoming` 并核对大小及 SHA-256。
4. 安装包就位后原子替换 `test.json`，客户端才会看到新版本。

需要紧急停止向新检查分发当前版本时，可在专用打包发布器点击“暂停分发”，
或运行：

```powershell
.\scripts\pause-update.ps1 `
  -Channel test `
  -RemoteHost intdemo-test `
  -RemotePath /opt/intdemo/deploy/updates
```

脚本会把活动清单完整归档到
`/opt/intdemo/deploy/updates-paused/`，再原子替换为暂停标记；不会删除
安装包，也不会中断已经取得清单或正在下载的客户端。暂停期间新的更新检查
按“暂无更新”处理。之后用发布器发布更高版本会自动恢复分发，且版本防降级
检查仍以暂停前版本为准。

客户端只接受与 API 相同主机、相同 HTTPS 信任链下的更新地址，并在运行
安装包前核对清单声明的大小及 SHA-256。UOS 完整更新使用 `.deb` 并调用系统提权
安装；v1.2.2 之后的匹配客户端也可在用户目录应用 `.intlayer`，失败时回退完整
DEB。Windows 使用 Inno Setup `.exe`。未购买 Windows 代码签名证书前，
其他电脑首次运行安装包可能显示“未知发布者”；这不应通过关闭
SmartScreen 或禁用 TLS 校验来规避，正式发布建议购买组织代码签名证书。

发布时加 `-Mandatory` 会生成强制更新清单。客户端检测到强制更新后不能
关闭提示窗口，必须下载并重启安装；普通更新提供“立即更新”“后台更新”
“稍后更新”和“不再提示”。“后台更新”会收起弹窗并继续在“系统设置”显示
进度；“不再提示”只对当前版本生效，用户仍可在“系统设置”中手动下载。
提示窗口等待处理时仅锁定业务内容，主窗口最小化、最大化和关闭按钮保持可用；
下载期间系统设置与弹窗会显示进度，其他业务页面可以继续使用，开始安装时才
要求保存并停止当前业务。

当前测试安装包使用 Inno Setup 6.7.3。该版本编译器会明确标注仅限
非商业用途；迁入公司正式商用前需购买 Inno Setup 商业许可证，或替换成
公司已经授权的 MSI/安装包工具。

## 8. 官方参考

- [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https)
- [Caddy `tls internal`](https://caddyserver.com/docs/caddyfile/directives/tls)
- [腾讯轻量服务器域名与备案说明](https://cloud.tencent.com/document/product/1207/81332)
