"""Stable output naming for image extension pipelines."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Iterable


def plan_extended_output_names(source_names: Iterable[str | Path]) -> dict[str, str]:
    """Map source basenames to distinct PNG names while preserving legacy names when unique.

    A source whose stem is unique keeps the historical ``{stem}.png`` output. Sources
    sharing a stem use ``{source-filename}.png`` so different input formats cannot
    overwrite each other.
    """
    names = sorted({Path(name).name for name in source_names})
    by_stem: dict[str, list[str]] = defaultdict(list)
    for name in names:
        by_stem[Path(name).stem].append(name)

    planned = {
        name: (
            f"{Path(name).stem}.png"
            if len(by_stem[Path(name).stem]) == 1
            else f"{name}.png"
        )
        for name in names
    }

    # A qualified name can itself equal another source's legacy output (for example,
    # ``cover.jpg`` and ``cover.jpg.png``). Hash only those secondary conflicts.
    by_output: dict[str, list[str]] = defaultdict(list)
    for name, output in planned.items():
        by_output[output].append(name)
    conflicts = {
        name
        for owners in by_output.values()
        if len(owners) > 1
        for name in owners
    }
    used_outputs = {
        output
        for output, owners in by_output.items()
        if len(owners) == 1
    }
    for name in sorted(conflicts):
        attempt = 0
        while True:
            digest = hashlib.sha256(f"{attempt}:{name}".encode("utf-8")).hexdigest()
            candidate = f"{Path(name).stem}--{digest}.png"
            if candidate not in used_outputs:
                planned[name] = candidate
                used_outputs.add(candidate)
                break
            attempt += 1

    return planned
