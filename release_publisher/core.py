from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
REMOTE_HOST_PATTERN = re.compile(r"^[A-Za-z0-9._@:-]+$")
REMOTE_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._/-]+$")


def _safe_remote_path(value: str) -> bool:
    normalized = str(value or "").strip()
    return bool(
        REMOTE_PATH_PATTERN.fullmatch(normalized)
        and normalized != "/"
        and all(part not in {"", ".", ".."} for part in normalized.split("/")[1:])
    )


class PublisherError(ValueError):
    pass


def _version_key(value: str) -> tuple[int, int, int]:
    normalized = str(value or "").strip()
    if not VERSION_PATTERN.fullmatch(normalized):
        raise PublisherError(f"版本号必须使用 x.y.z 格式：{normalized or '-'}")
    return tuple(int(part) for part in normalized.split("."))


@dataclass(frozen=True)
class PublisherSettings:
    base_url: str = ""
    ca_bundle: str = ""
    control_username: str = "admin"
    inno_compiler: str = ""
    remote_host: str = ""
    remote_path: str = "/opt/intdemo/deploy/updates"
    identity_file: str = ""
    channel: str = "test"
    build_portable: bool = True
    build_windows: bool = True
    build_uos: bool = True
    uos_builder_host: str = ""
    uos_builder_path: str = "/opt/intdemo"


class SettingsStore:
    VERSION = 1

    def __init__(self, path: str | Path | None = None):
        if path is None:
            if os.name == "nt":
                root = Path(
                    os.environ.get(
                        "LOCALAPPDATA",
                        Path.home() / "AppData" / "Local",
                    )
                )
                path = root / "IntDemoReleasePublisher" / "settings.json"
            else:
                root = Path(
                    os.environ.get(
                        "XDG_CONFIG_HOME",
                        Path.home() / ".config",
                    )
                )
                path = root / "intdemo-release-publisher" / "settings.json"
        self.path = Path(path).expanduser().resolve()

    def load(self) -> PublisherSettings:
        if not self.path.is_file():
            return PublisherSettings()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if int(payload.get("schema_version") or 0) != self.VERSION:
                return PublisherSettings()
            values = payload.get("settings")
            if not isinstance(values, dict):
                return PublisherSettings()
            allowed = set(PublisherSettings.__dataclass_fields__)
            return PublisherSettings(
                **{key: value for key, value in values.items() if key in allowed}
            )
        except (OSError, UnicodeError, ValueError, TypeError):
            return PublisherSettings()

    def save(self, settings: PublisherSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.VERSION,
            "settings": asdict(settings),
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ReleaseOptions:
    repo_root: Path
    version: str
    base_url: str
    notes: str
    ca_bundle: str = ""
    delta_from_version: str = ""
    channel: str = "test"
    mandatory: bool = False
    build_portable: bool = True
    inno_compiler: str = ""
    remote_host: str = ""
    remote_path: str = "/opt/intdemo/deploy/updates"
    identity_file: str = ""
    build_windows: bool = True
    build_uos: bool = False
    uos_builder_host: str = ""
    uos_builder_path: str = "/opt/intdemo"

    @property
    def full_installer(self) -> Path:
        return (
            self.repo_root
            / "dist"
            / "installer"
            / f"IntDemoOnline-Setup-{self.version}.exe"
        )

    @property
    def delta_installer(self) -> Path | None:
        if not self.delta_from_version:
            return None
        return (
            self.repo_root
            / "dist"
            / "installer"
            / (
                f"IntDemoOnline-Patch-{self.delta_from_version}"
                f"-to-{self.version}.exe"
            )
        )

    @property
    def uos_installer(self) -> Path:
        return (
            self.repo_root
            / "dist"
            / "uos-arm64"
            / f"IntDemo-UOS-arm64-{self.version}.deb"
        )

    @property
    def snapshot_path(self) -> Path:
        return (
            self.repo_root
            / "dist"
            / "release-snapshots"
            / f"{self.version}.json"
        )


@dataclass(frozen=True)
class CommandStep:
    key: str
    title: str
    program: str
    arguments: tuple[str, ...]
    working_directory: Path

    def display_command(self) -> str:
        return subprocess.list2cmdline([self.program, *self.arguments])


@dataclass(frozen=True)
class GitPushPlan:
    branch: str
    remote: str
    remote_branch: str
    upstream: str | None
    ahead_count: int
    commits: tuple[str, ...]
    step: CommandStep

    @property
    def target(self) -> str:
        return f"{self.remote}/{self.remote_branch}"

    @property
    def sets_upstream(self) -> bool:
        return self.upstream is None


def project_version(repo_root: str | Path) -> str:
    path = Path(repo_root) / "integrated_client" / "config.py"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublisherError(f"无法读取项目版本：{path}") from exc
    match = re.search(r'(?m)^APP_VERSION\s*=\s*"(?P<version>[^"]+)"\s*$', content)
    if not match:
        raise PublisherError("integrated_client/config.py 中缺少 APP_VERSION")
    version = match.group("version")
    _version_key(version)
    return version


def _version_sources(repo_root: Path) -> tuple[tuple[Path, re.Pattern[str], int], ...]:
    sources = (
        (
            repo_root / "integrated_client" / "config.py",
            re.compile(r'(?m)^APP_VERSION\s*=\s*"(?P<version>[^"]+)"\s*$'),
            1,
        ),
        (
            repo_root / "integrated_client" / "__init__.py",
            re.compile(r'(?m)^__version__\s*=\s*"(?P<version>[^"]+)"\s*$'),
            1,
        ),
        (
            repo_root / "server" / "app" / "__init__.py",
            re.compile(r'(?m)^__version__\s*=\s*"(?P<version>[^"]+)"\s*$'),
            1,
        ),
        (
            repo_root / "server" / "pyproject.toml",
            re.compile(r'(?m)^version\s*=\s*"(?P<version>[^"]+)"\s*$'),
            1,
        ),
        (
            repo_root / "server" / "app" / "main.py",
            re.compile(r'(?m)^\s*version="(?P<version>[^"]+)",\s*$'),
            1,
        ),
        (
            repo_root / "server" / "app" / "main.py",
            re.compile(
                r'(?m)^\s*return \{"status": "live", '
                r'"version": "(?P<version>[^"]+)"\}\s*$'
            ),
            1,
        ),
        (
            repo_root / "server" / "app" / "schemas.py",
            re.compile(
                r'(?m)^\s*client_version: str = Field'
                r'\(default="(?P<version>[^"]+)",'
            ),
            1,
        ),
        (
            repo_root / "installer" / "intdemo.iss",
            re.compile(
                r'(?m)^\s*#define MyAppVersion "(?P<version>[^"]+)"\s*$'
            ),
            1,
        ),
        (
            repo_root / "installer" / "version_info.txt",
            re.compile(
                r"StringStruct\(u'FileVersion', u'(?P<version>[^']+)'\)"
            ),
            1,
        ),
        (
            repo_root / "installer" / "version_info.txt",
            re.compile(
                r"StringStruct\(u'ProductVersion', u'(?P<version>[^']+)'\)"
            ),
            1,
        ),
        (
            repo_root / "docker-compose.yml",
            re.compile(r"(?m)^\s*image: intdemo-api:(?P<version>[^\s]+)\s*$"),
            1,
        ),
    )
    script_sources = tuple(
        (
            repo_root / "scripts" / script_name,
            re.compile(
                r'(?m)^\s*\[string\]\$Version = '
                r'"(?P<version>[^"]+)",\s*$'
            ),
            1,
        )
        for script_name in (
            "build-installer.ps1",
            "build-portable.ps1",
            "build-releases.ps1",
            "build-online-test.ps1",
        )
    )
    return sources + script_sources


def project_version_mismatches(
    repo_root: str | Path,
    expected_version: str,
) -> list[str]:
    root = Path(repo_root).resolve()
    _version_key(expected_version)
    mismatches: list[str] = []
    for path, pattern, expected_count in _version_sources(root):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            mismatches.append(f"缺少版本文件：{path.relative_to(root)}")
            continue
        matches = list(pattern.finditer(content))
        if len(matches) != expected_count:
            mismatches.append(
                f"{path.relative_to(root)}：应找到 {expected_count} 个版本字段，"
                f"实际为 {len(matches)}"
            )
            continue
        for match in matches:
            actual = match.group("version")
            if actual != expected_version:
                mismatches.append(
                    f"{path.relative_to(root)}：{actual} ≠ {expected_version}"
                )
    version_info = root / "installer" / "version_info.txt"
    try:
        content = version_info.read_text(encoding="utf-8")
    except OSError:
        return mismatches
    parts = tuple(int(item) for item in expected_version.split(".")) + (0,)
    expected_tuple = ", ".join(str(item) for item in parts)
    for field in ("filevers", "prodvers"):
        match = re.search(
            rf"(?m)^\s*{field}=\((?P<version>[^)]+)\),\s*$",
            content,
        )
        if not match or match.group("version").strip() != expected_tuple:
            actual = match.group("version").strip() if match else "缺失"
            mismatches.append(
                f"installer/version_info.txt：{field}={actual} ≠ {expected_tuple}"
            )
    return mismatches


def git_status(repo_root: str | Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=Path(repo_root),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise PublisherError(f"无法运行 Git：{exc}") from exc
    if result.returncode != 0:
        raise PublisherError(
            f"无法读取 Git 工作区状态：{result.stderr.strip() or result.stdout.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line.strip()]


def build_git_commit_steps(
    repo_root: str | Path,
    message: str,
) -> list[CommandStep]:
    root = Path(repo_root).resolve()
    normalized_message = str(message or "").strip()
    if not normalized_message:
        raise PublisherError("Git 提交说明不能为空")
    git_program = shutil.which("git")
    if not git_program:
        raise PublisherError("未找到 Git，请先安装 Git 并将其加入 PATH")
    return [
        CommandStep(
            key="git_stage",
            title="暂存全部 Git 变更",
            program=git_program,
            arguments=("add", "--all"),
            working_directory=root,
        ),
        CommandStep(
            key="git_commit",
            title="创建本地 Git 提交",
            program=git_program,
            arguments=("commit", "-m", normalized_message),
            working_directory=root,
        ),
    ]


def _run_git_capture(
    repo_root: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise PublisherError(f"无法运行 Git：{exc}") from exc


def _git_output(
    repo_root: Path,
    *arguments: str,
    action: str,
) -> str:
    result = _run_git_capture(repo_root, *arguments)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise PublisherError(f"{action}失败：{detail or 'Git 未返回错误详情'}")
    return result.stdout.strip()


def _current_git_branch(repo_root: Path) -> str:
    branch = _git_output(
        repo_root,
        "rev-parse",
        "--abbrev-ref",
        "HEAD",
        action="读取当前 Git 分支",
    )
    if not branch or branch == "HEAD":
        raise PublisherError("当前处于 detached HEAD，无法安全推送")
    return branch


def build_git_push_plan(repo_root: str | Path) -> GitPushPlan:
    root = Path(repo_root).resolve()
    changes = git_status(root)
    if changes:
        raise PublisherError("推送前请先提交全部 Git 工作区变更")

    branch = _current_git_branch(root)
    _git_output(
        root,
        "rev-parse",
        "--verify",
        "HEAD",
        action="读取当前 Git 提交",
    )

    upstream_result = _run_git_capture(
        root,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
    )
    upstream = (
        upstream_result.stdout.strip()
        if upstream_result.returncode == 0
        else None
    )
    if upstream:
        remote = _git_output(
            root,
            "config",
            "--get",
            f"branch.{branch}.remote",
            action="读取 Git 上游远程",
        )
        merge_ref = _git_output(
            root,
            "config",
            "--get",
            f"branch.{branch}.merge",
            action="读取 Git 上游分支",
        )
        if remote == "." or not merge_ref.startswith("refs/heads/"):
            raise PublisherError(f"当前上游 {upstream} 不是可推送的远程分支")
        remote_branch = merge_ref.removeprefix("refs/heads/")
        counts = _git_output(
            root,
            "rev-list",
            "--left-right",
            "--count",
            f"{upstream}...HEAD",
            action="比较本地与上游提交",
        ).split()
        if len(counts) != 2:
            raise PublisherError("无法解析本地与上游的提交差异")
        behind_count, ahead_count = (int(value) for value in counts)
        if behind_count:
            raise PublisherError(
                f"当前分支落后 {upstream} {behind_count} 个提交，"
                "请先拉取并处理差异后再推送"
            )
        commit_range = f"{upstream}..HEAD"
        push_arguments = (
            "push",
            "--porcelain",
            remote,
            f"HEAD:{merge_ref}",
        )
    else:
        remotes = _git_output(
            root,
            "remote",
            action="读取 Git 远程仓库",
        ).splitlines()
        if "origin" in remotes:
            remote = "origin"
        elif len(remotes) == 1:
            remote = remotes[0]
        elif not remotes:
            raise PublisherError("尚未配置 Git 远程仓库，无法推送")
        else:
            raise PublisherError(
                "当前分支没有上游且存在多个远程仓库，请先手动设置上游"
            )
        _git_output(
            root,
            "remote",
            "get-url",
            "--push",
            remote,
            action=f"读取 Git 远程 {remote}",
        )
        remote_branch = branch
        ahead_count = int(
            _git_output(
                root,
                "rev-list",
                "--count",
                "HEAD",
                action="统计当前分支提交",
            )
        )
        commit_range = "HEAD"
        merge_ref = f"refs/heads/{remote_branch}"
        push_arguments = (
            "push",
            "--porcelain",
            "--set-upstream",
            remote,
            f"HEAD:{merge_ref}",
        )

    commits = tuple(
        line
        for line in _git_output(
            root,
            "log",
            "--format=%h %s",
            "--max-count=20",
            commit_range,
            action="读取待推送提交",
        ).splitlines()
        if line.strip()
    )
    git_program = shutil.which("git")
    if not git_program:
        raise PublisherError("未找到 Git，请先安装 Git 并将其加入 PATH")
    return GitPushPlan(
        branch=branch,
        remote=remote,
        remote_branch=remote_branch,
        upstream=upstream,
        ahead_count=ahead_count,
        commits=commits,
        step=CommandStep(
            key="git_push",
            title=f"推送 Git 分支 {branch}",
            program=git_program,
            arguments=push_arguments,
            working_directory=root,
        ),
    )


def build_git_mirror_push_plans(
    repo_root: str | Path,
    remote_names: tuple[str, ...] = ("origin", "gitee"),
) -> list[GitPushPlan]:
    """Prepare ordinary, non-force pushes of the branch to GitHub and Gitee."""
    root = Path(repo_root).resolve()
    if git_status(root):
        raise PublisherError("推送前请先提交全部 Git 工作区变更")
    branch = _current_git_branch(root)
    _git_output(root, "rev-parse", "--verify", "HEAD", action="读取当前 Git 提交")
    configured = set(
        _git_output(root, "remote", action="读取 Git 远程仓库").splitlines()
    )
    missing = [name for name in remote_names if name not in configured]
    if missing:
        raise PublisherError(
            "缺少双仓库远程配置：" + "、".join(missing)
        )
    upstream_result = _run_git_capture(
        root,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
    )
    current_upstream = (
        upstream_result.stdout.strip()
        if upstream_result.returncode == 0
        else None
    )
    git_program = shutil.which("git")
    if not git_program:
        raise PublisherError("未找到 Git，请先安装 Git 并将其加入 PATH")

    plans: list[GitPushPlan] = []
    for index, remote in enumerate(remote_names):
        _git_output(
            root,
            "remote",
            "get-url",
            "--push",
            remote,
            action=f"读取 Git 远程 {remote}",
        )
        tracking = f"{remote}/{branch}"
        tracking_result = _run_git_capture(
            root,
            "rev-parse",
            "--verify",
            f"refs/remotes/{tracking}",
        )
        tracking_exists = tracking_result.returncode == 0
        if tracking_exists:
            counts = _git_output(
                root,
                "rev-list",
                "--left-right",
                "--count",
                f"{tracking}...HEAD",
                action=f"比较本地与 {tracking} 提交",
            ).split()
            if len(counts) != 2:
                raise PublisherError(f"无法解析本地与 {tracking} 的提交差异")
            behind_count, ahead_count = (int(value) for value in counts)
            if behind_count:
                raise PublisherError(
                    f"当前分支落后 {tracking} {behind_count} 个提交，"
                    "请先拉取并处理差异后再双仓库推送"
                )
            commit_range = f"{tracking}..HEAD"
        else:
            ahead_count = int(
                _git_output(
                    root,
                    "rev-list",
                    "--count",
                    "HEAD",
                    action=f"统计待推送到 {remote} 的提交",
                )
            )
            commit_range = "HEAD"
        commits = tuple(
            line
            for line in _git_output(
                root,
                "log",
                "--format=%h %s",
                "--max-count=20",
                commit_range,
                action=f"读取待推送到 {remote} 的提交",
            ).splitlines()
            if line.strip()
        )
        set_upstream = current_upstream is None and index == 0
        arguments = ["push", "--porcelain"]
        if set_upstream:
            arguments.append("--set-upstream")
        arguments.extend((remote, f"HEAD:refs/heads/{branch}"))
        plans.append(
            GitPushPlan(
                branch=branch,
                remote=remote,
                remote_branch=branch,
                upstream=(None if set_upstream else tracking),
                ahead_count=ahead_count,
                commits=commits,
                step=CommandStep(
                    key=f"git_push_{remote}",
                    title=f"推送 Git 分支到 {remote}",
                    program=git_program,
                    arguments=tuple(arguments),
                    working_directory=root,
                ),
            )
        )
    return plans


def _replace_exact(
    content: str,
    pattern: str,
    replacement: str,
    *,
    path: Path,
    expected_count: int = 1,
    flags: int = 0,
) -> str:
    updated, count = re.subn(pattern, replacement, content, flags=flags)
    if count != expected_count:
        raise PublisherError(
            f"{path}：版本字段数量异常，应为 {expected_count}，实际为 {count}"
        )
    return updated


def set_project_version(
    repo_root: str | Path,
    new_version: str,
    *,
    require_clean: bool = True,
) -> list[Path]:
    root = Path(repo_root).resolve()
    new_key = _version_key(new_version)
    current = project_version(root)
    if new_key <= _version_key(current):
        raise PublisherError(f"新版本 {new_version} 必须高于当前版本 {current}")
    if require_clean:
        changes = git_status(root)
        if changes:
            raise PublisherError("同步版本号前必须提交或清理现有工作区变更")

    simple_targets = {
        root / "integrated_client" / "config.py": (
            r'(?m)^(APP_VERSION\s*=\s*")[^"]+(")\s*$',
            rf'\g<1>{new_version}\g<2>',
            1,
        ),
        root / "integrated_client" / "__init__.py": (
            r'(?m)^(__version__\s*=\s*")[^"]+(")\s*$',
            rf'\g<1>{new_version}\g<2>',
            1,
        ),
        root / "server" / "app" / "__init__.py": (
            r'(?m)^(__version__\s*=\s*")[^"]+(")\s*$',
            rf'\g<1>{new_version}\g<2>',
            1,
        ),
        root / "server" / "pyproject.toml": (
            r'(?m)^(version\s*=\s*")[^"]+(")\s*$',
            rf'\g<1>{new_version}\g<2>',
            1,
        ),
        root / "server" / "app" / "main.py": (
            r'(?m)(version=")[^"]+(")|(return \{"status": "live", "version": ")[^"]+(")',
            lambda match: (
                f'{match.group(1)}{new_version}{match.group(2)}'
                if match.group(1)
                else f'{match.group(3)}{new_version}{match.group(4)}'
            ),
            2,
        ),
        root / "server" / "app" / "schemas.py": (
            r'(client_version: str = Field\(default=")[^"]+(",)',
            rf'\g<1>{new_version}\g<2>',
            1,
        ),
        root / "installer" / "intdemo.iss": (
            r'(?m)^(\s*#define MyAppVersion ")[^"]+(")\s*$',
            rf'\g<1>{new_version}\g<2>',
            1,
        ),
        root / "docker-compose.yml": (
            r"(?m)^(\s*image: intdemo-api:)[^\s]+\s*$",
            rf"\g<1>{new_version}",
            1,
        ),
    }
    for script_name in (
        "build-installer.ps1",
        "build-portable.ps1",
        "build-releases.ps1",
        "build-online-test.ps1",
    ):
        simple_targets[root / "scripts" / script_name] = (
            r'(?m)^(\s*\[string\]\$Version = ")[^"]+(",)\s*$',
            rf'\g<1>{new_version}\g<2>',
            1,
        )

    staged: dict[Path, str] = {}
    for path, (pattern, replacement, count) in simple_targets.items():
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PublisherError(f"无法读取版本文件：{path}") from exc
        staged[path] = _replace_exact(
            content,
            pattern,
            replacement,
            path=path,
            expected_count=count,
        )

    version_info = root / "installer" / "version_info.txt"
    content = version_info.read_text(encoding="utf-8")
    parts = (*new_key, 0)
    tuple_text = ", ".join(str(item) for item in parts)
    content = _replace_exact(
        content,
        r"(?m)^(\s*filevers=\()[^)]+(\),)\s*$",
        rf"\g<1>{tuple_text}\g<2>",
        path=version_info,
    )
    content = _replace_exact(
        content,
        r"(?m)^(\s*prodvers=\()[^)]+(\),)\s*$",
        rf"\g<1>{tuple_text}\g<2>",
        path=version_info,
    )
    content = _replace_exact(
        content,
        r"(StringStruct\(u'FileVersion', u')[^']+('\))",
        rf"\g<1>{new_version}\g<2>",
        path=version_info,
    )
    content = _replace_exact(
        content,
        r"(StringStruct\(u'ProductVersion', u')[^']+('\))",
        rf"\g<1>{new_version}\g<2>",
        path=version_info,
    )
    staged[version_info] = content

    written: list[Path] = []
    for path, content in staged.items():
        temporary = path.with_suffix(path.suffix + ".release-publisher.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
            written.append(path)
        finally:
            temporary.unlink(missing_ok=True)
    return written


def find_inno_compiler(configured: str = "") -> Path | None:
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_file() else None
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("ProgramFiles")
    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Programs" / "Inno Setup 6" / "ISCC.exe"
        )
    if program_files:
        candidates.append(Path(program_files) / "Inno Setup 6" / "ISCC.exe")
    if program_files_x86:
        candidates.append(Path(program_files_x86) / "Inno Setup 6" / "ISCC.exe")
    return next((path.resolve() for path in candidates if path.is_file()), None)


def is_native_uos_arm64_builder() -> bool:
    machine = platform.machine().strip().casefold()
    return sys.platform.startswith("linux") and machine in {"aarch64", "arm64"}


def validate_test_environment(repo_root: str | Path) -> list[str]:
    root = Path(repo_root).resolve()
    errors: list[str] = []
    try:
        current = project_version(root)
    except PublisherError as exc:
        errors.append(str(exc))
    else:
        errors.extend(project_version_mismatches(root, current))
    executable = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    for label, path in (
        ("客户端开发环境", root / ".venv" / executable),
        ("服务端测试环境", root / "server" / ".venv" / executable),
    ):
        if not path.is_file():
            errors.append(f"{label}不存在：{path}")
    return list(dict.fromkeys(errors))


def validate_release_options(
    options: ReleaseOptions,
    *,
    for_build: bool = False,
    for_publish: bool = False,
    for_pipeline: bool = False,
) -> list[str]:
    root = options.repo_root.resolve()
    errors: list[str] = []
    try:
        target_key = _version_key(options.version)
    except PublisherError as exc:
        errors.append(str(exc))
        target_key = None

    parsed = urlparse(options.base_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        errors.append("服务地址必须是有效的 HTTPS URL")
    elif not options.ca_bundle:
        try:
            is_ip = ipaddress.ip_address(parsed.hostname) is not None
        except ValueError:
            is_ip = False
        if is_ip:
            errors.append("使用公网 IP 服务地址时必须选择私有 CA 根证书")

    if options.ca_bundle and not Path(options.ca_bundle).expanduser().is_file():
        errors.append(f"CA 根证书不存在：{options.ca_bundle}")
    if options.channel not in {"test", "stable"}:
        errors.append("发布通道只能是 test 或 stable")
    if (for_publish or for_pipeline) and not str(options.notes or "").strip():
        errors.append("更新说明不能为空")

    if not options.build_windows and not options.build_uos:
        errors.append("请至少选择一个构建平台")
    if (for_publish or for_pipeline) and not (
        options.build_windows and options.build_uos
    ):
        errors.append("双端发布必须同时选择 Windows x64 和 UOS ARM64")

    if options.delta_from_version:
        if not options.build_windows:
            errors.append("增量更新仅适用于 Windows，必须选择 Windows x64")
        try:
            delta_key = _version_key(options.delta_from_version)
            if target_key is not None and delta_key >= target_key:
                errors.append("增量来源版本必须低于目标版本")
        except PublisherError as exc:
            errors.append(str(exc))
        snapshot = (
            root
            / "dist"
            / "release-snapshots"
            / f"{options.delta_from_version}.json"
        )
        if (for_build or for_publish or for_pipeline) and not snapshot.is_file():
            errors.append(f"缺少增量来源快照：{snapshot}")

    if options.inno_compiler and not Path(options.inno_compiler).expanduser().is_file():
        errors.append(f"Inno Setup 编译器不存在：{options.inno_compiler}")
    if (
        for_build
        and options.build_windows
        and find_inno_compiler(options.inno_compiler) is None
    ):
        errors.append("未找到 Inno Setup 6 编译器")
    if for_build and options.build_windows and os.name != "nt":
        errors.append("Windows 安装包必须在 Windows x64 主机上构建")

    if options.build_uos:
        if options.uos_builder_path and not _safe_remote_path(
            options.uos_builder_path
        ):
            errors.append("UOS 构建仓库目录必须是安全的绝对 Linux 路径")
        if for_build and not is_native_uos_arm64_builder():
            if not options.uos_builder_host:
                errors.append("非 UOS ARM64 主机必须填写 UOS SSH 构建主机")
            elif not REMOTE_HOST_PATTERN.fullmatch(options.uos_builder_host):
                errors.append("UOS SSH 构建主机格式无效")
            if not _safe_remote_path(options.uos_builder_path):
                errors.append("UOS 构建仓库目录必须是安全的绝对 Linux 路径")
            if shutil.which("ssh") is None or shutil.which("scp") is None:
                errors.append("远程构建 UOS ARM64 包需要系统提供 ssh 和 scp")

    try:
        current = project_version(root)
    except PublisherError as exc:
        errors.append(str(exc))
    else:
        if current != options.version:
            errors.append(f"项目当前版本是 {current}，不是目标版本 {options.version}")
        errors.extend(project_version_mismatches(root, options.version))

    if for_publish:
        if options.build_windows and not options.full_installer.is_file():
            errors.append(f"完整安装包不存在：{options.full_installer}")
        if options.delta_installer is not None and not options.delta_installer.is_file():
            errors.append(f"增量安装包不存在：{options.delta_installer}")
        if options.build_uos and not options.uos_installer.is_file():
            errors.append(f"UOS ARM64 安装包不存在：{options.uos_installer}")
    if for_publish or for_pipeline:
        if options.snapshot_path.exists():
            errors.append(
                f"版本 {options.version} 已存在发布快照；已发布版本不可覆盖"
            )
        if options.remote_host:
            if not REMOTE_HOST_PATTERN.fullmatch(options.remote_host):
                errors.append("远程主机格式无效")
            if shutil.which("ssh") is None or shutil.which("scp") is None:
                errors.append("远程发布需要系统提供 ssh 和 scp")
            try:
                changes = git_status(root)
            except PublisherError as exc:
                errors.append(str(exc))
            else:
                if changes:
                    errors.append("远程发布要求 Git 工作区无未提交变更")
    if for_build and options.build_uos and not is_native_uos_arm64_builder():
        try:
            changes = git_status(root)
        except PublisherError as exc:
            errors.append(str(exc))
        else:
            if changes:
                errors.append("UOS 远程构建要求 Git 工作区无未提交变更")

    if not _safe_remote_path(options.remote_path):
        errors.append("远程更新目录必须是安全的绝对 Linux 路径")
    if options.identity_file and not Path(options.identity_file).expanduser().is_file():
        errors.append(f"SSH 私钥不存在：{options.identity_file}")

    errors.extend(validate_test_environment(root))
    return list(dict.fromkeys(errors))


def validate_pause_distribution_options(options: ReleaseOptions) -> list[str]:
    errors: list[str] = []
    if not options.remote_host:
        errors.append("暂停分发必须填写 SSH 主机")
    elif not REMOTE_HOST_PATTERN.fullmatch(options.remote_host):
        errors.append("远程主机格式无效")
    if options.channel not in {"test", "stable"}:
        errors.append("发布通道只能是 test 或 stable")
    if not _safe_remote_path(options.remote_path):
        errors.append("远程更新目录必须是安全的绝对 Linux 路径")
    if options.identity_file and not Path(options.identity_file).expanduser().is_file():
        errors.append(f"SSH 私钥不存在：{options.identity_file}")
    if shutil.which("ssh") is None or shutil.which("scp") is None:
        errors.append("暂停分发需要系统提供 ssh 和 scp")
    return list(dict.fromkeys(errors))


def _powershell_step(
    options: ReleaseOptions,
    *,
    key: str,
    title: str,
    script_name: str,
    script_arguments: list[str],
) -> CommandStep:
    script = options.repo_root / "scripts" / script_name
    return CommandStep(
        key=key,
        title=title,
        program="powershell.exe",
        arguments=(
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *script_arguments,
        ),
        working_directory=options.repo_root,
    )


def build_pause_distribution_steps(options: ReleaseOptions) -> list[CommandStep]:
    arguments = [
        "-Channel",
        options.channel,
        "-RemoteHost",
        options.remote_host,
        "-RemotePath",
        options.remote_path,
    ]
    if options.identity_file:
        arguments.extend(["-IdentityFile", options.identity_file])
    return [
        _powershell_step(
            options,
            key="pause_distribution",
            title=f"暂停 {options.channel} 通道分发",
            script_name="pause-update.ps1",
            script_arguments=arguments,
        )
    ]


def build_test_steps(options: ReleaseOptions) -> list[CommandStep]:
    root = options.repo_root
    executable = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return [
        CommandStep(
            key="compile_client",
            title="检查客户端 Python 语法",
            program=str(root / ".venv" / executable),
            arguments=(
                "-m",
                "compileall",
                "-q",
                "main.py",
                "integrated_client",
                "release_publisher",
            ),
            working_directory=root,
        ),
        CommandStep(
            key="test_client",
            title="运行客户端自动化测试",
            program=str(root / ".venv" / executable),
            arguments=("-m", "pytest", "tests", "-q"),
            working_directory=root,
        ),
        CommandStep(
            key="test_server",
            title="运行服务端自动化测试",
            program=str(root / "server" / ".venv" / executable),
            arguments=("-m", "pytest", "-q"),
            working_directory=root / "server",
        ),
    ]


def build_package_steps(options: ReleaseOptions) -> list[CommandStep]:
    steps: list[CommandStep] = []
    if options.build_windows:
        arguments = [
            "-BaseUrl",
            options.base_url,
            "-Version",
            options.version,
        ]
        if options.ca_bundle:
            arguments.extend(["-CaBundle", options.ca_bundle])
        if options.delta_from_version:
            arguments.extend(["-DeltaFromVersion", options.delta_from_version])
        if options.inno_compiler:
            arguments.extend(["-InnoCompiler", options.inno_compiler])
        script_name = (
            "build-releases.ps1"
            if options.build_portable
            else "build-installer.ps1"
        )
        steps.append(
            _powershell_step(
                options,
                key="build_windows_packages",
                title=(
                    "构建 Windows 便携包、完整安装包和增量包"
                    if options.build_portable
                    else "构建 Windows 完整安装包和增量包"
                ),
                script_name=script_name,
                script_arguments=arguments,
            )
        )
    if options.build_uos:
        if is_native_uos_arm64_builder():
            uos_arguments = [
                str(options.repo_root / "scripts/uos-arm64/build.sh"),
                "--base-url",
                options.base_url,
                "--channel",
                options.channel,
            ]
            if options.ca_bundle:
                uos_arguments.extend(["--ca-bundle", options.ca_bundle])
            else:
                uos_arguments.append("--no-ca-bundle")
            steps.append(
                CommandStep(
                    key="build_uos_package",
                    title="构建 UOS ARM64 DEB 安装包",
                    program=shutil.which("bash") or "bash",
                    arguments=tuple(uos_arguments),
                    working_directory=options.repo_root,
                )
            )
        else:
            remote_arguments = [
                "-BuilderHost",
                options.uos_builder_host,
                "-BuilderRepoPath",
                options.uos_builder_path,
                "-Version",
                options.version,
                "-BaseUrl",
                options.base_url,
                "-Channel",
                options.channel,
            ]
            if options.identity_file:
                remote_arguments.extend(["-IdentityFile", options.identity_file])
            if options.ca_bundle:
                remote_arguments.extend(["-CaBundle", options.ca_bundle])
            steps.append(
                _powershell_step(
                    options,
                    key="build_uos_package",
                    title=f"在 {options.uos_builder_host} 构建 UOS ARM64 DEB",
                    script_name="build-uos-remote.ps1",
                    script_arguments=remote_arguments,
                )
            )
    return steps


def build_publish_steps(options: ReleaseOptions) -> list[CommandStep]:
    arguments = [
        "-WindowsInstaller",
        str(options.full_installer),
        "-UosInstaller",
        str(options.uos_installer),
        "-Version",
        options.version,
        "-Notes",
        options.notes.strip(),
        "-Channel",
        options.channel,
        "-RemotePath",
        options.remote_path,
    ]
    if options.mandatory:
        arguments.append("-Mandatory")
    if options.delta_installer is not None:
        arguments.extend(
            [
                "-DeltaInstaller",
                str(options.delta_installer),
                "-DeltaFromVersion",
                options.delta_from_version,
            ]
        )
    if options.remote_host:
        arguments.extend(["-RemoteHost", options.remote_host])
    if options.identity_file:
        arguments.extend(["-IdentityFile", options.identity_file])
    return [
        _powershell_step(
            options,
            key="publish",
            title=(
                f"发布 {options.version} 到 {options.remote_host}"
                if options.remote_host
                else f"生成 {options.version} 本地发布目录"
            ),
            script_name="publish-update.ps1",
            script_arguments=arguments,
        ),
        _powershell_step(
            options,
            key="snapshot",
            title=f"保存 {options.version} 发布快照",
            script_name="save-release-snapshot.ps1",
            script_arguments=["-Version", options.version],
        ),
    ]


def build_release_plan(
    options: ReleaseOptions,
    *,
    include_tests: bool,
    include_build: bool,
    include_publish: bool,
) -> list[CommandStep]:
    steps: list[CommandStep] = []
    if include_tests:
        steps.extend(build_test_steps(options))
    if include_build:
        steps.extend(build_package_steps(options))
    if include_publish:
        steps.extend(build_publish_steps(options))
    return steps
