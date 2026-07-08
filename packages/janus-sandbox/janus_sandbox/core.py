import logging
import subprocess
import tempfile
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class SandboxConfig:
    """Configuration limits for the Docker sandbox."""
    image: str = "python:3.12-slim"
    memory_limit: str = "512m"
    cpu_limit: str = "1"
    pid_limit: int = 128
    timeout_seconds: int = 60

class SandboxRunner:
    """Runs untrusted code inside a highly restricted Docker container."""
    
    def __init__(self, config: SandboxConfig = None):
        self.config = config or SandboxConfig()

    def is_docker_available(self) -> bool:
        """Check if Docker is available on this host."""
        try:
            proc = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def create_sandbox_copy(self, repo_dir: str | Path) -> Path:
        """Copy the repo into an isolated temp dir so edits don't touch the working tree."""
        tmp = Path(tempfile.mkdtemp(prefix="adv_review_sandbox_"))
        shutil.copytree(repo_dir, tmp, dirs_exist_ok=True)
        return tmp

    def run_containerized(
        self, cmd: list[str], repo_dir: Path, timeout: int | None = None
    ) -> tuple[int, str]:
        """Execute a command inside a locked-down Docker container.
        
        The container:
        - Mounts repo_dir as /workspace (read-write)
        - Has no network access (--network none)
        - Has CPU, memory, and PID limits
        - Is read-only except /workspace and /tmp
        - Is automatically removed after execution
        """
        if not self.is_docker_available():
            logger.error("Docker is required but unavailable. Failing securely.")
            return 1, "GATE ERROR: Docker is required but unavailable. Failing securely."
            
        effective_timeout = timeout or self.config.timeout_seconds
        docker_cmd = [
            "docker", "run",
            "--rm",
            "--network", "none",
            "--memory", self.config.memory_limit,
            "--cpus", self.config.cpu_limit,
            "--pids-limit", str(self.config.pid_limit),
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "-v", f"{repo_dir.resolve()}:/workspace:rw",
            "-w", "/workspace",
            self.config.image,
        ] + cmd

        try:
            proc = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode, output.strip()
        except subprocess.TimeoutExpired:
            return 1, f"TIMEOUT after {effective_timeout}s running containerized: {' '.join(cmd)}"
        except FileNotFoundError:
            logger.error("Docker binary not found.")
            return 1, "GATE ERROR: Docker is required but not found. Failing securely."
