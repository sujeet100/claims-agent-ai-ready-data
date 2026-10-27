"""The System of Record's structured half: Apache Iceberg tables, queried with DuckDB.

Iceberg gives the ACID writes, schema enforcement and snapshot history the paper asks of a
governed lakehouse. The catalog is PyIceberg's SQL catalog in SQLite and the warehouse is a
local folder; in production the same tables would sit behind a REST catalog (Polaris,
Lakekeeper, Unity Catalog) on object storage.
"""

from functools import lru_cache

import duckdb
import pandas as pd
import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NoSuchTableError

from claims import config

NAMESPACES = ("bronze", "silver", "gold", "meta")


@lru_cache(maxsize=1)
def catalog() -> SqlCatalog:
    config.WAREHOUSE.mkdir(parents=True, exist_ok=True)
    cat = SqlCatalog("claims", uri=f"sqlite:///{config.CATALOG_DB}", warehouse=f"file://{config.WAREHOUSE}")
    for ns in NAMESPACES:
        cat.create_namespace_if_not_exists(ns)
    return cat


def _to_arrow(df: pd.DataFrame | pa.Table) -> pa.Table:
    table = df if isinstance(df, pa.Table) else pa.Table.from_pandas(df, preserve_index=False)
    fields = []
    for f in table.schema:
        t = f.type
        if pa.types.is_timestamp(t):
            t = pa.timestamp("us")
        elif pa.types.is_null(t):
            t = pa.string()
        elif pa.types.is_large_string(t):
            t = pa.string()
        fields.append(pa.field(f.name, t))
    return table.cast(pa.schema(fields))


def write(name: str, df: pd.DataFrame | pa.Table) -> int:
    """Overwrite an Iceberg table, keeping its snapshot history when the schema is unchanged."""
    data = _to_arrow(df)
    cat = catalog()
    try:
        table = cat.load_table(name)
        if table.schema().as_arrow().equals(data.schema, check_metadata=False):
            table.overwrite(data)
            return data.num_rows
        cat.drop_table(name)
    except NoSuchTableError:
        pass
    cat.create_table(name, schema=data.schema).append(data)
    return data.num_rows


def read(name: str) -> pd.DataFrame:
    return catalog().load_table(name).scan().to_pandas()


def exists(name: str) -> bool:
    try:
        catalog().load_table(name)
        return True
    except NoSuchTableError:
        return False


def tables(namespaces=NAMESPACES) -> list[str]:
    cat = catalog()
    return [".".join(t) for ns in namespaces for t in cat.list_tables(ns)]


def snapshots(name: str) -> list[dict]:
    t = catalog().load_table(name)
    return [{"snapshot_id": s.snapshot_id, "timestamp_ms": s.timestamp_ms, "operation": s.summary.operation.value if s.summary else None}
            for s in t.snapshots()]


def duck(names: list[str] | None = None) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB session with the lakehouse tables loaded under their schemas."""
    con = duckdb.connect()
    for ns in NAMESPACES:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {ns}")
    for name in names or tables():
        arrow = catalog().load_table(name).scan().to_arrow()
        con.register("_t", arrow)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _t")
        con.unregister("_t")
    return con
