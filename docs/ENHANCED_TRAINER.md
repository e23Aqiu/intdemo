# 强化训练组件

强化训练器是管理员离线分发的可选组件，不上传服务器，也不由客户端联网下载。
组件支持 Windows x86_64 和统信 UOS/Linux aarch64 本机 CPU 训练。两个平台必须分别在
目标系统本机用 PyInstaller 构建，禁止交叉打包。
UOS 的 PyInstaller 6 `onedir` 产物允许使用仅指向组件目录内普通文件的符号链接；签包
时会把这些链接展开为受签名保护的普通文件，越界链接、目录链接和特殊文件仍会被拒绝。

## 模型与协议

- 算法标识：`tiny-cnn-onnx-v1`
- 数字验证码：完整原图输入的四位置多头 Tiny CNN，不切成四个等宽字符；输入
  `[1,1,48,112]`，输出 `[1,4,10]`。训练增强包含右边缘随机残缺。
- 文字点选验证码：按成功点击坐标取得候选字符训练裁图；输入 `[N,3,64,64]`，输出
  `[N,C]`，字符标签以 `labels_json` 写入 ONNX custom metadata。
- ONNX custom metadata 包含 `algorithm`、`captcha_type`、`version`、
  `training_mode`、`trainer_version`、`output_layout` 和 `onnx_opset`；点选模型另含
  `labels_json`。模型只有一个浮点输入和一个浮点输出。服务端上传、激活、下载时会
  校验元数据、opset、图结构及输入输出形状，客户端安装时还会执行真实推理自检；
  点选模型会分别用 1 个和 2 个候选裁图验证动态批次。
- 训练器 protocol v1 输出目录严格只包含 `candidate.onnx`、`metadata.json`、
  `metrics.json`。JSON、文件大小及两个文件的 SHA-256 会由客户端再次校验。

训练命令由客户端生成，等价于：

```text
intdemo-trainer train --protocol-version 1 --dataset DATASET.zip \
  --captcha-type numeric|click --output OUTPUT_DIR
```

可选的 CPU 参数通过环境变量设置：

- `INTDEMO_TRAINER_THREADS`：CPU 线程数，默认不超过 4。
- `INTDEMO_TRAINER_EPOCHS`：轮数，默认 24，允许 1～200。
- `INTDEMO_TRAINER_BATCH_SIZE`：批量大小；ARM64 默认 16，其他平台默认 32。

## 本机构建

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
  --key-id trainer-production-1 `
  --private-key-file D:\secure\trainer-ed25519-private.txt `
  all --build-root D:\trainer-build `
  --output D:\release\IntDemo-Trainer-1.0.0-windows-x86_64.inttrainer
```

在 UOS ARM64 上：

```bash
python scripts/package-trainer.py \
  --platform linux-aarch64 \
  --key-id trainer-production-1 \
  --private-key-file /secure/trainer-ed25519-private.txt \
  all --build-root /tmp/intdemo-trainer-build \
  --output /release/IntDemo-Trainer-1.0.0-linux-aarch64.inttrainer
```

私钥文件内容必须是 Base64 编码的 32 字节 Ed25519 私钥。也可以不用
`--private-key-file`，改为显式设置 `INTDEMO_TRAINER_SIGNING_PRIVATE_KEY`。两者同时存在
会被拒绝。私钥、公钥签发材料及实际组件包均不得提交到仓库。

外层 `.inttrainer` 是 ZIP 容器，`manifest.json` 使用专用 Ed25519 密钥签名，每个文件
同时记录大小和 SHA-256。应用安装包不会把生产公钥硬编码进程序；发布器在构建 Windows
安装包/便携包或 UOS DEB 时，通过管理员选择的 `trainer-trust.json` 将公钥随包写入。
未选择该文件时，安装包仍可运行标准训练，但不能校验和安装签名的强化组件。在线离线
授权密钥不会被复用为组件签名密钥。
`trainer-trust.json` 损坏或格式无效时，机器学习页面仍可打开并继续使用标准模式，但会
禁用强化组件安装并显示配置错误；发布器在构建前会拒绝无效配置，避免生成无法安装组件的
产物。

## 安装与卸载

管理员在客户端“机器学习”页面查看当前训练模式、实际算法、组件版本、目标平台和
占用空间，并从本地选择与当前系统相符的 `.inttrainer` 文件安装。安装时会验证签名、
平台、文件清单和组件自检；Windows x64 包不能装到 UOS ARM64，反之亦然。
升级成功后会保留当前版本和最近一个非活动版本作为本地回退点，更早版本自动清理；
页面显示的占用空间包含这两个版本及组件清单等本地文件。

训练、安装/升级、自检和卸载共用一个跨进程非阻塞独占锁：Windows 使用系统文件锁，
UOS/Linux 使用 `flock`。锁文件中的 PID 和操作名仅用于诊断，实际所有权由内核锁决定；
进程异常退出时锁会自动释放，下一次操作会覆盖陈旧元数据，不会因残留锁文件永久阻塞。

卸载入口只移除本机强化训练器及其模式设置，不删除已采集样本、标准/强化候选模型、
服务端模型或客户端已缓存的 ONNX 推理模型。组件卸载或不可用后，新训练回到标准模式。
