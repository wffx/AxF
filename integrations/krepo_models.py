from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class KRepoCommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def diagnostic(self, limit: int = 4000) -> str:
        text = (self.stderr or self.stdout or f"kRepo command exited {self.returncode}").strip()
        return text[-limit:] if text else f"kRepo command exited {self.returncode}"


@dataclass(frozen=True)
class KRepoArtifact:
    name: str
    path: Path
    output_key: str


@dataclass(frozen=True)
class KRepoStepResult:
    provider: str
    artifacts: tuple[KRepoArtifact, ...]
    commands: tuple[KRepoCommandResult, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(command.ok for command in self.commands)

    def outputs(self) -> dict[str, str]:
        return {artifact.output_key: str(artifact.path) for artifact in self.artifacts}


@dataclass(frozen=True)
class KRepoReport:
    json_path: Path | None = None
    markdown_path: Path | None = None


@dataclass(frozen=True)
class KRepoParams:
    text_path: Path


@dataclass(frozen=True)
class KRepoCalls:
    text_path: Path


@dataclass(frozen=True)
class KRepoSubsource:
    source_path: Path
