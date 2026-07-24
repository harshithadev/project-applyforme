from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


LABEL = "com.applyforme.local-agent"
PLIST_NAME = f"{LABEL}.plist"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ServicePaths:
    project_root: Path
    runner: Path
    launch_agents_dir: Path
    plist: Path
    log_dir: Path
    stdout_log: Path
    stderr_log: Path


def service_paths(
    project_root: Path | None = None,
    home: Path | None = None,
    launch_agents_dir: Path | None = None,
) -> ServicePaths:
    root = (project_root or PROJECT_ROOT).expanduser().resolve()
    user_home = (home or Path.home()).expanduser().resolve()
    agents = (
        launch_agents_dir.expanduser().resolve()
        if launch_agents_dir
        else user_home / "Library" / "LaunchAgents"
    )
    log_dir = root / "data" / "logs"
    return ServicePaths(
        project_root=root,
        runner=root / "scripts" / "run-service",
        launch_agents_dir=agents,
        plist=agents / PLIST_NAME,
        log_dir=log_dir,
        stdout_log=log_dir / "launch-agent.out.log",
        stderr_log=log_dir / "launch-agent.err.log",
    )


def _service_path() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def _domain_path() -> str:
    return f"gui/{os.getuid()}"


def _environment(project_root: Path, home: Path) -> dict[str, str]:
    path_parts = [
        str(project_root / ".venv" / "bin"),
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    return {
        "APPLYFORME_ROOT": str(project_root),
        "HOME": str(home),
        "PATH": ":".join(path_parts),
        "PYTHONUNBUFFERED": "1",
    }


def launch_agent_definition(paths: ServicePaths, home: Path | None = None) -> dict[str, Any]:
    user_home = (home or Path.home()).expanduser().resolve()
    return {
        "Label": LABEL,
        "ProgramArguments": [str(paths.runner)],
        "WorkingDirectory": str(paths.project_root),
        "EnvironmentVariables": _environment(paths.project_root, user_home),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(paths.stdout_log),
        "StandardErrorPath": str(paths.stderr_log),
    }


def render_launch_agent(paths: ServicePaths, home: Path | None = None) -> bytes:
    return plistlib.dumps(
        launch_agent_definition(paths, home),
        fmt=plistlib.FMT_XML,
        sort_keys=False,
    )


def _launchctl(
    arguments: list[str],
    runner: RunCommand = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(
        ["/bin/launchctl", *arguments],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("The background service installer currently supports macOS only")


def _write_plist(paths: ServicePaths, home: Path | None = None) -> None:
    if not paths.runner.is_file():
        raise RuntimeError(f"Service runner is missing: {paths.runner}")
    paths.launch_agents_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    paths.runner.chmod(paths.runner.stat().st_mode | 0o111)
    temporary = paths.plist.with_suffix(".plist.tmp")
    temporary.write_bytes(render_launch_agent(paths, home))
    temporary.chmod(0o644)
    temporary.replace(paths.plist)


def _parse_print_output(output: str) -> dict[str, object]:
    state_match = re.search(r"^\s*state = ([^\s]+)", output, re.MULTILINE)
    pid_match = re.search(r"^\s*pid = (\d+)", output, re.MULTILINE)
    exit_match = re.search(r"^\s*last exit code = (-?\d+)", output, re.MULTILINE)
    state = state_match.group(1) if state_match else ""
    return {
        "state": state,
        "running": state == "running",
        "pid": int(pid_match.group(1)) if pid_match else None,
        "last_exit_code": int(exit_match.group(1)) if exit_match else None,
    }


def status(
    *,
    project_root: Path | None = None,
    home: Path | None = None,
    launch_agents_dir: Path | None = None,
    runner: RunCommand = subprocess.run,
) -> dict[str, object]:
    paths = service_paths(project_root, home, launch_agents_dir)
    supported = sys.platform == "darwin"
    result: dict[str, object] = {
        "supported": supported,
        "label": LABEL,
        "installed": paths.plist.is_file(),
        "loaded": False,
        "running": False,
        "state": "",
        "pid": None,
        "last_exit_code": None,
        "plist_path": str(paths.plist),
        "stdout_log": str(paths.stdout_log),
        "stderr_log": str(paths.stderr_log),
        "dashboard_url": "http://127.0.0.1:8787",
    }
    if not supported:
        result["message"] = "Automatic startup is currently supported on macOS only."
        return result
    printed = _launchctl(["print", _service_path()], runner)
    if printed.returncode == 0:
        result["loaded"] = True
        result.update(_parse_print_output(printed.stdout))
    if result["running"]:
        result["message"] = "ApplyForMe is running as a macOS login service."
    elif result["loaded"]:
        result["message"] = "The login service is loaded but is not currently running."
    elif result["installed"]:
        result["message"] = "The login service is installed and will load at the next login."
    else:
        result["message"] = "The macOS login service is not installed."
    return result


def install(
    *,
    activate: bool = True,
    project_root: Path | None = None,
    home: Path | None = None,
    launch_agents_dir: Path | None = None,
    runner: RunCommand = subprocess.run,
) -> dict[str, object]:
    _require_macos()
    paths = service_paths(project_root, home, launch_agents_dir)
    _write_plist(paths, home)
    if activate:
        existing = _launchctl(["print", _service_path()], runner)
        if existing.returncode == 0:
            _launchctl(["bootout", _service_path()], runner)
        bootstrapped = _launchctl(
            ["bootstrap", _domain_path(), str(paths.plist)],
            runner,
        )
        if bootstrapped.returncode != 0:
            raise RuntimeError(
                "Could not load the macOS login service: "
                + (bootstrapped.stderr.strip() or bootstrapped.stdout.strip())
            )
        started = _launchctl(["kickstart", "-k", _service_path()], runner)
        if started.returncode != 0:
            raise RuntimeError(
                "Could not start the macOS login service: "
                + (started.stderr.strip() or started.stdout.strip())
            )
    return status(
        project_root=project_root,
        home=home,
        launch_agents_dir=launch_agents_dir,
        runner=runner,
    )


def restart(runner: RunCommand = subprocess.run) -> dict[str, object]:
    _require_macos()
    current = status(runner=runner)
    if not current["installed"]:
        raise RuntimeError("Install the macOS login service before restarting it")
    if not current["loaded"]:
        return install(runner=runner)
    restarted = _launchctl(["kickstart", "-k", _service_path()], runner)
    if restarted.returncode != 0:
        raise RuntimeError(
            "Could not restart the macOS login service: "
            + (restarted.stderr.strip() or restarted.stdout.strip())
        )
    return status(runner=runner)


def uninstall(
    *,
    deactivate: bool = True,
    project_root: Path | None = None,
    home: Path | None = None,
    launch_agents_dir: Path | None = None,
    runner: RunCommand = subprocess.run,
) -> dict[str, object]:
    _require_macos()
    paths = service_paths(project_root, home, launch_agents_dir)
    if deactivate:
        _launchctl(["bootout", _service_path()], runner)
    if paths.plist.exists():
        paths.plist.unlink()
    return status(
        project_root=project_root,
        home=home,
        launch_agents_dir=launch_agents_dir,
        runner=runner,
    )
