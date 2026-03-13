#!/usr/bin/env python3
"""
Generate Echo RPM Dashboard data.

Queries Kusto for 7-day peak RPM actuals (ES + CWC Paid) and Echo/Echo Std
approved RPMs, computes 3% targets, margins, and proposed tiers, then injects
the data into echo-rpm-dashboard.html.

Usage:
    python generate_echo_dashboard.py
"""

from azure.identity import InteractiveBrowserCredential, AuthenticationRecord, TokenCachePersistenceOptions
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder, ClientRequestProperties
from pathlib import Path
from datetime import timedelta, datetime, timezone
import json
import math
import re

CLUSTER = "https://modeldqasprodeus2.eastus.kusto.windows.net"
DATABASE = "modeldqaseus2db"
AUTH_RECORD_PATH = Path(r"C:\Users\jcarroll\.claude\skills\echo-3pct\.auth_record.json")
DASHBOARD_PATH = Path(__file__).parent / "echo-rpm-dashboard.html"

ES  = "e48e91ad-9692-479d-8483-d58ec1f6642d"
CP  = "6acc8f69-1e5c-4705-9794-f6e7554a7eeb"
EC  = "287c96b7-cb6d-4998-b8b0-e0cc66c884db"
EST = "d9b24723-326f-4c51-8997-c760a75dbbb3"


def get_client():
    cache_options = TokenCachePersistenceOptions(name="echo_3pct")
    auth_record = AuthenticationRecord.deserialize(
        AUTH_RECORD_PATH.read_text(encoding="utf-8")
    )
    cred = InteractiveBrowserCredential(
        cache_persistence_options=cache_options, authentication_record=auth_record
    )
    cred.get_token("https://kusto.kusto.windows.net/.default")
    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(CLUSTER, cred)
    return KustoClient(kcsb)


def make_props():
    props = ClientRequestProperties()
    props.set_option(
        ClientRequestProperties.results_defer_partial_query_failures_option_name, True
    )
    props.set_option("servertimeout", timedelta(minutes=5))
    return props


def _round_up_rpm(target):
    """Round target RPM up to a clean tier."""
    if target <= 0:
        return 100
    tiers = [100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000,
             200000, 500000, 1000000]
    for t in tiers:
        if t >= target:
            return t
    return math.ceil(target / 500000) * 500000


def main():
    client = get_client()
    props = make_props()

    # Actuals for baseline scenarios (ES + CWC Paid)
    print("[query] Fetching actuals (7d)...")
    q_actuals = f"""
    LLMAPIRequestTracingEvent_Global
    | where TIMESTAMP > ago(7d)
    | where ScenarioGuid in ("{ES}", "{CP}")
    | where HttpResponseStatusCode == '200' and RequestSucceeded == 1
    | extend ModelName = case(
        ResolvedModelName startswith "prod-", substring(ResolvedModelName, 5),
        ResolvedModelName startswith "dev-", substring(ResolvedModelName, 4),
        ResolvedModelName)
    | where isnotempty(ModelName)
    | summarize RPM = count() by ScenarioGuid, ModelName, bin(TIMESTAMP, 1m)
    | summarize PeakRPM = max(RPM) by ScenarioGuid, ModelName
    """
    r1 = client.execute(DATABASE, q_actuals, properties=props)
    actuals = {}
    for row in r1.primary_results[0]:
        actuals[(str(row["ModelName"]), str(row["ScenarioGuid"]))] = int(row["PeakRPM"])
    print(f"  {len(actuals)} actuals rows")

    # Approved from polymer (Echo + Echo Std only)
    print("[query] Fetching approved RPMs from polymer...")
    q_approved = f"""
    cluster("https://polymer.centralus.kusto.windows.net/").database("polymerdb").llmscenarios
    | where id in ("{EC}", "{EST}")
    | extend ProdCap = parse_json(ProdCapacity)
    | mv-expand ProdModel = ProdCap.Models
    | extend ModelName = tostring(ProdModel.ModelName)
    | where isnotempty(ModelName)
    | extend ApprovedRPM = tolong(extract(@"T(\\d+)RPM", 1, tostring(ProdModel.ThrottlingCategory)))
    | where ApprovedRPM > 0
    | project ScenarioGuid = id, ModelName, ApprovedRPM
    """
    r2 = client.execute(DATABASE, q_approved, properties=props)
    approved = {}
    for row in r2.primary_results[0]:
        approved[(str(row["ModelName"]), str(row["ScenarioGuid"]))] = int(row["ApprovedRPM"])
    print(f"  {len(approved)} approved rows")

    # Build rows
    all_models = sorted({k[0] for k in actuals} | {k[0] for k in approved})
    rows_data = []
    for m in all_models:
        es_act   = actuals.get((m, ES), 0)
        cp_act   = actuals.get((m, CP), 0)
        ec_appr  = approved.get((m, EC), 0)
        est_appr = approved.get((m, EST), 0)
        if es_act + cp_act + ec_appr + est_appr == 0:
            continue
        base_act = es_act + cp_act
        pct3 = math.ceil(0.03 * base_act) if base_act > 0 else 0
        echo_margin = ec_appr - pct3 if (ec_appr > 0 or pct3 > 0) else None
        estd_margin = est_appr - pct3 if (est_appr > 0 or pct3 > 0) else None
        echo_action = _round_up_rpm(pct3) if (echo_margin is not None and echo_margin < 0) else None
        estd_action = _round_up_rpm(pct3) if (estd_margin is not None and estd_margin < 0) else None

        rows_data.append({
            "model": m,
            "es_act": es_act,
            "cp_act": cp_act,
            "base_act": base_act,
            "pct3": pct3,
            "ec_appr": ec_appr,
            "est_appr": est_appr,
            "echo_margin": echo_margin,
            "estd_margin": estd_margin,
            "echo_action": echo_action,
            "estd_action": estd_action,
        })
    rows_data.sort(key=lambda r: r["base_act"], reverse=True)
    print(f"  {len(rows_data)} models total")

    action_count = sum(1 for r in rows_data if r["echo_action"] is not None or r["estd_action"] is not None)
    print(f"  {action_count} models need action")

    # Generate JS data
    def js_val(v):
        if v is None:
            return "null"
        if isinstance(v, int):
            return str(v)
        return json.dumps(v)

    js_lines = []
    keys = ["model", "es_act", "cp_act", "base_act", "pct3",
            "ec_appr", "est_appr", "echo_margin", "estd_margin",
            "echo_action", "estd_action"]
    for r in rows_data:
        parts = [f"{k}: {js_val(r[k])}" for k in keys]
        js_lines.append("        {" + ", ".join(parts) + "},")
    js_data = "\n".join(js_lines)

    # Read HTML template and inject data
    html = DASHBOARD_PATH.read_text(encoding="utf-8")

    # Replace data placeholder
    html = re.sub(
        r'(const echoData = \[\n).*?(\n    \];)',
        rf'\g<1>{js_data}\g<2>',
        html,
        flags=re.DOTALL,
    )

    # Update footer date
    today = datetime.now(tz=timezone.utc).strftime("%B %d, %Y").replace(" 0", " ")
    html = html.replace("__LAST_UPDATED__", today)
    html = re.sub(
        r'(Echo Shadow RPM Gap Analysis \| Last Updated: )[^<]+',
        rf'\g<1>{today}',
        html,
    )

    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    print(f"[done] Dashboard updated: {DASHBOARD_PATH}")


if __name__ == "__main__":
    main()
