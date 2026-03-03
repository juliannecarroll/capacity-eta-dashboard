import json, sys, os
from azure.identity import AzureCliCredential, DeviceCodeCredential, InteractiveBrowserCredential

# Try to get an ADO token
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
ADO_ORG = "https://o365exchange.visualstudio.com"
ADO_PROJECT = "O365 Core"

# ADO work item IDs extracted from Kusto data
ado_ids_str = sys.argv[1] if len(sys.argv) > 1 else ""
ado_ids = [int(x) for x in ado_ids_str.split(",") if x.strip()]

if not ado_ids:
    print("No ADO IDs provided")
    sys.exit(1)

print(f"Fetching {len(ado_ids)} ADO work items...")

# Get token using DeviceCodeCredential for interactive auth
try:
    # Try cached credentials first
    from azure.identity import SharedTokenCacheCredential
    cred = SharedTokenCacheCredential(tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47")
    token = cred.get_token(f"{ADO_RESOURCE}/.default")
    print("Got token from shared cache")
except Exception as e:
    print(f"SharedTokenCache failed: {e}")
    try:
        cred = DeviceCodeCredential(tenant_id="72f988bf-86f1-41af-91ab-2d7cd011db47")
        token = cred.get_token(f"{ADO_RESOURCE}/.default")
        print("Got token from device code")
    except Exception as e2:
        print(f"DeviceCode failed: {e2}")
        sys.exit(1)

# Batch fetch work items
import urllib.request

results = {}
batch_size = 200
for i in range(0, len(ado_ids), batch_size):
    batch = ado_ids[i:i+batch_size]
    ids_param = ",".join(str(x) for x in batch)
    url = f"{ADO_ORG}/{ADO_PROJECT.replace(' ', '%20')}/_apis/wit/workitems?ids={ids_param}&fields=System.Id,System.State,Microsoft.VSTS.Scheduling.TargetDate&api-version=7.0"
    
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json"
    })
    
    try:
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        for wi in data.get("value", []):
            wi_id = str(wi["id"])
            state = wi["fields"].get("System.State", "Unknown")
            target_date = wi["fields"].get("Microsoft.VSTS.Scheduling.TargetDate", "")
            if target_date:
                target_date = target_date.split("T")[0]
            results[wi_id] = {"state": state, "targetDate": target_date}
        print(f"  Fetched batch {i//batch_size + 1}: {len(data.get('value', []))} items")
    except Exception as e:
        print(f"  Batch {i//batch_size + 1} error: {e}")

# Write results
with open(os.path.join(os.path.dirname(__file__), "ado_results.json"), "w") as f:
    json.dump(results, f, indent=2)

print(f"Done. Got {len(results)} ADO work items.")
