"""Tiny CPU-friendly CNN definitions with conservative ONNX operations."""

from __future__ import annotations


def _torch(torch_module=None):
    if torch_module is not None:
        return torch_module
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("强化训练组件缺少 PyTorch CPU 运行库") from exc
    return torch


def build_numeric_model(torch_module=None):
    """Return a whole-image four-head network; no character slicing exists."""

    torch = _torch(torch_module)
    nn = torch.nn

    class NumericTinyCnn(nn.Module):
        def __init__(self):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(1, 16, 3, stride=2, padding=1),
                nn.BatchNorm2d(16),
                nn.ReLU(inplace=True),
                nn.Conv2d(16, 16, 3, padding=1, groups=16),
                nn.Conv2d(16, 32, 1),
                nn.BatchNorm2d(32),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(2),
                nn.Conv2d(32, 32, 3, padding=1, groups=32),
                nn.Conv2d(32, 64, 1),
                nn.BatchNorm2d(64),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(2),
                nn.Conv2d(64, 96, 3, padding=1),
                nn.ReLU(inplace=True),
                nn.AdaptiveAvgPool2d((2, 7)),
            )
            self.classifier = nn.Sequential(
                nn.Flatten(),
                nn.Linear(96 * 2 * 7, 128),
                nn.ReLU(inplace=True),
                nn.Dropout(0.15),
                nn.Linear(128, 4 * 10),
            )

        def forward(self, images):
            return self.classifier(self.features(images)).reshape(-1, 4, 10)

    return NumericTinyCnn()


def build_click_model(class_count: int, torch_module=None):
    if not 2 <= int(class_count) <= 1024:
        raise ValueError("点选字符类别数量必须在 2 到 1024 之间")
    torch = _torch(torch_module)
    nn = torch.nn

    class ClickTinyCnn(nn.Module):
        def __init__(self):
            super().__init__()
            self.features = nn.Sequential(
                nn.Conv2d(3, 24, 3, stride=2, padding=1),
                nn.BatchNorm2d(24),
                nn.ReLU(inplace=True),
                nn.Conv2d(24, 24, 3, padding=1, groups=24),
                nn.Conv2d(24, 48, 1),
                nn.BatchNorm2d(48),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(2),
                nn.Conv2d(48, 48, 3, padding=1, groups=48),
                nn.Conv2d(48, 80, 1),
                nn.BatchNorm2d(80),
                nn.ReLU(inplace=True),
                nn.MaxPool2d(2),
                nn.Conv2d(80, 96, 3, padding=1),
                nn.ReLU(inplace=True),
                nn.AdaptiveAvgPool2d((1, 1)),
            )
            self.classifier = nn.Linear(96, int(class_count))

        def forward(self, images):
            features = self.features(images).flatten(1)
            return self.classifier(features)

    return ClickTinyCnn()


def parameter_count(model) -> int:
    return sum(int(parameter.numel()) for parameter in model.parameters())
