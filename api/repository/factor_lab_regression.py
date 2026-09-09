"""Deterministic per-date least squares compiled to ClickHouse array operations.

Modified Gram-Schmidt QR, with an unpenalized intercept via centering. Ridge
appends sqrt(alpha) I to centered X and zeroes to centered y. No server UDFs.
All node IDs/configuration are validated by the public graph compiler first.
"""


def compile_residualize(node_id, config, inputs, params):
    base = f"node_{node_id}"
    names = config["exposures"]
    k = len(names)
    params[f"{base}_min"] = config.get("min_count", 30)
    params[f"{base}_alpha"] = float(config.get("alpha", 1)) if config["method"] == "ridge" else 0.0
    joins = " ".join(f"INNER JOIN node_{inputs[name]} AS x{j} ON x{j}.trade_date = y.trade_date AND x{j}.security_id = y.security_id" for j, name in enumerate(names))
    valid = " AND ".join(f"x{j}.is_valid AND isFinite(x{j}.value)" for j in range(k))
    xs = ", ".join(f"assumeNotNull(x{j}.value)" for j in range(k))
    ctes = [f"""{base}_groups AS (
        SELECT y.trade_date AS trade_date,
            arraySort(t -> t.1, groupArray(tuple(y.security_id, assumeNotNull(y.value), {xs}))) AS rows
        FROM node_{inputs['target']} AS y {joins}
        WHERE y.is_valid AND isFinite(y.value) AND {valid}
        GROUP BY y.trade_date
    )"""]
    centered = []
    for j in range(k):
        raw = f"arrayMap(t -> t.{j + 3}, rows)"
        extra = ", ".join(f"sqrt({{{base}_alpha:Float64}})" if q == j else "toFloat64(0)" for q in range(k))
        centered.append(f"arrayConcat(arrayMap(v -> v - arrayAvg({raw}), {raw}), [{extra}]) AS x{j}")
    ctes.append(f"""{base}_center AS (
        SELECT *, length(rows) AS n,
            arrayConcat(arrayMap(t -> t.2 - arrayAvg(arrayMap(r -> r.2, rows)), rows), arrayWithConstant({k}, toFloat64(0))) AS ys,
            {', '.join(centered)} FROM {base}_groups
    )""")
    previous = f"{base}_center"
    norms = []
    for j in range(k):
        # Sequential projections improve stability compared with normal equations.
        vec = f"x{j}"
        for q in range(j):
            stage = f"{base}_orth_{j}_{q}"
            ctes.append(f"{stage} AS (SELECT *, arrayMap((a, b) -> a - arrayDotProduct({vec}, q{q}) * b, {vec}, q{q}) AS u{j}_{q} FROM {previous})")
            previous, vec = stage, f"u{j}_{q}"
        stage = f"{base}_qr{j}"
        norm = f"sqrt(arrayDotProduct({vec}, {vec}))"
        ctes.append(f"{stage} AS (SELECT *, {norm} AS norm{j}, arrayMap(v -> v / if({norm} > 0, {norm}, 1), {vec}) AS q{j} FROM {previous})")
        previous = stage
        norms.append(f"norm{j} > 1e-10 * greatest(1.0, sqrt(arrayDotProduct(x{j}, x{j})))")
    fitted = " + ".join(f"arrayDotProduct(ys, q{j}) * q{j}[pos]" for j in range(k))
    ctes.append(f"""{base}_residuals AS (
        SELECT trade_date, rows[pos].1 AS security_id,
            ys[pos] - ({fitted}) AS residual,
            n >= {{{base}_min:UInt32}} AND {' AND '.join(norms)} AS fit_valid,
            if(n < {{{base}_min:UInt32}}, 'regression_min_count', 'regression_rank_deficient') AS fit_reason
        FROM {previous} ARRAY JOIN arrayEnumerate(rows) AS pos
    )""")
    ctes.append(f"""{base} AS (
        SELECT y.trade_date AS trade_date, y.security_id AS security_id,
            if(ifNull(r.fit_valid, false) AND isFinite(r.residual), r.residual, NULL) AS value,
            ifNull(r.fit_valid, false) AND isFinite(r.residual) AS is_valid,
            multiIf(r.security_id = '' OR r.security_id IS NULL, 'regression_missing_input',
                NOT ifNull(r.fit_valid, false), r.fit_reason, NOT isFinite(r.residual), 'non_finite_result', '') AS invalid_reason
        FROM node_{inputs['target']} AS y LEFT JOIN {base}_residuals AS r
            ON r.trade_date = y.trade_date AND r.security_id = y.security_id
    )""")
    return ctes
