# 强化训练组件

强化训练器是管理员离线分发的可选组件，通过客户端本地文件选择器导入。组件包本身
不上传服务器，也不由客户端或服务端联网更新。组件只用于管理员本机的强化训练、产物
校验和自检；业务推理由桌面客户端内置运行时完成，普通电脑不需要导入组件。组件支持
Windows x86_64 和统信 UOS/Linux aarch64，两个平台必须分别在目标系统本机用
PyInstaller 构建，禁止交叉打包。
UOS 的 PyInstaller 6 `onedir` 产物允许使用仅指向组件目录内普通文件的符号链接；打包
时会把这些链接展开为普通文件，越界链接、目录链接和特殊文件仍会被拒绝。

## 模型与协议

- 当前组件版本：`2.0.0`；训练 protocol 为 v2，稳定模型运行时版本为 `2.0.0`。
- 服务端可见的算法标识：`captcha-engine-v1`。模型是数据包，不包含需要在普通客户端
  执行的组件程序。
- 数字验证码：完整原图输入的四位置多头 Tiny CNN，不切成四个等宽字符；输入
  `[1,1,48,112]`，输出 `[1,4,10]`。网络、训练方法和右边缘随机残缺增强保持不变。
- 文字点选验证码：组件先用多视图检测得到候选文字框，再为“题目文字 × 候选框”提取
  33 维 OCR 概率、识别一致性、候选稳定性和几何特征。小型排序网络输入 `[N,33]`、
  输出 `[N,1]`，最后执行全局一对一匹配，避免把两个题目文字分配到同一候选框。
  该方案不建立固定汉字类别表，因此后续采集到新文字时无需修改客户端或服务端协议。
- 低置信度、候选框不足或全局匹配不明确时客户端内置运行时返回“拒答”，并自动使用原有
  `ddddocr` 流程；不会为了使用新模型而强制点击低质量坐标。
- 训练结果是 `candidate.intmodel` 数据包，包含稳定的外层清单、文件大小和 SHA-256。
  服务端校验外层数据包，每台桌面客户端再用内置运行时校验模型系列并加载 ONNX。
  管理员应用后，其他在线客户端自动同步模型，不需要拥有 `.inttrainer`。
- 训练组件版本和模型运行时版本独立。训练方法、超参数和阈值更新只要继续输出既有模型
  契约，就只发布新组件；新增输入输出契约或模型系列时才需要升级客户端，服务端外层协议
  不变时无需修改。
- 训练 protocol v2 输出目录严格只包含 `candidate.intmodel`、`metadata.json`、
  `metrics.json`。JSON、文件大小及两个文件的 SHA-256 会由客户端再次校验。
- 组件在标准输出按 JSON Lines 发送 `started`、`feature`、`epoch`、`evaluation` 和
  `completed` 事件。事件只写入管理员本机训练终端，不增加服务端字段，不包含图片。

训练命令由客户端生成，等价于：

```text
intdemo-trainer train --protocol-version 2 --dataset DATASET.zip \
  --captcha-type numeric|click --output OUTPUT_DIR
```

组件内的推理服务仅用于训练产物自检，等价于：

```text
intdemo-trainer serve --engine-api-version 1
```

可选的 CPU 参数通过环境变量设置：

- `INTDEMO_TRAINER_THREADS`：CPU 线程数，默认不超过 4。
- `INTDEMO_TRAINER_EPOCHS`：轮数，默认 24，允许 1～200。客户端机器学习页面可
  直接设置并持久保存该值；标准 HOG + SVM 模式不使用训练轮次。
- `INTDEMO_TRAINER_BATCH_SIZE`：批量大小；ARM64 默认 16，其他平台默认 32。

## 一键打包组件

以下脚本只生成本地导入用的 `.inttrainer` 验证码组件，不会构建客户端程序、Windows
安装包、UOS `tar.gz`/DEB 或服务端。两个平台仍必须在对应的目标系统本机打包，不能
交叉构建。

首次运行需要指定经过审核、包含全部传递依赖的本平台离线 wheel 目录。脚本会创建并
缓存独立 Python 3.10 环境；相同组件版本再次打包时可不再指定 wheel 目录。

Windows x64 首次运行：

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\scripts\build-trainer-component-windows.ps1 `
  -Wheelhouse D:\wheelhouse\windows-x86_64
```

Windows x64 后续运行：

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\scripts\build-trainer-component-windows.ps1
```

UOS ARM64 首次运行：

```bash
bash scripts/uos-arm64/build-trainer-component.sh \
  --wheelhouse /data/wheelhouse/linux-aarch64
```

UOS ARM64 后续运行：

```bash
bash scripts/uos-arm64/build-trainer-component.sh
```

也可以用 Windows 的 `-PythonPath` 或 UOS 的 `--python` 指向已经准备好的 Python
3.10 本机环境。两个脚本都会依次验证平台、固定依赖版本、CPU 版 PyTorch、源码
自检、PyInstaller 冻结产物自检和 ZIP 完整性，并在以下时间戳目录生成组件及
`.sha256`：

```text
output/components/YYYYMMDD-HHMMSS/
```

脚本参数可分别通过以下命令查看：

```powershell
Get-Help .\scripts\build-trainer-component-windows.ps1 -Detailed
```

```bash
bash scripts/uos-arm64/build-trainer-component.sh --help
```

## 手动本机构建

先由管理员从已审核的离线 wheel 仓库准备独立 Python 3.10 环境。构建工具不会执行
`pip install`，也不会访问网络。平台依赖清单位于：

- `enhanced_trainer/requirements-windows-x86_64.txt`
- `enhanced_trainer/requirements-linux-aarch64.txt`

例如，从已审核且与本机平台相符的离线 wheel 目录安装：

```text
python -m pip install --no-index --find-links WHEELHOUSE \
  -r enhanced_trainer/requirements-PLATFORM.txt
```

在 Windows x64 上：

```powershell
python scripts/package-trainer.py `
  --platform windows-x86_64 `
  all --build-root D:\trainer-build `
  --output D:\release\IntDemo-Trainer-2.0.0-windows-x86_64.inttrainer
```

在 UOS ARM64 上：

```bash
python scripts/package-trainer.py \
  --platform linux-aarch64 \
  all --build-root /tmp/intdemo-trainer-build \
  --output /release/IntDemo-Trainer-2.0.0-linux-aarch64.inttrainer
```

外层 `.inttrainer` 是 ZIP 容器，也是管理员安装所需的唯一文件，不需要配套 JSON、
公钥或私钥。`manifest.json` 记录每个文件的大小和 SHA-256；安装器还会检查平台、路径、
入口程序、清单完整性，并在激活前运行组件 `self-test`。SHA-256 用于发现传输损坏或包内
文件与清单不一致，不用于证明发布者身份，因此管理员应只安装来自可信本地来源的组件包。
实际组件包不得提交到仓库。

## 安装与卸载

管理员在客户端“机器学习”页面查看当前训练模式、实际算法、组件版本、目标平台和
占用空间，并从本地选择与当前系统相符的 `.inttrainer` 文件安装。安装时会验证平台、
文件清单、文件大小/SHA-256 和组件自检；Windows x64 包不能装到 UOS ARM64，反之亦然。
升级成功后会保留当前版本和最近一个非活动版本作为本地回退点，更早版本自动清理；
页面显示的占用空间包含这两个版本及组件清单等本地文件。

训练、安装/升级、自检和卸载共用一个跨进程非阻塞独占锁：Windows 使用系统文件锁，
UOS/Linux 使用 `flock`。锁文件中的 PID 和操作名仅用于诊断，实际所有权由内核锁决定；
进程异常退出时锁会自动释放，下一次操作会覆盖陈旧元数据，不会因残留锁文件永久阻塞。

训练终端提供“中止训练”按钮。中止时客户端终止组件训练子进程并清理临时目录，不上传
半成品候选模型。强化训练不再设置固定时长超时，持续时间只由数据量、轮次、硬件和用户
主动中止决定。

卸载入口只移除管理员本机强化训练器及其模式设置，不删除已采集样本、候选模型、服务端
模型或客户端已缓存的模型包。组件卸载或不可用后，新训练回到标准模式；已应用模型的
业务识别继续由客户端内置运行时完成，不会因训练组件卸载而回退。
