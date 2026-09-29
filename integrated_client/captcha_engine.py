"""Thread-safe host for the locally imported CAPTCHA engine component."""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import threading
import uuid

from .platform_support import (
    system_application_environment,
    system_application_launch_context,
)
from .trainer_component import (
    CAPTCHA_ENGINE_API_VERSION,
    TrainerComponentError,
    TrainerComponentManager,
)

ENGINE_REQUEST_TIMEOUT_SECONDS = 60
MAX_ENGINE_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_ENGINE_IMAGE_BYTES = 1024 * 1024


class CaptchaEngineClientError(RuntimeError):
    pass


class LocalCaptchaEngineClient:
    """Maintain one long-lived component subprocess for local inference."""

    def __init__(self, component_manager=None, *, process_factory=None):
        self.component_manager = component_manager or TrainerComponentManager()
        self.process_factory = process_factory or subprocess.Popen
        self._lock = threading.RLock()
        self._process = None
        self._reader = None
        self._responses = queue.Queue()
        self._component_version = ""
        self._artifacts: dict[str, tuple[str, bytes]] = {}

    def _reader_loop(self, process, responses) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                if len(raw) > 1024 * 1024:
                    responses.put(CaptchaEngineClientError("验证码组件响应过大"))
                    continue
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    responses.put(CaptchaEngineClientError("验证码组件响应格式无效"))
                    continue
                responses.put(payload)
        finally:
            try:
                stream.close()
            except OSError:
                pass
            responses.put(CaptchaEngineClientError("验证码组件进程已退出"))

    def _terminate_locked(self) -> None:
        process = self._process
        self._process = None
        self._component_version = ""
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass
        while True:
            try:
                self._responses.get_nowait()
            except queue.Empty:
                break

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    self._request_locked("shutdown", {}, timeout=3, ensure=False)
                except CaptchaEngineClientError:
                    pass
            self._terminate_locked()

    def component_changed(self) -> None:
        with self._lock:
            self._terminate_locked()

    def _start_locked(self) -> None:
        status = self.component_manager.get_status(verify_files=True)
        if not status.available:
            raise CaptchaEngineClientError(status.message or "本机验证码组件不可用")
        try:
            command = self.component_manager.build_engine_command()
            environment = system_application_environment()
            environment.update(
                {
                    "INTDEMO_TRAINER_PROTOCOL_VERSION": "2",
                    "INTDEMO_CAPTCHA_ENGINE_API_VERSION": str(
                        CAPTCHA_ENGINE_API_VERSION
                    ),
                }
            )
            options = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "env": environment,
                "shell": False,
                "bufsize": 0,
            }
            if os.name == "nt":
                options["creationflags"] = getattr(
                    subprocess,
                    "CREATE_NO_WINDOW",
                    0,
                )
            with system_application_launch_context():
                process = self.process_factory(command, **options)
        except (OSError, subprocess.SubprocessError, TrainerComponentError) as exc:
            raise CaptchaEngineClientError(f"无法启动本机验证码组件：{exc}") from exc
        self._process = process
        self._component_version = status.version
        responses = queue.Queue()
        self._responses = responses
        try:
            self._reader = threading.Thread(
                target=self._reader_loop,
                args=(process, responses),
                name="intdemo-captcha-engine-reader",
                daemon=True,
            )
            self._reader.start()
            hello = self._request_locked("hello", {}, timeout=10, ensure=False)
            if (
                not isinstance(hello, dict)
                or hello.get("engine_api_version") != CAPTCHA_ENGINE_API_VERSION
            ):
                raise CaptchaEngineClientError("验证码组件推理协议不兼容")
            for captcha_type, (version, artifact) in self._artifacts.items():
                self._load_locked(captcha_type, version, artifact)
        except Exception:
            self._terminate_locked()
            raise

    def _ensure_locked(self) -> None:
        process = self._process
        status = self.component_manager.get_status(verify_files=False)
        if not status.available:
            self._terminate_locked()
            raise CaptchaEngineClientError(status.message or "本机验证码组件不可用")
        if (
            process is None
            or process.poll() is not None
            or status.version != self._component_version
        ):
            self._terminate_locked()
            self._start_locked()

    def _request_locked(
        self,
        command: str,
        payload: dict,
        *,
        timeout: float = ENGINE_REQUEST_TIMEOUT_SECONDS,
        ensure: bool = True,
    ):
        if ensure:
            self._ensure_locked()
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise CaptchaEngineClientError("验证码组件进程未运行")
        request_id = uuid.uuid4().hex
        request = {"request_id": request_id, "command": command, **payload}
        try:
            process.stdin.write(
                json.dumps(
                    request,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            self._terminate_locked()
            raise CaptchaEngineClientError("无法向验证码组件发送请求") from exc
        try:
            response = self._responses.get(timeout=timeout)
        except queue.Empty as exc:
            self._terminate_locked()
            raise CaptchaEngineClientError("验证码组件响应超时") from exc
        if isinstance(response, Exception):
            self._terminate_locked()
            raise response
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            self._terminate_locked()
            raise CaptchaEngineClientError("验证码组件响应与请求不匹配")
        if response.get("ok") is not True:
            raise CaptchaEngineClientError(str(response.get("error") or "验证码组件执行失败"))
        return response.get("result")

    def _load_locked(self, captcha_type: str, version: str, artifact: bytes):
        return self._request_locked(
            "load_model",
            {
                "captcha_type": captcha_type,
                "version": version,
                "artifact_base64": base64.b64encode(artifact).decode("ascii"),
            },
        )

    def load_model(self, captcha_type: str, version: str, artifact: bytes) -> None:
        captcha = str(captcha_type or "").strip().casefold()
        if captcha not in {"numeric", "click"}:
            raise CaptchaEngineClientError("验证码类型无效")
        data = bytes(artifact or b"")
        if not 0 < len(data) <= MAX_ENGINE_ARTIFACT_BYTES:
            raise CaptchaEngineClientError("验证码组件模型包大小无效")
        model_version = str(version or "").strip()
        if not model_version or len(model_version) > 80:
            raise CaptchaEngineClientError("验证码组件模型版本无效")
        with self._lock:
            self._load_locked(captcha, model_version, data)
            self._artifacts[captcha] = (model_version, data)

    def unload_model(self, captcha_type: str) -> None:
        captcha = str(captcha_type or "").strip().casefold()
        with self._lock:
            self._artifacts.pop(captcha, None)
            if self._process is not None and self._process.poll() is None:
                try:
                    self._request_locked(
                        "unload_model",
                        {"captcha_type": captcha},
                        ensure=False,
                    )
                except CaptchaEngineClientError:
                    self._terminate_locked()

    def predict_numeric(self, image_bytes: bytes) -> dict:
        image = bytes(image_bytes or b"")
        if not 0 < len(image) <= MAX_ENGINE_IMAGE_BYTES:
            raise CaptchaEngineClientError("验证码图片大小无效")
        with self._lock:
            result = self._request_locked(
                "predict_numeric",
                {
                    "image_base64": base64.b64encode(image).decode("ascii")
                },
            )
        return dict(result or {})

    def predict_click(self, image_bytes: bytes, targets) -> dict:
        image = bytes(image_bytes or b"")
        target_list = [str(value or "").strip() for value in (targets or [])]
        if not 0 < len(image) <= MAX_ENGINE_IMAGE_BYTES:
            raise CaptchaEngineClientError("验证码图片大小无效")
        if not 1 <= len(target_list) <= 8 or any(not value for value in target_list):
            raise CaptchaEngineClientError("点选验证码目标文字无效")
        with self._lock:
            result = self._request_locked(
                "predict_click",
                {
                    "image_base64": base64.b64encode(image).decode("ascii"),
                    "targets": target_list,
                },
            )
        return dict(result or {})


__all__ = [
    "ENGINE_REQUEST_TIMEOUT_SECONDS",
    "MAX_ENGINE_ARTIFACT_BYTES",
    "MAX_ENGINE_IMAGE_BYTES",
    "CaptchaEngineClientError",
    "LocalCaptchaEngineClient",
]
