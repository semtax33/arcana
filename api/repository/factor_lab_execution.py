"""Bound repeated ClickHouse CTE expansion with session-local shared results."""
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
import re
import uuid


def configure_factor_lab_client(client):
    """Bound every statement on this run's client, including intermediate SELECTs.

    Keep stricter caller/server limits (notably isolated resource tests).
    Clients without the HTTP settings interface remain usable test doubles.
    """
    setter = getattr(client, "set_client_setting", None)
    getter = getattr(client, "get_client_setting", None)
    if not callable(setter) or not callable(getter):
        return
    for name, limit in {
        "max_threads": 2,
        "max_memory_usage": 2 * 1024**3,
        "max_bytes_before_external_group_by": 256 * 1024**2,
        "max_bytes_before_external_sort": 256 * 1024**2,
    }.items():
        current = getter(name)
        if current is None:
            setting = getattr(client, "server_settings", {}).get(name)
            current = getattr(setting, "value", None)
        try:
            existing = int(current)
        except (TypeError, ValueError):
            existing = 0
        setter(name, min(existing, limit) if existing > 0 else limit)


@contextmanager
def materialize_factor_lab_query(client, compiled, *, disk_backed=None):
    """Compute shared CTEs once, preserving the compiler's SQL and row semantics.

    Regular ClickHouse CTEs inline their entire dependency tree at every use.
    Temporary tables cut those repeated branches without experimental SQL
    features. They belong to this client's session and are removed on failure
    as well as success; ancestors are released after their last consumer.
    """
    configure_factor_lab_client(client)
    if not compiled.common_table_expressions:
        yield compiled
        return

    definitions = {}
    for cte in compiled.common_table_expressions:
        name, body = cte.split(" AS (", 1)
        definitions[name] = body[:-1]
    # Names come from validated compiler identifiers. Parameter names and SQL
    # string literals must not be interpreted as CTE references.
    tokens = re.compile(r"'([^'\\]|\\.|'')*'|\{[^}]*\}|\b[A-Za-z_][A-Za-z0-9_]*\b")

    def references(sql):
        return [m.group() for m in tokens.finditer(sql) if m.group() in definitions]

    def reachable(sql):
        found = set()
        pending = references(sql)
        while pending:
            name = pending.pop()
            if name not in found:
                found.add(name)
                pending.extend(references(definitions[name]))
        return found

    def render(sql):
        needed = reachable(sql)
        ctes = [f"{name} AS ({body})" for name, body in definitions.items() if name in needed]
        return ("WITH\n" + ",\n".join(ctes) + "\n" if ctes else "") + sql

    needed = reachable(compiled.result_query)
    uses = Counter(references(compiled.result_query))
    for name in needed:
        uses.update(references(definitions[name]))
    # Stage node outputs even when used only once: otherwise a wide final
    # weighted score still constructs all of its expensive branches together.
    node_outputs = {"node_" + name for name in compiled.execution_order}
    shared = [name for name in definitions if name in needed
              and (uses[name] > 1 or name in node_outputs)]
    if disk_backed is None:
        disk_backed = ("trade_dates" in compiled.parameters
                       or "temporal_end_date" in compiled.parameters
                       or compiled.parameters.get("start_date") != compiled.parameters.get("end_date"))
    engine = "MergeTree ORDER BY tuple()" if disk_backed else "Memory"
    created = {}
    prefix = "lab_stage_" + uuid.uuid4().hex
    try:
        for index, name in enumerate(shared):
            table = f"{prefix}_{index}"
            stage_query = render(f"SELECT * FROM {name}")
            # Retain the existing workaround for composed lab-factor reads.
            if "FROM factor_lab_values AS f" in stage_query:
                stage_query += "\nSETTINGS query_plan_enable_optimizations = 0"
            # Register first: CREATE AS SELECT can create the table before its
            # SELECT fails, and that partially created table also needs cleanup.
            created[name] = table
            client.command(f"CREATE TEMPORARY TABLE {table} ENGINE={engine} AS\n{stage_query}",
                           parameters=compiled.parameters)
            definitions[name] = f"SELECT * FROM {table}"
            remaining = reachable(compiled.result_query)
            for obsolete in list(created):
                if obsolete not in remaining:
                    client.command(f"DROP TEMPORARY TABLE IF EXISTS {created[obsolete]}")
                    del created[obsolete]
        yield replace(compiled, query=render(compiled.result_query))
    finally:
        for table in reversed(list(created.values())):
            try:
                client.command(f"DROP TEMPORARY TABLE IF EXISTS {table}")
            except Exception:
                # A lost server connection must not mask the original failure.
                # These tables are session-local and expire with the session.
                pass
