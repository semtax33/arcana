from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DataLakePaths:
    root: Path

    @classmethod
    def from_project_root(cls, project_root: str | Path) -> "DataLakePaths":
        return cls(Path(project_root) / "data-lake")

    def bronze(self, provider: str, *parts: str) -> Path:
        return self.root.joinpath("bronze", provider, *parts)

    def silver(self, provider: str, *parts: str) -> Path:
        return self.root.joinpath("silver", provider, *parts)

    def meta(self, *parts: str) -> Path:
        return self.root.joinpath("meta", *parts)

