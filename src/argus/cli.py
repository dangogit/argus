"""Engine CLI compatibility entrypoint for the installed Python package."""
import os
import sys
import tempfile
from typing import List, Optional

from argus.engine import (
    OUTAGE_RC,
    UNKNOWN_ENGINE_RC,
    EngineOutageError,
    UnknownEngineError,
)
from argus.engine import router
from argus.engine.adapters import ADAPTERS

USAGE = (
    "usage: argus engine (run|list) [--engine <name>] [--fallback <name>]"
    " [--show-cost] --prompt <text>"
)


def _die(msg: str) -> int:
    print(f"[argus] error: {msg}", file=sys.stderr)
    return 1


def _engine_run(args: List[str]) -> int:
    engine_arg = fallback_arg = None
    prompt = ""
    show_cost = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--engine", "--fallback", "--prompt"):
            if i + 1 >= len(args):
                return _die(f"{a} needs a value")
            val = args[i + 1]
            if a == "--engine":
                engine_arg = val
            elif a == "--fallback":
                fallback_arg = val
            else:
                prompt = val
            i += 2
        elif a == "--show-cost":
            show_cost = True
            i += 1
        else:
            return _die(f"unknown engine run option: {a}")

    primary = router.default_engine(engine_arg)
    fallback = router.fallback_engine(fallback_arg)

    # Scope ARGUS_ENGINE_META to this invocation only: save and restore (or
    # remove) the prior value so parallel tests and nested calls stay isolated.
    prior_meta = os.environ.get("ARGUS_ENGINE_META")
    meta_file: Optional[str] = None

    if show_cost:
        fd, meta_file = tempfile.mkstemp(prefix="argus-cost.")
        os.close(fd)
        os.environ["ARGUS_ENGINE_META"] = meta_file

    rc = 0
    try:
        result = router.run_with_fallback(primary, fallback, prompt)
        print(result.text)
    except UnknownEngineError as e:
        print(str(e), file=sys.stderr)
        rc = UNKNOWN_ENGINE_RC
    except EngineOutageError:
        rc = OUTAGE_RC
    finally:
        # Always restore env, whether or not show_cost was set.
        if show_cost:
            if prior_meta is None:
                os.environ.pop("ARGUS_ENGINE_META", None)
            else:
                os.environ["ARGUS_ENGINE_META"] = prior_meta

    if show_cost and meta_file:
        cost_source, cost_usd = "unpriced", ""
        try:
            with open(meta_file, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            for line in lines:
                k, _, v = line.partition("=")
                if k == "costSource" and v:
                    cost_source = v
                elif k == "costUsd":
                    cost_usd = v
        finally:
            try:
                os.unlink(meta_file)
            except OSError:
                pass
        print(f"cost: source={cost_source} usd={cost_usd}")

    return rc


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "engine":
        print(USAGE, file=sys.stderr)
        return 1
    sub = args[1] if len(args) > 1 else ""
    if sub == "list":
        for name in sorted(ADAPTERS):
            print(name)
        return 0
    if sub == "run":
        return _engine_run(args[2:])
    return _die(USAGE)


if __name__ == "__main__":
    raise SystemExit(main())
