# 统信 UOS ARM64 应用内分层更新

## 1. 当前方案与版本边界

- 实施版本：v1.2.2。
- 平台：统信 UOS Desktop 20 Professional ARM64（`linux-aarch64`）。
- 新协议：`uos-layered-v1`。
- v1.2.2 是分层更新基线完整版本；它仍以一个 DEB 安装，并在 DEB 内写入
  `layer-layout.json`。
- 首个可发布的分层更新是 v1.2.2 → 后续版本。v1.2.1 及更早 DEB 没有分层
  布局，因此以这些版本为来源时，构建器会自动生成“仅完整包”结果。
- 原 `uos-deb-xdelta-v1` 协议仍保留在客户端和服务器中用于兼容已经发布的
  清单，但新构建不再对整个压缩 DEB 制作差分。

## 2. 目标和不变项

1. 用户首次和手工安装时仍只安装一个 DEB，不需要额外插件、Python、更新器或
   root 常驻服务。
2. 常规业务更新不再重新下载未变化的 Python/Qt 运行库和 Chromium。
3. 每个版本仍保留完整 DEB，分层条件不满足、下载失败、校验失败或本机基线
   不可用时自动下载完整 DEB。
4. v1.1.0、v1.1.1 和所有未声明 `uos-layered-v1` 能力的客户端只收到完整包。
5. 不修改账号表、访问令牌、刷新令牌、设备授权、客户端数据库路径或同步协议。
   已登录账号和本地数据不因更新协议升级而迁移或失效。

## 3. 三层结构

完整 DEB 内部保持原安装结构，但为更新计算拆成三个逻辑层：

```text
/opt/apps/com.e23aqiu.intdemo/files/
├── app/intdemo-client       # app：业务 Python/PYZ 可执行层
├── app/_internal/           # runtime：Python、Qt、动态库和数据文件
├── browser/                 # browser：内置 Chromium
├── intdemo-client           # 稳定启动器（随完整 DEB 安装）
├── client-online.json       # 连接配置（继续由完整 DEB/用户配置管理）
├── certs/                   # HTTPS CA（继续由完整 DEB 管理）
└── layer-layout.json        # 三层大小、文件数和 SHA-256 布局
```

每次 UOS 完整构建都会重新计算三层布局。构建下一版本时，与真实已发布来源 DEB
中的布局比较：

- 只改业务 Python 代码：通常只携带 `app`。
- 依赖或资源变化：携带 `app` 和 `runtime`。
- Chromium 变化：额外携带 `browser`。
- 稳定启动器、在线连接配置或 CA 发生变化：不能通过三层包替换，自动只发布完整
  DEB；布局中的引导文件指纹会阻止遗漏这些变化。
- 分层 ZIP 达到完整 DEB 的 50%：不发布分层包，自动只发布完整 DEB。

分层包扩展名为 `.intlayer`，内部包含唯一 `manifest.json` 和发生变化的完整层。
它不对 `.deb`、`data.tar.xz` 等压缩流做二进制差分。

## 4. 客户端处理和回滚

客户端通过 HTTPS 下载后先校验清单声明的大小和 SHA-256，再执行以下流程：

1. 核对来源版本、来源布局哈希、目标版本和目标布局哈希。
2. 拒绝绝对路径、`..`、重复路径、未声明层、特殊文件和越界符号链接。
3. 在用户数据目录的临时版本目录中解开变化层。
4. 未变化的层在 UOS 上以用户权限符号链接复用；不支持符号链接的文件系统回退
   为复制，结果必须相同。
5. 对组装后的 `app`、`runtime`、`browser` 重新逐层计算哈希。
6. 全部匹配后原子写入待启动版本标记。
7. 用户点击“重启并应用”后，稳定启动器先等待旧进程退出，再启动待确认版本。
8. 新版本创建 Qt 应用、数据库和主控制器后，Qt 事件循环持续运行并显示登录界面，
   才确认版本。
9. 如果新版本在确认前退出或崩溃，下一次启动会删除激活标记，自动回到上一个
   已确认版本或 DEB 内置版本。

目录如下：

```text
<get_data_dir()>/uos-layers/
├── downloads/               # 已下载并通过 SHA-256 的 .intlayer
├── versions/<版本>/         # 组装后的用户级版本
├── pending-version          # 待试运行版本
├── pending-attempted        # 已尝试启动但尚未确认
└── current-version          # 已确认版本
```

该目录只保存程序层，不保存或搬移 `client-v2.db`、账号凭据、在线会话、用户配置、
浏览器业务资料或同步数据。删除它只会让客户端回到 DEB 内置版本，并在下一次更新时
使用完整包。

## 5. 清单能力协商

v1.2.2 起的 UOS 客户端发送：

```text
X-IntDemo-Update-Capabilities: uos-layered-v1, uos-deb-xdelta-v1
```

分层清单示例：

```json
{
  "platforms": {
    "linux-aarch64": {
      "full": {
        "installer_path": "/updates/files/IntDemo-UOS-arm64-1.2.3.deb",
        "sha256": "<完整 DEB SHA-256>",
        "size": 327000000
      },
      "layered_updates": [
        {
          "format": "uos-layered-v1",
          "from_version": "1.2.2",
          "source_layout_sha256": "<v1.2.2 布局 SHA-256>",
          "target_layout_sha256": "<v1.2.3 布局 SHA-256>",
          "installer_path": "/updates/files/IntDemo-UOS-arm64-Layers-1.2.2-to-1.2.3.intlayer",
          "sha256": "<分层包 SHA-256>",
          "size": 18000000,
          "target_sha256": "<完整 DEB SHA-256>",
          "target_size": 327000000
        }
      ]
    }
  }
}
```

服务器仅在平台、`from_version` 和能力头同时匹配时返回分层包。否则会移除
`layered_updates` 并把完整 DEB 展平到 schema-v1 字段。因此 v1.1.0、v1.1.1、
v1.2.1 的响应和安装行为保持原样。

## 6. UOS 真机构建、Windows 发布

来源版本为 `N`、目标版本为 `N+1` 时：

1. Windows 发布器填写“UOS 分层来源”为 `N`，点击“导出已发布基线包”。
2. 将 `uos-delta-base-N.zip` 带到 UOS ARM64 真机并点击“导入已发布基线包”。
   文件名沿用旧格式以兼容已有发布器数据；包内仍是真实已发布 DEB 和发布收据。
3. UOS 项目切到目标版本并构建。`build.sh` 生成完整 DEB 和目标布局，随后
   `build-layers.sh` 解开真实来源 DEB、比较三层、生成 `.intlayer` 并进行完整回放。
4. 若来源没有布局、引导文件变化、三层均无变化或包体不低于完整 DEB 的 50%，
   报告写入 `fallback_to_full=true`，标准结果 ZIP 仍会正常导出，只是不带
   `.intlayer`。
5. 把一个 `uos-build-result-*.zip` 带回 Windows，点击“导入统信构建包”。发布器
   自动恢复来源版本并识别“分层”或“完整包回退”，不需要单独选择裸文件。
6. Windows 构建结果就绪后执行双端发布。

操作次数与原基线传递方案相同：UOS 仍负责 ARM64 构建和回放，Windows 仍负责导入
一个结果 ZIP 并发布。

## 7. 发布器门禁

分层候选必须同时满足：

- 来源 DEB 与来源 `publish-receipt.json` 的名称、大小、SHA-256 一致；
- 来源/目标版本正确且来源低于目标；
- 目标完整 DEB 与构建报告一致；
- 来源与目标布局自校验通过；
- 来源与目标的稳定启动器、在线配置和 CA 引导指纹一致；
- `.intlayer` 在 ARM64 构建机完成组装回放；
- 回放后的三个层与目标布局完全一致；
- 分层包严格小于目标完整 DEB 的 50%。

未过体积门槛、旧来源无布局、引导文件变化或三层无变化属于安全的完整包回退，
不是构建失败。清单中始终包含完整 DEB，只有合格结果才包含
`layered_updates`。

发布分层包前，发布器会访问 `/updates/capabilities.json`，确认服务器已经声明
`uos-layered-v1`；未升级服务器时阻止发布分层清单。完整包发布和旧客户端更新不依赖
该新能力。

## 8. 部署与兼容顺序

1. 先部署包含 `uos-layered-v1` 选择逻辑的服务器。该变更只增加匿名更新清单能力，
   不执行数据库迁移，也不改变认证接口。
2. 验证 v1.1.0、v1.1.1 无能力头请求仍得到完整 DEB，登录/刷新/查询/同步正常。
3. 发布 v1.2.2 完整 DEB。即使“UOS 分层来源”填写 v1.2.1，也会因来源无布局
   自动发布完整包；v1.2.2 由此建立首个分层基线。
4. 下一版先在测试通道只定向 v1.2.2，验证业务层下载、启动确认和故障回退。
5. 保留目标完整 DEB，灰度通过后再扩大范围。

## 9. 验收清单

- 完整安装只需一个 DEB，无外部插件。
- v1.1.0/v1.1.1 更新清单仍为完整包，已有登录状态不变。
- v1.2.1 → v1.2.2 构建不会因缺少分层包报错，而是正常完整包回退。
- v1.2.2 → 后续仅业务变更时 `.intlayer` 不包含 `runtime` 和 `browser`。
- 运行库或 Chromium 改变时，相应层自动进入包。
- 包超过 50%、引导文件变化、布局不匹配、下载损坏、磁盘不足时使用完整 DEB。
- 待确认版本启动失败后，下一次启动回到已确认版本。
- 正常确认后，版本检查使用新 `APP_VERSION`，账号数据库与令牌文件路径未变化。
