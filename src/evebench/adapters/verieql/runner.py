from __future__ import annotations

import builtins
import itertools
import json
import sys
import warnings
from pathlib import Path
from typing import Any


def main() -> None:
    payload = json.load(sys.stdin)
    root = Path(payload["root"]).resolve()
    sys.path.insert(0, str(root))

    # VeriEQL ships a performance-patched Z3 wrapper whose API is required by
    # constants.py. Keep that override local to this isolated child process.
    warnings.filterwarnings(
        "ignore",
        message="pkg_resources is deprecated as an API.*",
        category=UserWarning,
    )
    import pkg_resources

    original_resource_filename = pkg_resources.resource_filename

    def z3_resource_filename(package: str, resource: str) -> str:
        if package == "z3" and resource == "lib":
            return payload["z3_library_path"]
        return original_resource_filename(package, resource)

    pkg_resources.resource_filename = z3_resource_filename
    from z3py_libs import z3 as verieql_z3

    sys.modules["z3"] = verieql_z3

    from environment import Environment
    from errors import (
        NotSupportedError,
        SyntaxError as VeriEQLSyntaxError,
        UnknownError,
    )

    try:
        with Environment(
            generate_code=True,
            timer=True,
            show_counterexample=False,
            semantics="bag",
            dialect=_dialect(payload["dialect"]),
        ) as environment:
            for table_name, columns in payload["schema"].items():
                environment.create_database(
                    attributes=columns,
                    bound_size=payload["bound_size"],
                    name=table_name,
                )
            constraints = _install_unique_constraints(
                environment,
                payload["constraints"],
            )
            environment.add_constraints(constraints)
            environment.save_checkpoints()
            environment.reload_checkpoints()
            result = environment.analyze(
                payload["sql1"],
                payload["sql2"],
            )
            output = {
                "state": "equivalent" if result is True else "not_equivalent",
                "bound_size": payload["bound_size"],
                "traversing_time_s": environment.traversing_time,
                "solving_time_s": environment.solving_time,
            }
            if result is not True and environment.counterexample_dict:
                output["counterexample"] = dict(environment.counterexample_dict)
    except (VeriEQLSyntaxError, builtins.SyntaxError) as exc:
        output = {"state": "syntax_error", "reason": str(exc)}
    except NotSupportedError as exc:
        output = {"state": "unsupported", "reason": str(exc)}
    except UnknownError as exc:
        output = {"state": "unknown", "reason": str(exc)}
    except NotImplementedError as exc:
        output = {"state": "unsupported", "reason": str(exc)}
    except Exception as exc:
        output = {
            "state": "error",
            "reason": f"{type(exc).__name__}: {exc}",
        }

    print(json.dumps(output, separators=(",", ":"), default=_json_default))


def _dialect(dialect: str) -> str:
    return {
        "postgres": "postgresql",
    }.get(dialect.lower(), dialect.lower())


def _install_unique_constraints(environment, constraints: list[dict]) -> list[dict]:
    """Install SQL UNIQUE facts without adding PRIMARY KEY nullability."""
    from constants import Or

    ordinary: list[dict] = []
    for constraint in constraints:
        if set(constraint) != {"unique"}:
            ordinary.append(constraint)
            continue

        operands = constraint["unique"]
        names = [operand["value"] for operand in operands]
        table_names = {name.partition("__")[0] for name in names}
        if len(table_names) != 1:
            raise NotImplementedError(f"Malformed unique constraint: {constraint}")
        table = environment.databases[next(iter(table_names))]
        attributes = []
        for name in names:
            attribute = next((item for item in table.attributes if item == name), None)
            if attribute is None:
                raise NotImplementedError(f"Unknown unique attribute: {name}")
            attributes.append(attribute)

        for left, right in itertools.combinations(table.tuples, 2):
            nullable_or_distinct = []
            for attribute in attributes:
                nullable_or_distinct.extend(
                    (
                        attribute.NULL(left.SORT),
                        attribute.NULL(right.SORT),
                        attribute.VALUE(left.SORT) != attribute.VALUE(right.SORT),
                    )
                )
            environment.DBMS_facts.append(Or(*nullable_or_distinct))
    return ordinary


def _json_default(value: Any) -> str:
    return str(value)


if __name__ == "__main__":
    main()
