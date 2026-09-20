"""Vendored unchanged from https://github.com/yushinliou/elisa (cohort_policy.py, 2026-09-19).
The de-identification policy the submitted file goes through. Do not edit here; upstream owns it.
"""
"""Cohort release policy shared by analyze.py and the attack harness.

One definition of the release, so the evaluator and the attacker always look
at the same cohorts. Needs a DuckDB view named `raw` over the source data.

Guarantees the resulting release realises (measured in attacks/):
  * k-anonymity, k >= selected threshold, on the cohort key
    (province, radio_access_type, application_category).
  * NO l-diversity for the numeric fields; the nonzero-support gate below is a
    partial mitigation for sparse fields, not a diversity guarantee.
  * NO differential privacy. One disjoint partition at one threshold does not
    give an attacker overlapping queries for reconstruction attacks.

Pooling hierarchy (each step recounts distinct people from raw rows, never
adds distinct counts across groups):
  1. application leaves that cannot support a metric -> 'Pooled categories'
  2. a province/network branch that still cannot -> 'All applications'
  3. a province/network branch that STILL cannot -> province 'Other provinces'
     (merged within the same network). Keeps ONE disjoint partition: nothing is
     published at a second granularity, which attacks Tier 3 asserts rather than
     assumes (a coarser extra table would need its own residual check).
A metric is released for a cohort only if >= k distinct subscribers have a
NONZERO value; a non-null count cannot see sparse fields.
"""

KEYS = ['province', 'radio_access_type', 'application_category']
OTHER_PROVINCES = 'Other provinces'
# (source field, label, decimals) for the metrics the release publishes.
SHIPPED_METRICS = [('tp_dl_avg', 'throughput', 6),
                   ('cont_rtt_internet_avg', 'latency', 1),
                   ('data_GB_sum', 'volume', 6)]


def aggregate_sql(metrics):
    aggs = []
    for f, label, _ in metrics:
        aggs += [f'count({f}) AS {label}_n',
                 f'count(distinct CASE WHEN {f} IS NOT NULL THEN msisdn END) AS {label}_people',
                 f'count(distinct CASE WHEN {f}>0 THEN msisdn END) AS {label}_nonzero_people',
                 f'avg({f}) AS {label}_mean', f'sum({f}) AS {label}_sum']
    return ','.join(aggs)


def build_leaf(c, metrics):
    """Original (unpooled) cohorts -> table `cohorts`."""
    fields = ','.join(KEYS)
    c.execute(f'CREATE OR REPLACE TABLE cohorts AS SELECT {fields},count(*) AS n,'
              f'count(distinct msisdn) AS people,{aggregate_sql(metrics)} FROM raw GROUP BY {fields}')


def bad_condition(k, metrics, pool_nonzero=True):
    if pool_nonzero:
        per_metric = [f'({l}_people>0 AND {l}_nonzero_people<{k})' for _, l, _ in metrics]
    else:
        per_metric = [f'({l}_people>0 AND {l}_people<{k})' for _, l, _ in metrics]
    return f'people<{k} OR ' + ' OR '.join(per_metric)


def build_policy(c, k, metrics, pooling='adaptive', pool_nonzero=True, pool_province=True):
    """Creates `policy` (final cohorts), `current_release` (gated, people>=k) and
    view `final_mapped` (every source row with its released province/application
    label, plus `source_province`). `pool_nonzero` / `pool_province` exist so the
    earlier behaviour can be reproduced for regression tests."""
    fields = ','.join(KEYS)
    agg = aggregate_sql(metrics)
    bad = bad_condition(k, metrics, pool_nonzero)
    if pooling == 'suppress':
        c.execute('CREATE OR REPLACE VIEW final_mapped AS SELECT *,province AS source_province FROM raw')
        c.execute('CREATE OR REPLACE TABLE policy AS SELECT * FROM cohorts')
    else:
        c.execute(f'''CREATE OR REPLACE TABLE leaf_map AS SELECT {fields},
            CASE WHEN {bad} THEN 'Pooled categories' ELSE application_category END AS pooled_app FROM cohorts''')
        c.execute('''CREATE OR REPLACE VIEW mapped AS SELECT r.* EXCLUDE(application_category),m.pooled_app AS application_category
            FROM raw r JOIN leaf_map m USING(province,radio_access_type,application_category)''')
        c.execute(f'''CREATE OR REPLACE TABLE pooled AS SELECT {fields},count(*) AS n,
            count(distinct msisdn) AS people,{agg} FROM mapped GROUP BY {fields}''')
        c.execute(f'''CREATE OR REPLACE TABLE fallback AS SELECT DISTINCT province,radio_access_type
            FROM pooled WHERE {bad}''')
        c.execute('''CREATE OR REPLACE VIEW app_mapped AS SELECT m.* EXCLUDE(application_category),
            CASE WHEN f.province IS NOT NULL THEN 'All applications' ELSE m.application_category END AS application_category
            FROM mapped m LEFT JOIN fallback f USING(province,radio_access_type)''')
        c.execute(f'''CREATE OR REPLACE TABLE policy_app AS SELECT {fields},count(*) AS n,
            count(distinct msisdn) AS people,{agg} FROM app_mapped GROUP BY {fields}''')
        if pool_province:
            c.execute(f'''CREATE OR REPLACE TABLE province_map AS SELECT province,radio_access_type,
                CASE WHEN bool_or({bad}) THEN '{OTHER_PROVINCES}' ELSE province END AS released_province
                FROM policy_app GROUP BY province,radio_access_type''')
        else:
            c.execute('''CREATE OR REPLACE TABLE province_map AS SELECT DISTINCT province,radio_access_type,
                province AS released_province FROM policy_app''')
        c.execute('''CREATE OR REPLACE VIEW final_mapped AS SELECT a.* EXCLUDE(province),
            p.released_province AS province,a.province AS source_province
            FROM app_mapped a JOIN province_map p ON a.province=p.province AND a.radio_access_type=p.radio_access_type''')
        c.execute(f'''CREATE OR REPLACE TABLE policy AS SELECT {fields},count(*) AS n,
            count(distinct msisdn) AS people,{agg} FROM final_mapped GROUP BY {fields}''')
    expr = []
    for _, l, d in metrics:
        expr += [f'CASE WHEN {l}_nonzero_people>={k} THEN round({l}_mean,{d}) END AS {l}_released',
                 f'CASE WHEN {l}_nonzero_people>={k} THEN {l}_n ELSE 0 END AS {l}_support']
    c.execute(f'CREATE OR REPLACE TABLE current_release AS SELECT *,{",".join(expr)} FROM policy WHERE people>={k}')
