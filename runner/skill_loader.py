from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SkillConfigError
from .simple_yaml import load_simple_yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SKILLS_ROOT = PROJECT_ROOT / "skills"


@dataclass(frozen=True)
class SkillSpec:
    name: str
    root: Path
    instructions: str
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def default_lane(self) -> str:
        return str(self.config.get("default_lane") or self.config.get("lane") or "control")

    @property
    def executor(self) -> str:
        return str(self.config.get("executor") or "")

    @property
    def entrypoint(self) -> str:
        return str(self.config.get("entrypoint") or "")


def load_skill(name: str, skills_root: Path | None = None) -> SkillSpec:
    root = (skills_root or DEFAULT_SKILLS_ROOT) / name
    if not root.is_dir():
        raise SkillConfigError(f"skill not found: {name}")
    instructions_path = root / "SKILL.md"
    if not instructions_path.exists():
        raise SkillConfigError(f"SKILL.md not found for skill: {name}")
    config_path = root / "skill.yaml"
    config = load_simple_yaml(config_path) if config_path.exists() else {}
    configured_name = str(config.get("name") or name)
    if configured_name != name:
        raise SkillConfigError(f"skill name mismatch: {configured_name} != {name}")
    return SkillSpec(
        name=name,
        root=root,
        instructions=instructions_path.read_text(encoding="utf-8"),
        config=config,
    )


def skill_exists(name: str, skills_root: Path | None = None) -> bool:
    try:
        load_skill(name, skills_root)
    except SkillConfigError:
        return False
    return True
