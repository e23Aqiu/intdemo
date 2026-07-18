# 运输业务一体化客户端（IntDemo）

IntDemo 是一个面向运输业务处理场景的 Windows 桌面客户端。项目将“运输证查询回填”和“爱企查批量查询”整合为统一的 PyQt5 应用，通过一条可暂停、继续和停止的处理流水线，完成运输证号查询、营运企业回填以及企业法人、地址、电话补齐。

当前版本：`0.1.0`

## 项目介绍

项目主要提供以下能力：

- 使用统一登录入口和主界面管理运输业务处理流程。
- 支持管理员和普通用户两种角色，以及账号创建、重置密码、启用和停用。
- 对同一份 `.xlsx` 文件依次执行运输证查询、营运企业回填和爱企查信息补齐。
- 实时展示数据预览、总体进度、分步骤状态和运行日志。
- 支持数字验证码、中文点选验证码、网站登录和必要的人工录入。
- 提供按站点、完成类型和违规原因筛选的数据仪表盘。
- 将账号、角色、密码哈希和统计事件保存在本地 SQLite 数据库中。
- 提供离线自动化测试，并支持使用 PyInstaller 构建 Windows 客户端。

> 查询网站的验证码、页面结构和访问策略可能变化。涉及真实网站的功能需要使用合法账号、授权数据和当前网络环境进行验收。

## 环境要求

- Windows 10/11（项目当前主要支持的平台）
- 64 位 Python 3.9，推荐 Python 3.9.13
- 可用的现代浏览器和网络连接（执行在线查询时需要）
- Git（仅克隆和参与开发时需要）

## 安装方法

### 1. 获取代码

```powershell
git clone https://github.com/e23Aqiu/intdemo.git
cd intdemo
```

仓库当前为私有仓库，克隆账号需要拥有访问权限。

### 2. 创建并激活虚拟环境

```powershell
py -3.9 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

如果 PowerShell 阻止激活脚本，可在当前终端临时允许本地脚本：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 3. 安装依赖

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如需构建安装包，再安装开发依赖：

```powershell
python -m pip install -r requirements-dev.txt
```

## 运行方法

在已激活虚拟环境的终端中执行：

```powershell
python main.py
```

也可以在资源管理器中双击 `run.bat`。该脚本会优先使用项目目录内的 `.venv`。

### 首次登录

- 管理员账号：`admin`
- 初始密码：`Admin@123`

首次登录后系统会要求修改初始密码。正式使用前应创建个人管理员账号，并妥善保管密码。

系统还会幂等创建以下普通站点账号，重复启动不会重置已有密码：

| 用户名称 | 登录账号 | 初始密码 |
| --- | --- | --- |
| 萝岗中心站 | `luogang` | `123456` |
| 太平中心站 | `taiping` | `123456` |
| 道滘中心站 | `daojiao` | `123456` |
| 宝安中心站 | `baoan` | `123456` |
| 南头中心站 | `nantou` | `123456` |

### 本地数据

Windows 默认数据目录为：

```text
%LOCALAPPDATA%\IntDemoClient\client.db
```

可通过环境变量 `INTDEMO_DATA_DIR` 指定其他数据目录。数据库保存账号、密码哈希、角色、状态和统计事件，不保存明文密码。数据库、日志、用户表格和导出结果均已通过 `.gitignore` 排除，不应提交到 Git。

## 测试与构建

运行离线自动化测试：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
python -m unittest discover -s tests -v
```

测试覆盖账号与权限、密码处理、统计口径、主窗口装配、实时表格模型、三步自动衔接和 Excel 结果回写。在线查询仍需单独进行人工验收。

构建 Windows 可执行程序：

```powershell
pyinstaller integrated_client.spec
```

构建产物会生成在 `build/` 和 `dist/`，这两个目录不会进入 Git。

## 项目目录结构

```text
intdemo/
├── main.py                         # 应用入口
├── run.bat                         # Windows 快速启动脚本
├── requirements.txt                # 运行依赖
├── requirements-dev.txt            # 构建与开发依赖
├── integrated_client.spec          # PyInstaller 构建配置
├── integrated_client/
│   ├── app_controller.py           # 应用生命周期与窗口协调
│   ├── config.py                   # 应用常量与数据目录配置
│   ├── database.py                 # 账号、权限与统计数据持久化
│   ├── models.py                   # 领域数据模型
│   ├── security.py                 # 密码哈希与校验
│   ├── tools/
│   │   ├── transport_tool.py       # 运输证查询与营运信息回填
│   │   └── aiqicha_tool.py         # 爱企查企业信息补齐
│   └── ui/
│       ├── account_page.py         # 账号管理页面
│       ├── auth_dialogs.py         # 登录与密码对话框
│       ├── main_window.py          # 主窗口
│       ├── statistics_page.py      # 数据仪表盘
│       ├── theme.py                # 全局界面主题
│       └── workflow_page.py        # 一体化业务流水线页面
├── tests/                           # 离线自动化测试
├── DEVELOPMENT_LOG.md              # 历史开发记录
└── README.md                        # 项目说明
```

## 后续开发计划

- 建立 GitHub Actions，在提交和 Pull Request 上自动运行离线测试。
- 将网站选择器和业务适配器进一步模块化，降低外部页面变化带来的维护成本。
- 增加受控的集成测试数据和端到端验收清单，提升在线流程回归效率。
- 完善错误恢复、任务断点续跑、结构化日志和问题诊断能力。
- 优化首次启动流程，逐步减少代码内预置账号密码，并加强敏感配置管理。
- 建立版本号、变更日志、发布包和升级说明的标准发布流程。
- 持续梳理依赖版本与安全更新，并验证 Python 新版本兼容性。

## 维护说明

- 不要提交真实业务表格、运行数据库、日志、Cookie、访问令牌或网站账号信息。
- 修改业务工具后应同时运行离线测试，并对相关真实网站流程进行授权环境下的人工验收。
- 详细的历史实现记录见 `DEVELOPMENT_LOG.md`。
