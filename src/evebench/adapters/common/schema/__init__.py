"""Internal source-schema interpretation shared by backend compilers."""

from .models import SchemaCatalog
from .parser import parse_schema

__all__ = ["SchemaCatalog", "parse_schema"]
