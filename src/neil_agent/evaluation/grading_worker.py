"""Standalone source invocation worker. It does not decide whether a task passes."""

from __future__ import annotations

import contextlib
import importlib
import json
import sys
from pathlib import Path


def decode(value):
    if isinstance(value, dict):
        if set(value) == {"$set"}:
            return set(value["$set"])
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


def encode(value):
    if isinstance(value, set):
        return {"$set": sorted(value)}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Only string mapping keys are supported")
        return {key: encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise ValueError("Unsupported return type")


def mutable_ids(value, seen=None):
    if seen is None:
        seen = set()
    if not isinstance(value, (list, tuple, dict, set)) or id(value) in seen:
        return set()
    if len(seen) >= 1024:
        raise ValueError("Collection graph exceeds limits")
    seen.add(id(value))
    result = {id(value)} if isinstance(value, (list, dict, set)) else set()
    for item in value.values() if isinstance(value, dict) else value:
        result.update(mutable_ids(item, seen))
    return result


def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    sys.path.insert(0, sys.argv[2])
    evidence = []
    # Source prints are diagnostics. They are never accepted as grading receipts.
    with contextlib.redirect_stdout(sys.stderr):
        for case in request["calls"]:
            args = [decode(value) for value in case["args"]]
            value = None
            error_name = None
            function = getattr(importlib.import_module(case["module"]), case["function"])
            try:
                value = function(*args)
            except Exception as error:
                error_name = type(error).__name__
            evidence.append({
                "value": encode(value),
                "error": error_name,
                "args_after": [encode(arg) for arg in args],
                "same_input": any(value is arg for arg in args),
                "shares_input": bool(mutable_ids(value) & mutable_ids(args)),
            })
    print(json.dumps({"challenge": request["challenge"], "calls": evidence}, allow_nan=False))


if __name__ == "__main__":
    main()
