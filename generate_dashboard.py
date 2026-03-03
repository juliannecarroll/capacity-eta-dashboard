import json, re, os
from datetime import datetime

DASHBOARD = os.path.expanduser("~/capacity-eta-dashboard/capacity-eta-dashboard.html")
ADO_RESULTS = os.path.expanduser("~/capacity-eta-dashboard/ado_results.json")

# Kusto data columns (in order)
COLS = ["Id","Type","Status","SubmittedByName","ScenarioName","FeatureName","FeaturePhase","ProductGroup","ModelName","Environment","Mode","TotalGPU","TotalRequestedGpuCount","ChampRank","CouncilDecision","AdoLink","SubmittedTime","LastModifiedTime"]

# Load Kusto results from JSON file
with open(os.path.expanduser("~/capacity-eta-dashboard/kusto_data.json"), "r", encoding="utf-8") as f:
    kusto_rows = json.load(f)

# Parse existing HTML to extract old ADO statuses
with open(DASHBOARD, "r", encoding="utf-8") as f:
    html = f.read()

# Extract existing capacityData entries for adoStatus/targetDate lookup (fallback)
ado_lookup = {}
pattern = r'adoLink:\s*"(\d+)".*?targetDate:\s*"([^"]*)".*?adoStatus:\s*"([^"]*)"'
for m in re.finditer(pattern, html):
    ado_id, target_date, ado_status = m.group(1), m.group(2), m.group(3)
    ado_lookup[ado_id] = {"targetDate": target_date, "adoStatus": ado_status}

print(f"Found {len(ado_lookup)} existing ADO entries in dashboard HTML")

# Override with fresh ADO results if available
ado_fresh = {}
if os.path.exists(ADO_RESULTS):
    with open(ADO_RESULTS, "r", encoding="utf-8") as f:
        ado_fresh = json.load(f)
    print(f"Loaded {len(ado_fresh)} fresh ADO statuses from ado_results.json")
    for ado_id, info in ado_fresh.items():
        ado_lookup[ado_id] = {
            "targetDate": info.get("targetDate", ""),
            "adoStatus": info.get("state", "New")
        }
else:
    print("No ado_results.json found - using existing ADO statuses from HTML")

# Transform Kusto rows to dashboard format
new_entries = []
for row in kusto_rows:
    r = dict(zip(COLS, row))
    ado_id = str(r["AdoLink"]) if r["AdoLink"] else ""
    old = ado_lookup.get(ado_id, {})
    
    entry = {
        "id": r["Id"],
        "type": r["Type"],
        "status": r["Status"],
        "submittedBy": r["SubmittedByName"],
        "scenarioName": r["ScenarioName"],
        "featureName": r["FeatureName"],
        "featurePhase": r["FeaturePhase"],
        "productGroup": r["ProductGroup"],
        "model": r["ModelName"],
        "env": r["Environment"],
        "mode": r["Mode"],
        "gpuCount": r["TotalRequestedGpuCount"] or r["TotalGPU"] or 0,
        "rank": r["ChampRank"] or 0,
        "councilDecision": r["CouncilDecision"],
        "adoLink": ado_id,
        "submittedTime": r["SubmittedTime"].split("T")[0] if r["SubmittedTime"] else "",
        "targetDate": old.get("targetDate", ""),
        "adoStatus": old.get("adoStatus", "New"),
    }
    new_entries.append(entry)

print(f"Generated {len(new_entries)} entries from Kusto data")

# Generate JS array
def js_val(v):
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v))

lines = []
for e in new_entries:
    parts = []
    for k in ["id","type","status","submittedBy","scenarioName","featureName","featurePhase","productGroup","model","env","mode","gpuCount","rank","councilDecision","adoLink","submittedTime","targetDate","adoStatus"]:
        v = e[k]
        if k in ("gpuCount", "rank"):
            parts.append(f"{k}: {v}")
        else:
            parts.append(f'{k}: "{v}"')
    lines.append("            { " + ", ".join(parts) + " },")

new_data_js = "\n".join(lines)

# Replace capacityData in HTML
# Find the start and end of the capacityData array
start_marker = "        const capacityData = [\n"
end_marker = "\n        ];"

start_idx = html.index(start_marker) + len(start_marker)
# Find the matching end bracket
end_idx = html.index(end_marker, start_idx)

new_html = html[:start_idx] + new_data_js + html[end_idx:]

with open(DASHBOARD, "w", encoding="utf-8") as f:
    f.write(new_html)

# Update footer date
today_str = datetime.now().strftime("%B %d, %Y").replace(" 0", " ")  # e.g. "March 3, 2026"
footer_pattern = r'(LLM API Capacity Request ETA Dashboard \| Last Updated: )[^<]+'
new_html_final = re.sub(footer_pattern, rf'\g<1>{today_str}', new_html)
if new_html_final != new_html:
    with open(DASHBOARD, "w", encoding="utf-8") as f:
        f.write(new_html_final)
    print(f"Footer updated: Last Updated: {today_str}")

print(f"\nDashboard updated with {len(new_entries)} records!")
print(f"Status breakdown:")
from collections import Counter
statuses = Counter(e["status"] for e in new_entries)
for s, c in statuses.most_common():
    print(f"  {s}: {c}")
ado_statuses = Counter(e["adoStatus"] for e in new_entries)
print(f"ADO Status breakdown:")
for s, c in ado_statuses.most_common():
    print(f"  {s}: {c}")
