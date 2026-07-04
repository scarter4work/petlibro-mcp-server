"""Load pets.toml + env credentials; resolve names to devices."""
from __future__ import annotations
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


class UnknownPetError(Exception):
    def __init__(self, name: str, valid: list[str]):
        super().__init__(
            f"Unknown pet '{name}'. Valid pets: {', '.join(sorted(valid))}"
        )
        self.name = name
        self.valid = valid


class MissingCredentialsError(Exception):
    pass


@dataclass
class Feeder:
    name: str
    serial: str
    mac: str
    chip: str
    portions_per_cup: int


@dataclass
class Fountain:
    name: str
    serial: str
    mac: str
    near: list[str] = field(default_factory=list)


@dataclass
class Config:
    feeders: list[Feeder]
    fountains: list[Fountain]
    region: str
    max_cups_per_command: int
    email: str
    password: str

    def _by_name(self) -> dict[str, Feeder]:
        return {f.name.lower(): f for f in self.feeders}

    def resolve_feeders(self, names) -> list[Feeder]:
        if isinstance(names, str) and names.lower() == "all":
            return list(self.feeders)
        if isinstance(names, str):
            names = [names]
        index = self._by_name()
        valid = [f.name for f in self.feeders]
        out = []
        for n in names:
            key = n.lower()
            if key not in index:
                raise UnknownPetError(n, valid)
            out.append(index[key])
        return out

    def resolve_fountains(self, name=None) -> list[Fountain]:
        if name is None:
            return list(self.fountains)
        for f in self.fountains:
            if f.name.lower() == name.lower():
                return [f]
        raise UnknownPetError(name, [f.name for f in self.fountains])


def cups_to_portions(feeder: Feeder, cups: float) -> int:
    return round(cups * feeder.portions_per_cup)


def load_config(toml_path, env: Mapping[str, str] = os.environ) -> Config:
    data = tomllib.loads(Path(toml_path).read_text())
    defaults = data.get("defaults", {})
    ppc = int(defaults.get("portions_per_cup", 12))
    region = defaults.get("region", "US")
    max_cups = int(defaults.get("max_cups_per_command", 4))

    feeders = [
        Feeder(
            name=p["name"], serial=p["serial"], mac=p.get("mac", ""),
            chip=str(p.get("chip", "")),
            portions_per_cup=int(p.get("portions_per_cup", ppc)),
        )
        for p in data.get("pet", [])
    ]
    fountains = [
        Fountain(name=f["name"], serial=f["serial"], mac=f.get("mac", ""),
                 near=list(f.get("near", [])))
        for f in data.get("fountain", [])
    ]

    email = env.get("PETLIBRO_EMAIL")
    password = env.get("PETLIBRO_PASSWORD")
    if not email or not password:
        raise MissingCredentialsError(
            "Set PETLIBRO_EMAIL and PETLIBRO_PASSWORD (see .env)."
        )
    region = env.get("PETLIBRO_REGION", region)

    return Config(feeders, fountains, region, max_cups, email, password)
