from dataclasses import dataclass
from sqlglot import exp


@dataclass(frozen=True)
class SchemaCatalog:
    """Ordered source ASTs with disjoint groups referencing the same ASTs."""

    dialect: str
    statements: tuple[exp.Expression, ...]
    tables: tuple[exp.Create, ...]
    alters: tuple[exp.AlterTable, ...]
    functions: tuple[exp.Expression, ...]
    other_statements: tuple[exp.Expression, ...]
