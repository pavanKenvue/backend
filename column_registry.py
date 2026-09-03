"""
Single source of truth for which column identifiers may reach SQL.

Both the API layer and the query layer import this module. Previously the
allowlist lived in a closure inside `create_app`, so query construction had
no way to consult it and interpolated raw query-string values into
f-strings. Every identifier now resolves through `REGISTRY.resolve()` or it
does not reach a query at all.

The only data file consumed is:

  column_map.json   {"COLUMN_NAME": "pWidgetParam", ...}   (required)
"""
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional
import boto3
import logger

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

COLUMN_MAP_PATH = os.getenv("COLUMN_MAP_PATH", os.path.join(_THIS_DIR, "resources", "column_map.json"))
COLUMN_MAP_KEY = os.getenv("COLUMN_MAP_KEY", "")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
ENABLE_LOCAL_FILE_READ = os.getenv("ENABLE_LOCAL_FILE_READ")

# Orientation of column_map.json. "auto" inspects the data; set explicitly to
# "column_to_param" or "param_to_column" to pin it.
COLUMN_MAP_ORIENTATION = os.getenv("COLUMN_MAP_ORIENTATION", "auto").lower()
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")

class UnknownColumn(ValueError):
    """The requested identifier is not in the allowlist."""

@dataclass(frozen=True)
class ColumnInfo:
    name: str
    params: tuple

    @property
    def param(self) -> str:
        """Primary QuickSight parameter for this column."""
        return self.params[0]


def _read_local_or_s3(local_path: str, s3_key: str, required: bool) -> Optional[dict]:   
    if os.path.exists(local_path) and ENABLE_LOCAL_FILE_READ:
        source = f"local file {local_path}"
        try:
            with open(local_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            raise RuntimeError(f"{source} is not valid JSON: {exc}") from exc
    if S3_BUCKET_NAME:
        source = f"s3://{S3_BUCKET_NAME}/{s3_key}"
        try:
            client = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))
            obj = client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
            return json.loads(obj["Body"].read().decode("utf-8"))
        except Exception as exc:
            if required:
                raise RuntimeError(f"Failed to load {source}: {exc}") from exc
            logger.warn(f"Optional file {source} not loaded: {exc}")
            return None 
    if required:
        raise RuntimeError(
            f"No source for {local_path}: file absent and S3_BUCKET_NAME unset"
        )
    return None


def _looks_like_sql_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_$#]*", value))


def _orient(raw: Dict[str, str]) -> Dict[str, str]:
    if COLUMN_MAP_ORIENTATION == "column_to_param":
        return dict(raw)
    if COLUMN_MAP_ORIENTATION == "param_to_column":
        return {v: k for k, v in raw.items()}

    key_hits = sum(1 for k in raw if _looks_like_sql_identifier(k))
    val_hits = sum(1 for v in raw.values() if _looks_like_sql_identifier(v))

    if key_hits >= val_hits:
        logger.info(
            f"column_map orientation detected: column -> param "
            f"({key_hits}/{len(raw)} keys look like SQL identifiers)"
        )
        return dict(raw)

    logger.info(
        f"column_map orientation detected: param -> column "
        f"({val_hits}/{len(raw)} values look like SQL identifiers)"
    )
    return {v: k for k, v in raw.items()}


class ColumnRegistry:
    def __init__(self, column_to_param: Dict[str, str]):
        self.rejected: List[str] = []
        self.collisions: Dict[str, List[str]] = {}

        params_by_column: Dict[str, List[str]] = {}
        for column, param in column_to_param.items():
            if not _IDENT_RE.match(column):
                self.rejected.append(column)
                continue
            params_by_column.setdefault(column.upper(), []).append(param)

        self._by_name: Dict[str, ColumnInfo] = {}
        for name, params in params_by_column.items():
            if len(params) > 1:
                self.collisions[name] = params
            self._by_name[name] = ColumnInfo(name=name, params=tuple(params))

        if self.rejected:
            logger.error(
                f"Rejected {len(self.rejected)} column_map entries that are not valid "
                f"SQL identifiers (spaces or punctuation in the name): "
                f"{self.rejected}. Verify these against the table schema and fix the map."
            )
        if self.collisions:
            for name, params in self.collisions.items():
                logger.warn(
                    f"Column {name} is mapped to {len(params)} widget params {params}; "
                    f"using {params[0]} as primary. Full list is on /columns.paramMapFull."
                )
        if not self._by_name:
            raise RuntimeError("Column registry is empty -- refusing to start")

        logger.info(
            f"Column registry ready: {len(self._by_name)} columns "
            f"({len(self.rejected)} rejected, {len(self.collisions)} param collisions)"
        )

    def resolve(self, name: Optional[str]) -> str:
        if not name or not isinstance(name, str):
            raise UnknownColumn("column name is required")
        info = self._by_name.get(name.strip().upper())
        if info is None:
            raise UnknownColumn(f"Unknown column: {name!r}")
        return info.name

    def info(self, name: str) -> ColumnInfo:
        return self._by_name[self.resolve(name)]

    def quoted(self, name: str) -> str:
        canonical = self.resolve(name)
        if not _IDENT_RE.match(canonical):
            raise UnknownColumn(f"Refusing to interpolate identifier: {canonical!r}")
        return f'"{canonical}"'

    @property
    def columns(self) -> List[str]:
        return sorted(self._by_name)

    def param_map(self) -> Dict[str, str]:
        """One primary param per column."""
        return {c.name: c.param for c in self._by_name.values()}

    def param_map_full(self) -> Dict[str, List[str]]:
        """Every param mapped to each column, for columns with more than one."""
        return {c.name: list(c.params) for c in self._by_name.values()}

    def diagnostics(self) -> dict:
        return {
            "columnCount": len(self._by_name),
            "rejectedEntries": self.rejected,
            "paramCollisions": self.collisions,
        }

    def describe(self) -> List[dict]:
        return [
            {"column": c.name, "param": c.param, "params": list(c.params)}
            for c in sorted(self._by_name.values(), key=lambda x: x.name)
        ]


def load_registry() -> ColumnRegistry:
    raw = _read_local_or_s3(COLUMN_MAP_PATH, COLUMN_MAP_KEY, required=True)
    if not (
        isinstance(raw, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items())
    ):
        raise RuntimeError("column_map JSON is not a flat object of string -> string")

    return ColumnRegistry(_orient(raw))

REGISTRY = load_registry()
