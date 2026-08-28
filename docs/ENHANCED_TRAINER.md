# 强化训练组件

强化训练器是管理员离线分发的可选组件，不上传服务器，也不由客户端联网下载。
组件支持 Windows x86_64 和统信 UOS/Linux aarch64 本机 CPU 训练。两个平台必须分别在
目标系统本机用 PyInstaller 构建，禁止交叉打包。
UOS 的 PyInstaller 6 `onedir` 产物允许使用仅指向组件目录内普通文件的符号链接；打包
时会把这些链接展开为普通文件，越界链接、目录链接和特殊文件仍会被拒绝。

## 模型与协议

- 当前组件版本：`1.1.0`。protocol 仍为 v1，旧客户端会忽略不认识的新增进度事件。
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
- 训练器在标准输出按 JSON Lines 发送 `started`、`epoch`、`evaluation` 和 `completed`
  事件。每轮包含损失、训练准确率、批次数、样本数、耗时和学习率；评估事件包含
  识别结果、真实结果和是否正确。事件只写入管理员本机训练终端，不增加服务端字段，
  不包含验证码图片。

训练命令由客户端生成，等价于：

```text
intdemo-trainer train --protocol-version 1 --dataset DATASET.zip \
  --captcha-type numeric|click --output OUTPUT_DIR
```

可选的 CPU 参数通过环境变量设置：

- `INTDEMO_TRAINER_THREADS`：CPU 线程数，默认不超过 4。
- `INTDEMO_TRAINER_EPOCHS`：轮数，默认 24，允许 1～200。客户端机器学习页面可
  直接设置并持久保存该值；标准 HOG + SVM 模式不使用训练轮次。
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
  all --build-root D:\trainer-build `
  --output D:\release\IntDemo-Trainer-1.1.0-windows-x86_64.inttrainer
```

在 UOS ARM64 上：

```bash
python scripts/package-trainer.py \
  --platform linux-aarch64 \
  all --build-root /tmp/intdemo-trainer-build \
  --output /release/IntDemo-Trainer-1.1.0-linux-aarch64.inttrainer
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

卸载入口只移除本机强化训练器及其模式设置，不删除已采集样本、标准/强化候选模型、
服务端模型或客户端已缓存的 ONNX 推理模型。组件卸载或不可用后，新训练回到标准模式。
