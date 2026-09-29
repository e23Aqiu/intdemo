# 强化训练组件

验证码引擎是管理员离线分发的可选组件，通过客户端本地文件选择器导入。组件包本身
不上传服务器，也不由客户端或服务端联网更新。组件同时负责强化训练和本机推理，支持
Windows x86_64 和统信 UOS/Linux aarch64。两个平台必须分别在目标系统本机用
PyInstaller 构建，禁止交叉打包。
UOS 的 PyInstaller 6 `onedir` 产物允许使用仅指向组件目录内普通文件的符号链接；打包
时会把这些链接展开为普通文件，越界链接、目录链接和特殊文件仍会被拒绝。

## 模型与协议

- 当前组件版本：`2.0.0`；训练 protocol 为 v2，稳定推理 API 为 v1。
- 服务端可见的算法标识：`captcha-engine-v1`。模型私有格式由本机组件解释。
- 数字验证码：完整原图输入的四位置多头 Tiny CNN，不切成四个等宽字符；输入
  `[1,1,48,112]`，输出 `[1,4,10]`。网络、训练方法和右边缘随机残缺增强保持不变。
- 文字点选验证码：组件先用多视图检测得到候选文字框，再为“题目文字 × 候选框”提取
  33 维 OCR 概率、识别一致性、候选稳定性和几何特征。小型排序网络输入 `[N,33]`、
  输出 `[N,1]`，最后执行全局一对一匹配，避免把两个题目文字分配到同一候选框。
  该方案不建立固定汉字类别表，因此后续采集到新文字时无需修改客户端或服务端协议。
- 低置信度、候选框不足或全局匹配不明确时组件返回“拒答”，客户端自动使用原有
  `ddddocr` 流程；不会为了使用新模型而强制点击低质量坐标。
- 训练结果是 `candidate.intmodel` 数据包，包含稳定的外层清单、文件大小和 SHA-256。
  服务端只校验此外层数据包，不解析组件的私有模型系列；客户端把模型交给当前本机
  组件做最终加载与真实推理自检。以后只要稳定推理 API 不变，新组件可支持新的私有
  模型系列，无需同时发布客户端和服务端。
- 训练 protocol v2 输出目录严格只包含 `candidate.intmodel`、`metadata.json`、
  `metrics.json`。JSON、文件大小及两个文件的 SHA-256 会由客户端再次校验。
- 组件在标准输出按 JSON Lines 发送 `started`、`feature`、`epoch`、`evaluation` 和
  `completed` 事件。事件只写入管理员本机训练终端，不增加服务端字段，不包含图片。

训练命令由客户端生成，等价于：

```text
intdemo-trainer train --protocol-version 2 --dataset DATASET.zip \
  --captcha-type numeric|click --output OUTPUT_DIR
```

推理服务由客户端常驻管理，等价于：

```text
intdemo-trainer serve --engine-api-version 1
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

卸载入口只移除本机验证码引擎及其模式设置，不删除已采集样本、候选模型、服务端模型
或客户端已缓存的模型包。组件卸载或不可用后，新训练回到标准模式，业务识别回退到内置
`ddddocr`；重新本地导入兼容组件后可再次加载对应模型。
