from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        home = Path(tmp) / "home"
        launch_agents = home / "Library" / "LaunchAgents"
        runner_path = root / "scripts" / "run-service"
        runner_path.parent.mkdir(parents=True)
        runner_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

        from job_agent import service

        paths = service.service_paths(root, home, launch_agents)
        definition = plistlib.loads(service.render_launch_agent(paths, home))
        assert definition["Label"] == service.LABEL
        assert definition["ProgramArguments"] == [str(paths.runner)]
        assert definition["WorkingDirectory"] == str(paths.project_root)
        assert definition["RunAtLoad"] is True and definition["KeepAlive"] is True
        assert definition["ProcessType"] == "Background"
        assert definition["EnvironmentVariables"]["APPLYFORME_ROOT"] == str(paths.project_root)
        assert str(home.resolve() / ".local" / "bin") in definition["EnvironmentVariables"]["PATH"]
        assert "SMTP_PASSWORD" not in definition["EnvironmentVariables"]
        assert definition["StandardOutPath"].endswith("data/logs/launch-agent.out.log")

        installed = service.install(
            activate=False,
            project_root=root,
            home=home,
            launch_agents_dir=launch_agents,
        )
        assert paths.plist.is_file()
        assert os.access(runner_path, os.X_OK)
        assert installed["installed"] is True
        plist = plistlib.loads(paths.plist.read_bytes())
        assert plist == definition

        commands: list[list[str]] = []

        def fake_launchctl(
            command: list[str],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            if command[1:3] == ["print", f"gui/{os.getuid()}/{service.LABEL}"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        f"gui/{os.getuid()}/{service.LABEL} = {{\n"
                        "\tstate = running\n"
                        "\tpid = 4242\n"
                        "\tlast exit code = 0\n"
                        "}\n"
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        running = service.status(
            project_root=root,
            home=home,
            launch_agents_dir=launch_agents,
            runner=fake_launchctl,
        )
        assert running["loaded"] is True
        assert running["running"] is True
        assert running["pid"] == 4242
        assert running["last_exit_code"] == 0

        removed = service.uninstall(
            deactivate=False,
            project_root=root,
            home=home,
            launch_agents_dir=launch_agents,
        )
        assert removed["installed"] is False
        assert not paths.plist.exists()
        assert any(command[1] == "print" for command in commands)

    print("service management ok")


if __name__ == "__main__":
    main()
