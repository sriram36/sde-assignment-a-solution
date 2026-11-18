"""Workflow lead schemas and the disposition catalogue."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .parsing import normalise_header


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    values: tuple = ()
    default: Optional[str] = None
    aliases: tuple = ()

    @property
    def header_keys(self) -> set[str]:
        return {normalise_header(self.name)} | {normalise_header(a) for a in self.aliases}


@dataclass(frozen=True)
class Schema:
    workflow_id: str
    fields: tuple

    @property
    def identity_field(self) -> Field:
        return next(f for f in self.fields if f.type == "phone")

    @property
    def reference_field(self) -> Field:
        return next(f for f in self.fields if f.reference)


def load_schema(path: Path) -> Schema:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    fields = tuple(
        Field(name=f["name"], type=f["type"], required=bool(f.get("required", False)),
              pii=bool(f.get("pii", False)), reference=bool(f.get("reference", False)),
              values=tuple(f.get("values", ())), default=f.get("default"),
              aliases=tuple(f.get("aliases", ())))
        for f in d["fields"])
    s = Schema(workflow_id=d["workflow_id"], fields=fields)
    if not any(f.type == "phone" for f in fields):
        raise ConfigError(f"{path}: schema needs a phone field (lead identity)")
    if not any(f.reference for f in fields):
        raise ConfigError(f"{path}: schema needs a reference field")
    return s


def load_schemas(schema_dir: Path) -> dict[str, Schema]:
    out = {}
    for p in sorted(Path(schema_dir).glob("*.json")):
        s = load_schema(p)
        out[s.workflow_id] = s
    if not out:
        raise ConfigError(f"no *.json schemas found in {schema_dir}")
    return out


@dataclass
class DispositionRow:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


def _b(v: str) -> bool:
    return v.strip().lower() in ("true", "1", "yes")


def load_dispositions(path: Path) -> tuple[list[DispositionRow], str]:
    raw = Path(path).read_bytes()
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            rows.append(DispositionRow(r["workflow_id"].strip(), r["code"].strip(), r["label"].strip(),
                                       _b(r["is_terminal"]), _b(r["is_success"])))
    return rows, hashlib.sha256(raw).hexdigest()
