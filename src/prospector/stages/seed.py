"""Stage 0 -- seed: load a company list the user already has.

Optional. Most projects start from a prompt and let the discover stage find the
companies, but when you already have a list -- a trade-show exhibitor list, a
conference delegate list, an export from a CRM -- this loads it and skips
straight to the research.

Accepts whatever you have: JSON (a bare list, or an object with a companies /
exhibitors / items / data key), CSV, TSV, or one name per line.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from rich.console import Console

from prospector.database import get_connection, upsert_lead

log = logging.getLogger(__name__)
console = Console()

_NAME_HEADERS = ("company", "company name", "organisation", "organization",
                 "exhibitor", "exhibitor name", "name", "companies", "account")


def _from_json(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(data, dict):
        for key in ("companies", "exhibitors", "leads", "items", "data", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise ValueError(
                "That JSON file has no list of companies in it. Expected a plain "
                "list, or an object with a 'companies' key."
            )

    out: list[str] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            name = next((item[k] for k in item
                         if k.lower() in _NAME_HEADERS and item[k]), "")
            if name:
                out.append(str(name).strip())
    return out


def _from_delimited(path: Path) -> list[str]:
    delimiter = "\t" if path.suffix.lower() in (".tsv", ".tab") else ","
    with path.open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            has_header = csv.Sniffer().has_header(sample) if sample.strip() else False
        except csv.Error:
            has_header = False
        rows = list(csv.reader(fh, delimiter=delimiter))

    if not rows:
        return []

    name_idx, start = 0, 0
    if has_header:
        header = [h.strip().lower() for h in rows[0]]
        start = 1
        for i, h in enumerate(header):
            if h in _NAME_HEADERS:
                name_idx = i
                break

    return [row[name_idx].strip() for row in rows[start:]
            if row and name_idx < len(row) and row[name_idx].strip()]


def _from_text(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def load_names(path: str | Path) -> list[str]:
    """Parse a company list file into a list of names."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".json":
        return _from_json(path)
    if suffix in (".csv", ".tsv", ".tab"):
        return _from_delimited(path)
    return _from_text(path)


def run_seed(list_path: str | Path, source_label: str = "") -> dict:
    """Load companies into the database. Re-running adds only new names."""
    names = load_names(list_path)
    label = source_label or Path(list_path).name

    conn = get_connection()
    created = sum(1 for name in names
                  if upsert_lead(name, source_list=label, conn=conn))

    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    console.print(
        f"  Loaded [bold]{created}[/bold] new companies from {label} "
        f"({len(names)} names in file, {total} total)"
    )
    return {"status": "ok", "new": created, "in_file": len(names), "total": total}
