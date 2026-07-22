import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from .database import Database


class WorkflowTimingService:
    """持久化记录一次业务批次及其多次运行、步骤和暂停时间。"""

    RESULT_COLUMNS = {
        "运输证号_纯数字",
        "查询状态",
        "车辆所有人/企业",
        "回填状态",
        "负责人/法人代表",
        "地址",
        "电话",
    }
    ACTIVE_STATES = {"running", "stopping"}
    TERMINAL_STATES = {"stopped", "succeeded", "failed", "interrupted"}

    def __init__(self, database: Database, user_id: int, clock=None):
        self.database = database
        self.user_id = int(user_id)
        self.clock = clock or time.perf_counter
        self.process_session_id = uuid.uuid4().hex
        self.database.recover_stale_workflow_runs()

        self.batch_id = None
        self.run_id = None
        self.state = "idle"
        self.current_step = 0
        self.attempt_id = None
        self.attempt_no = 0

        self._run_active_ms = 0
        self._run_paused_ms = 0
        self._run_segment_started = None
        self._step_active_ms = 0
        self._step_paused_ms = 0
        self._step_segment_started = None
        self._previous_active_ms = 0
        self._previous_paused_ms = 0
        self._final_snapshot = None

    @staticmethod
    def _normalize_source_value(value):
        if value is None:
            return ""
        try:
            if value != value:
                return ""
        except (TypeError, ValueError):
            pass
        return str(value).strip()

    @classmethod
    def _build_source_signature(cls, dataframe, ignore_empty_placeholders=True):
        columns = []
        for column_index, column in enumerate(dataframe.columns):
            column_name = str(column)
            if column_name in cls.RESULT_COLUMNS:
                continue
            if ignore_empty_placeholders and column_name.casefold().startswith("unnamed:"):
                values = dataframe.iloc[:, column_index]
                if all(cls._normalize_source_value(value) == "" for value in values):
                    continue
            columns.append(column_name)
        digest = hashlib.sha256()
        digest.update(
            json.dumps(columns, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        for _, row in dataframe.iterrows():
            values = [cls._normalize_source_value(row.get(column, "")) for column in columns]
            digest.update(b"\n")
            digest.update(
                json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        return digest.hexdigest()

    @classmethod
    def build_source_signature(cls, dataframe) -> str:
        """仅使用原始输入列生成签名，并忽略结果列间的全空占位列。"""
        return cls._build_source_signature(
            dataframe,
            ignore_empty_placeholders=True,
        )

    @classmethod
    def build_source_signature_candidates(cls, dataframe):
        """同时返回当前签名和旧版本可能生成的兼容签名。"""
        normalized = cls.build_source_signature(dataframe)
        legacy = cls._build_source_signature(
            dataframe,
            ignore_empty_placeholders=False,
        )
        return tuple(dict.fromkeys((normalized, legacy)))

    @staticmethod
    def format_duration(milliseconds: int) -> str:
        total_ms = max(0, int(round(milliseconds)))
        hours, remainder = divmod(total_ms, 60 * 60 * 1000)
        minutes, remainder = divmod(remainder, 60 * 1000)
        seconds, milliseconds = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

    @property
    def is_active(self) -> bool:
        return self.state in {"running", "paused", "stopping"}

    def start_run(self, file_path, dataframe, run_id=None):
        if self.is_active:
            raise RuntimeError("已有计时任务正在运行")
        normalized_path = str(Path(file_path).expanduser().resolve())
        signature = self.build_source_signature(dataframe)
        signature_candidates = self.build_source_signature_candidates(dataframe)
        run_id = str(run_id or uuid.uuid4().hex)
        result = self.database.start_workflow_run(
            self.user_id,
            normalized_path,
            signature,
            run_id,
            self.process_session_id,
            os.getpid(),
            source_signature_aliases=signature_candidates,
        )

        self.batch_id = result["batch_id"]
        self.run_id = result["run_id"]
        self.state = "running"
        self.current_step = 0
        self.attempt_id = None
        self.attempt_no = 0
        self._run_active_ms = 0
        self._run_paused_ms = 0
        self._run_segment_started = self.clock()
        self._step_active_ms = 0
        self._step_paused_ms = 0
        self._step_segment_started = None
        self._previous_active_ms = result["previous_active_ms"]
        self._previous_paused_ms = result["previous_paused_ms"]
        self._final_snapshot = None
        return result

    def start_step(self, step_no: int):
        if not self.is_active or not self.run_id:
            return None
        if self.attempt_id:
            self.finish_step("interrupted", "新步骤开始前结束旧步骤计时")
        result = self.database.start_workflow_step(self.run_id, int(step_no))
        self.current_step = int(step_no)
        self.attempt_id = result["attempt_id"]
        self.attempt_no = result["attempt_no"]
        self._step_active_ms = 0
        self._step_paused_ms = 0
        self._step_segment_started = self.clock()
        return result

    def _totals_at(self, now):
        run_active = self._run_active_ms
        run_paused = self._run_paused_ms
        if self._run_segment_started is not None:
            elapsed = max(
                0,
                int(round((now - self._run_segment_started) * 1000)),
            )
            if self.state in self.ACTIVE_STATES:
                run_active += elapsed
            elif self.state == "paused":
                run_paused += elapsed

        step_active = self._step_active_ms
        step_paused = self._step_paused_ms
        if self.attempt_id and self._step_segment_started is not None:
            elapsed = max(
                0,
                int(round((now - self._step_segment_started) * 1000)),
            )
            if self.state in self.ACTIVE_STATES:
                step_active += elapsed
            elif self.state == "paused":
                step_paused += elapsed
        return run_active, run_paused, step_active, step_paused

    def snapshot(self):
        if self._final_snapshot is not None:
            return dict(self._final_snapshot)
        if not self.run_id:
            return {
                "state": "idle",
                "run_active_ms": 0,
                "run_paused_ms": 0,
                "batch_active_ms": 0,
                "batch_paused_ms": 0,
                "current_step": 0,
            }
        run_active, run_paused, step_active, step_paused = self._totals_at(
            self.clock()
        )
        return {
            "state": self.state,
            "batch_id": self.batch_id,
            "run_id": self.run_id,
            "run_active_ms": run_active,
            "run_paused_ms": run_paused,
            "batch_active_ms": self._previous_active_ms + run_active,
            "batch_paused_ms": self._previous_paused_ms + run_paused,
            "step_active_ms": step_active,
            "step_paused_ms": step_paused,
            "current_step": self.current_step,
            "attempt_no": self.attempt_no,
        }

    def _checkpoint(self, event_type=None, reason="", details=None):
        if not self.is_active or not self.run_id:
            return
        snapshot = self.snapshot()
        self.database.checkpoint_workflow_timing(
            self.run_id,
            self.state,
            snapshot["run_active_ms"],
            snapshot["run_paused_ms"],
            current_step=self.current_step,
            attempt_id=self.attempt_id,
            step_status=self.state if self.attempt_id else None,
            step_active_ms=snapshot.get("step_active_ms", 0),
            step_paused_ms=snapshot.get("step_paused_ms", 0),
            event_type=event_type,
            reason=reason,
            details=details,
        )

    def heartbeat(self):
        self._checkpoint()

    def _transition(self, state, event_type, reason=""):
        if not self.is_active or self.state == state:
            return
        now = self.clock()
        run_active, run_paused, step_active, step_paused = self._totals_at(now)
        self._run_active_ms = run_active
        self._run_paused_ms = run_paused
        self._step_active_ms = step_active
        self._step_paused_ms = step_paused
        self.state = state
        self._run_segment_started = now
        if self.attempt_id:
            self._step_segment_started = now
        self._checkpoint(event_type, reason)

    def pause(self, reason=""):
        if self.state in self.ACTIVE_STATES:
            self._transition("paused", "paused", reason)

    def resume(self, reason=""):
        if self.state == "paused":
            self._transition("running", "resumed", reason)

    def request_stop(self, reason="用户手动停止"):
        if self.state in {"running", "paused"}:
            self._transition("stopping", "stop_requested", reason)

    def record_retry(self, retry_type: str, reason=""):
        if not self.is_active or not self.run_id:
            return
        self.database.record_workflow_retry(
            self.run_id,
            self.attempt_id,
            retry_type,
            reason,
        )

    def finish_step(self, status: str, error_summary=""):
        if not self.attempt_id:
            return
        now = self.clock()
        _, _, step_active, step_paused = self._totals_at(now)
        self.database.finish_workflow_step(
            self.attempt_id,
            status,
            step_active,
            step_paused,
            error_summary,
        )
        self.attempt_id = None
        self.attempt_no = 0
        self._step_active_ms = 0
        self._step_paused_ms = 0
        self._step_segment_started = None

    def finish_run(self, status: str, reason="", error_summary=""):
        if not self.run_id or self.state in self.TERMINAL_STATES:
            return self.snapshot()
        if status not in self.TERMINAL_STATES:
            raise ValueError("无效的计时结束状态")
        if self.attempt_id:
            step_status = "succeeded" if status == "succeeded" else status
            self.finish_step(step_status, error_summary or reason)
        now = self.clock()
        run_active, run_paused, _, _ = self._totals_at(now)
        result = self.database.finish_workflow_run(
            self.run_id,
            status,
            run_active,
            run_paused,
            reason,
            error_summary,
        )
        self.state = status
        self._run_active_ms = run_active
        self._run_paused_ms = run_paused
        self._run_segment_started = None
        self._final_snapshot = {
            "state": status,
            "batch_id": self.batch_id,
            "run_id": self.run_id,
            "run_active_ms": run_active,
            "run_paused_ms": run_paused,
            "batch_active_ms": result["active_ms"],
            "batch_paused_ms": result["paused_ms"],
            "current_step": self.current_step,
            "run_count": result["run_count"],
        }
        return dict(self._final_snapshot)
