"""准备隔离的本地 Python 环境，再启动只监听回环地址的应用。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv


ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT = ROOT / ".venv-local"
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = ENVIRONMENT / ".notebook-requirements.json"


class BootstrapError(Exception):
    """可直接显示给本地用户的错误。"""


def _is_python312(command):
    try:
        return subprocess.run(
            [*command, "-c", "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _find_python312():
    candidates = []
    if os.name == "nt":
        launcher = shutil.which("py")
        if launcher:
            candidates.append([launcher, "-3.12"])
    for name in ("python3.12", "python", "python3"):
        path = shutil.which(name)
        if path:
            candidates.append([path])
    if os.name == "nt":
        for variable, suffix in (
            ("LOCALAPPDATA", "Programs/Python/Python312/python.exe"),
            ("LOCALAPPDATA", "Programs/Python/Python312-32/python.exe"),
            ("LOCALAPPDATA", "Programs/Python/Python312-arm64/python.exe"),
            ("ProgramFiles", "Python312/python.exe"),
            ("ProgramFiles", "Python312-arm64/python.exe"),
            ("ProgramFiles(x86)", "Python312-32/python.exe"),
        ):
            base = os.environ.get(variable)
            if base:
                candidate = Path(base) / suffix
                if candidate.is_file():
                    candidates.append([str(candidate)])
    for command in candidates:
        if _is_python312(command):
            return command
    raise BootstrapError("没有找到 Python 3.12。Windows 请双击 start-windows.cmd；其他系统请先安装 Python 3.12。")


@contextmanager
def _setup_lock():
    """系统文件锁只覆盖环境准备；关闭进程后自动释放，不锁住应用运行。"""
    identity = hashlib.sha256(str(ROOT).encode("utf-8")).hexdigest()[:24]
    path = Path(tempfile.gettempdir()) / f"notebook-bootstrap-{identity}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as lock:
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b"\0")
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BootstrapError("另一个窗口正在准备本地环境，请等它完成后重试。") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _environment_python():
    return ENVIRONMENT / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _prepare_environment():
    if ENVIRONMENT.is_symlink():
        raise BootstrapError(".venv-local 是符号链接，未对它进行修改；请使用普通项目目录。")
    python = _environment_python()
    if ENVIRONMENT.exists():
        if not (ENVIRONMENT / "pyvenv.cfg").is_file() or not python.is_file():
            raise BootstrapError("已有 .venv-local 不是完整虚拟环境，未覆盖；将该目录改名后重试，详情见 LOCAL.md。")
        if not _is_python312([str(python)]):
            raise BootstrapError("已有 .venv-local 不是可用的 Python 3.12 环境；将该目录改名后重试，数据目录不受影响。")
    else:
        print("正在创建项目独立环境 .venv-local…", flush=True)
        try:
            venv.EnvBuilder(with_pip=True).create(ENVIRONMENT)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise BootstrapError("虚拟环境创建失败，请检查目录写入权限、路径是否含分号，以及 Python 3.12 的 venv/pip 组件。") from None
    return python


def _dependency_state():
    return {
        "requirements_sha256": hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
        "python": list(sys.version_info[:3]),
        "platform": sys.platform,
    }


def _dependencies_ready(python):
    # 使用虚拟环境中 pip 自带的版本/平台标记解析器，避免安装额外引导依赖。
    check = """
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys
from pip._vendor.packaging.requirements import Requirement

try:
    for line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        if version(requirement.name) not in requirement.specifier:
            raise SystemExit(1)
except (PackageNotFoundError, ValueError):
    raise SystemExit(1)
"""
    try:
        direct = subprocess.run([str(python), "-c", check, str(REQUIREMENTS)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=30, check=False)
        if direct.returncode:
            return False
        return subprocess.run(
            [str(python), "-m", "pip", "--disable-pip-version-check", "check"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _prepare_dependencies(python):
    required = _dependency_state()
    try:
        saved = json.loads(STAMP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = None
    if saved == required and _dependencies_ready(python):
        print("依赖已就绪，复用现有环境。", flush=True)
        return
    print("正在安装或核对应用依赖，首次运行需要联网…", flush=True)
    result = subprocess.run(
        [str(python), "-m", "pip", "--disable-pip-version-check", "install", "--no-input", "-r", str(REQUIREMENTS)],
        cwd=ROOT, check=False,
    )
    if result.returncode or not _dependencies_ready(python):
        raise BootstrapError("依赖安装或检查失败。请检查网络和上方错误后重新运行；未修改已有配置或笔记数据。")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=ENVIRONMENT,
                                     prefix=".notebook-requirements-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(required, stream, ensure_ascii=False)
    try:
        temporary.replace(STAMP)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="准备并启动笔记课间的独立本地环境")
    parser.add_argument("--port", type=int, default=18765, help="本机端口，默认 18765")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须为 1 到 65535")
    if sys.version_info[:2] != (3, 12):
        command = _find_python312()
        return subprocess.call([*command, str(Path(__file__).resolve()), *(sys.argv[1:] if argv is None else argv)], cwd=ROOT)
    if not REQUIREMENTS.is_file() or not (ROOT / "scripts/local_app.py").is_file():
        raise BootstrapError("项目文件不完整，请完整下载或更新仓库后重试。")
    with _setup_lock():
        python = _prepare_environment()
        _prepare_dependencies(python)
    command = [str(python), str(ROOT / "scripts/local_app.py"), "--port", str(args.port)]
    if args.no_browser:
        command.append("--no-browser")
    print("本地环境已准备，正在启动应用…", flush=True)
    if os.name != "nt":
        os.execv(str(python), command)
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as error:
        print(f"启动未完成：{error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n本地启动已停止。", file=sys.stderr)
        raise SystemExit(130)
    except OSError:
        print("启动未完成：无法访问文件或运行程序，请检查项目路径、权限和 LOCAL.md。", file=sys.stderr)
        raise SystemExit(1)
