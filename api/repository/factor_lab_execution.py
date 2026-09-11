"""Bound repeated ClickHouse CTE expansion with session-local shared results."""
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
import os
import re
import sys
import uuid
from urllib.parse import urlsplit


def configure_factor_lab_client(client):
    """Bound every statement on this run's client, including intermediate SELECTs.

    Keep stricter caller/server limits (notably isolated resource tests).
    Clients without the HTTP settings interface remain usable test doubles.
    """
    setter = getattr(client, "set_client_setting", None)
    getter = getattr(client, "get_client_setting", None)
    if not callable(setter) or not callable(getter):
        return
    # Reused HTTP connections through the Windows/WSL localhost relay add
    # ~40 ms to tiny statements. Session IDs survive a new TCP connection.
    # Keep pooling for remote hosts, where reconnecting can be expensive.
    if (sys.platform == "win32"
            and urlsplit(getattr(client, "url", "")).hostname in {"localhost", "127.0.0.1", "::1"}
            and isinstance(getattr(client, "headers", None), dict)):
        client.headers["Connection"] = "close"
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
    memory_budget = 0
    if disk_backed and callable(getattr(client, "get_client_setting", None)):
        # This caps retained stage data, not the in-flight query's memory.
        # A zero budget preserves the always-on-disk execution policy.
        requested = int(os.getenv("ARCANA_FACTOR_LAB_STAGE_MEMORY_BYTES", str(128 * 1024**2)))
        query_limit = int(client.get_client_setting("max_memory_usage"))
        memory_budget = max(0, min(requested, 128 * 1024**2, query_limit // 4))
    created = {}
    memory_sizes = {}
    spill_tables = set()
    prefix = "lab_stage_" + uuid.uuid4().hex
    try:
        for index, name in enumerate(shared):
            table = f"{prefix}_{index}"
            stage_query = render(f"SELECT * FROM {name}")
            # Retain the existing workaround for composed lab-factor reads.
            if "FROM factor_lab_values AS f" in stage_query:
                stage_query += "\nSETTINGS query_plan_enable_optimizations = 0"
            # HTTP sends bound values in the URL. Sending the entire graph's
            # hundreds of parameters for every small stage adds relay latency.
            parameter_names = {m.group()[1:].split(":", 1)[0] for m in tokens.finditer(stage_query)
                               if m.group().startswith("{")}
            stage_parameters = {key: compiled.parameters[key] for key in parameter_names}
            # Register first: CREATE AS SELECT can create the table before its
            # SELECT fails, and that partially created table also needs cleanup.
            created[name] = table
            use_memory = memory_budget > 0 and sum(memory_sizes.values()) < memory_budget
            stage_engine = "Memory" if use_memory else engine
            try:
                client.command(f"CREATE TEMPORARY TABLE {table} ENGINE={stage_engine} AS\n{stage_query}",
                               parameters=stage_parameters)
            except Exception as exc:
                if not use_memory or getattr(exc, "code", None) != 241:
                    raise
                # Memory CREATE AS SELECT retains the whole output. A large
                # output can exceed the query limit even though streaming it
                # into MergeTree succeeds. Discard partial output before retry.
                client.command(f"DROP TEMPORARY TABLE IF EXISTS {table}")
                client.command(f"CREATE TEMPORARY TABLE {table} ENGINE={engine} AS\n{stage_query}",
                               parameters=stage_parameters)
                use_memory = False
            if use_memory:
                size = client.query("""SELECT total_bytes FROM system.tables
                    WHERE is_temporary AND name = {stage_name:String}""",
                    parameters={"stage_name": table}).first_row[0]
                if size is not None and sum(memory_sizes.values()) + size <= memory_budget:
                    memory_sizes[name] = size
                else:
                    disk_table = table + "_disk"
                    spill_tables.add(disk_table)
                    client.command(f"CREATE TEMPORARY TABLE {disk_table} ENGINE={engine} AS SELECT * FROM {table}")
                    client.command(f"DROP TEMPORARY TABLE {table}")
                    created[name] = table = disk_table
                    spill_tables.remove(disk_table)
            definitions[name] = f"SELECT * FROM {table}"
            remaining = reachable(compiled.result_query)
            for obsolete in list(created):
                if obsolete not in remaining:
                    client.command(f"DROP TEMPORARY TABLE IF EXISTS {created[obsolete]}")
                    del created[obsolete]
                    memory_sizes.pop(obsolete, None)
        yield replace(compiled, query=render(compiled.result_query))
    finally:
        for table in reversed([*created.values(), *spill_tables]):
            try:
                client.command(f"DROP TEMPORARY TABLE IF EXISTS {table}")
            except Exception:
                # A lost server connection must not mask the original failure.
                # These tables are session-local and expire with the session.
                pass
