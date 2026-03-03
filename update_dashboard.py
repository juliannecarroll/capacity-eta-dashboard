#!/usr/bin/env python3
"""
Merge fresh Kusto data with ADO work item statuses and update the dashboard HTML.
Uses existing ADO data from HTML as fallback, attempts fresh ADO queries with org-level API.
"""

import json
import re
import sys
import os
from datetime import datetime

# Read existing HTML to extract current ADO data
html_path = os.path.join(os.path.dirname(__file__), 'capacity-eta-dashboard.html')
with open(html_path, 'r', encoding='utf-8') as f:
    html = f.read()

# Parse existing ADO data from current capacityData in HTML
existing_ado = {}
pattern = r'adoLink:\s*"(\d+)".*?targetDate:\s*"([^"]*?)".*?adoStatus:\s*"([^"]*?)"'
for m in re.finditer(pattern, html):
    existing_ado[m.group(1)] = {'targetDate': m.group(2), 'state': m.group(3)}

print(f"Extracted existing ADO data for {len(existing_ado)} items from HTML", file=sys.stderr)

# Try to get fresh ADO data
def try_ado_refresh(all_ids):
    """Attempt to get fresh ADO data using MSAL."""
    try:
        import msal
        import urllib.request
        import urllib.error

        app = msal.PublicClientApplication(
            '04b07795-8ddb-461a-bbee-02f9e1bf7b46',
            authority='https://login.microsoftonline.com/72f988bf-86f1-41af-91ab-2d7cd011db47'
        )

        # Try cached token first
        accounts = app.get_accounts()
        result = None
        for acct in accounts:
            result = app.acquire_token_silent(
                ['499b84ac-1321-427f-aa17-267ca6975798/.default'],
                account=acct
            )
            if result and 'access_token' in result:
                break

        if not result or 'access_token' not in result:
            # Device code flow
            flow = app.initiate_device_flow(
                scopes=['499b84ac-1321-427f-aa17-267ca6975798/.default']
            )
            if 'user_code' in flow:
                print(f"\n>>> {flow['message']}\n", file=sys.stderr)
            result = app.acquire_token_by_device_flow(flow)

        if 'access_token' not in result:
            print(f"Auth failed: {result.get('error_description', 'unknown')}", file=sys.stderr)
            return {}

        token = result['access_token']
        print("Got ADO access token", file=sys.stderr)

        ado_data = {}
        # Use org-level endpoint with batches of 50
        for i in range(0, len(all_ids), 50):
            batch = all_ids[i:i+50]
            ids_str = ','.join(batch)
            url = (
                f'https://o365exchange.visualstudio.com/_apis/wit/workitems'
                f'?ids={ids_str}'
                f'&fields=System.Id,System.State,Microsoft.VSTS.Scheduling.TargetDate'
                f'&api-version=7.0'
            )
            req = urllib.request.Request(url)
            req.add_header('Authorization', f'Bearer {token}')
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                    for item in data.get('value', []):
                        item_id = str(item['id'])
                        fields = item.get('fields', {})
                        td = fields.get('Microsoft.VSTS.Scheduling.TargetDate', '')
                        if td and 'T' in td:
                            td = td.split('T')[0]
                        ado_data[item_id] = {
                            'state': fields.get('System.State', ''),
                            'targetDate': td
                        }
                    print(f"  Batch {i//50+1}: OK ({len(batch)} items)", file=sys.stderr)
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:200]
                print(f"  Batch {i//50+1}: HTTP {e.code} - trying individual...", file=sys.stderr)
                # Try individual items
                for item_id in batch:
                    url2 = (
                        f'https://o365exchange.visualstudio.com/_apis/wit/workitems/{item_id}'
                        f'?fields=System.Id,System.State,Microsoft.VSTS.Scheduling.TargetDate'
                        f'&api-version=7.0'
                    )
                    req2 = urllib.request.Request(url2)
                    req2.add_header('Authorization', f'Bearer {token}')
                    try:
                        with urllib.request.urlopen(req2, timeout=10) as resp2:
                            item_data = json.loads(resp2.read().decode())
                            fields = item_data.get('fields', {})
                            td = fields.get('Microsoft.VSTS.Scheduling.TargetDate', '')
                            if td and 'T' in td:
                                td = td.split('T')[0]
                            ado_data[item_id] = {
                                'state': fields.get('System.State', ''),
                                'targetDate': td
                            }
                    except:
                        pass
            except Exception as e:
                print(f"  Batch {i//50+1}: Error - {e}", file=sys.stderr)

        print(f"Fetched fresh ADO data for {len(ado_data)} items", file=sys.stderr)
        return ado_data
    except Exception as e:
        print(f"ADO refresh failed: {e}", file=sys.stderr)
        return {}

# Load the Kusto data from refresh_result.json
refresh_path = os.path.join(os.path.dirname(__file__), 'refresh_result.json')
with open(refresh_path, 'r') as f:
    refresh = json.load(f)

# Kusto data rows (embedded in refresh_data.py which generated refresh_result.json)
# But refresh_result.json has the pre-formatted JS. Let's use the raw kusto data instead.
# We'll import it from refresh_data.py
sys.path.insert(0, os.path.dirname(__file__))
from refresh_data import kusto_rows

# Collect all ADO IDs
all_ado_ids = list(set(str(row[15]) for row in kusto_rows if row[15]))
print(f"\n{len(kusto_rows)} Kusto rows, {len(all_ado_ids)} unique ADO IDs", file=sys.stderr)

# Try to get fresh ADO data
print("Attempting to fetch fresh ADO data...", file=sys.stderr)
fresh_ado = try_ado_refresh(all_ado_ids)

# Merge: prefer fresh ADO data, fall back to existing
merged_ado = dict(existing_ado)
merged_ado.update(fresh_ado)
print(f"Merged ADO data: {len(merged_ado)} items total", file=sys.stderr)

# Helper to format dates
def format_date(iso_str):
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except:
        return ""

def js_escape(s):
    if s is None:
        return ""
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

# Generate the capacityData entries
items = []
for row in kusto_rows:
    ado_id = str(row[15])
    ado_info = merged_ado.get(ado_id, {"state": "", "targetDate": ""})
    
    target_date = ado_info.get("targetDate", "")
    ado_status = ado_info.get("state", "")
    submitted_time = format_date(row[16])
    gpu_count = row[12] if row[12] else row[11]
    
    item = (
        f'            {{ id: "{js_escape(row[0])}", type: "{js_escape(row[1])}", '
        f'status: "{js_escape(row[2])}", submittedBy: "{js_escape(row[3])}", '
        f'scenarioName: "{js_escape(row[4])}", featureName: "{js_escape(row[5])}", '
        f'featurePhase: "{js_escape(row[6])}", productGroup: "{js_escape(row[7])}", '
        f'model: "{js_escape(row[8])}", env: "{js_escape(row[9])}", '
        f'mode: "{js_escape(row[10])}", gpuCount: {gpu_count}, '
        f'rank: {row[13]}, councilDecision: "{js_escape(row[14])}", '
        f'adoLink: "{js_escape(ado_id)}", submittedTime: "{submitted_time}", '
        f'targetDate: "{target_date}", adoStatus: "{js_escape(ado_status)}" }}'
    )
    items.append(item)

# Build the new capacityData block
today = datetime.now().strftime("%B %d, %Y")
new_data_block = "        const capacityData = [\n" + ",\n".join(items) + "\n        ];"

# Replace in HTML
# Find the capacityData block: starts with "const capacityData = [" and ends with "];"
data_pattern = r'const capacityData = \[.*?\];'
new_html = re.sub(data_pattern, new_data_block.lstrip(), html, count=1, flags=re.DOTALL)

# Update the lastRefresh span
old_refresh_pattern = r'(<span id="lastRefresh">).*?(</span>)'
new_html = re.sub(old_refresh_pattern, rf'\g<1>{today} ({len(items)} items)\g<2>', new_html)

# Write updated HTML
with open(html_path, 'w', encoding='utf-8') as f:
    f.write(new_html)

print(f"\nUpdated dashboard with {len(items)} items, dated {today}", file=sys.stderr)
print(f"ADO data sources: {len(fresh_ado)} fresh + {len(existing_ado)} existing fallback", file=sys.stderr)
print("Done!", file=sys.stderr)
