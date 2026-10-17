import platform

from nexus.contracts.tools import SystemInfoResult


def system_info() -> SystemInfoResult:
    return SystemInfoResult(
        os_name=platform.system(),
        os_version=platform.version(),
        architecture=platform.machine(),
        python_version=platform.python_version(),
    )
