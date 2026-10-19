"""Reuse CI-built base images while still building and running real fixtures."""

import subprocess


def run_compose(command, arguments, *, env, cwd, timeout, fixture_agent=False):
    arguments = list(arguments)
    if env.get("GROUNDWORK_CI_PREBUILT_IMAGES") == "1":
        if arguments[0] == "build":
            arguments = ["build", *[
                service for service in arguments[1:]
                if service != "runtime-environment"
                and (service != "runtime-agent" or fixture_agent)
            ]]
            if len(arguments) == 1:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        elif arguments[0] == "up":
            arguments = [argument for argument in arguments if argument != "--build"]
            if "--no-build" not in arguments:
                arguments.insert(1, "--no-build")
    return subprocess.run([*command, *arguments], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=timeout)
