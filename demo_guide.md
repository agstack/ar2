# AgStack DPI Integration Demo Guide

This guide details how to manually run and test the complete AgStack DPI field-access grant lifecycle across the three node architectures: **AR Hub**, **AR Edge Node**, and **Pancake** (Issuance & Revocation).

## 1. Starting the Servers

You will need three separate terminal windows to run the servers on their respective ports.

### Terminal 1: AR Hub
The Hub acts as the central directory for accounts, JWKS public keys, and proxy routing.
```bash
cd /PATH_OF_ar2-hub/ar2-hub
source hub-env/bin/activate
uvicorn hub_main:app --host 0.0.0.0 --port 8000 --reload
```

### Terminal 2: AR Edge Node
The AR node stores the actual geometries and verifies grants to elevate access to `L1`.
```bash
cd /PATH_OF_ar2/ar2
source ar-env/bin/activate

# Provide Pancake's public key so AR2 can verify signatures
export AR_TRUSTED_ISSUER_PUBKEY="/PATH_WHERE_PANCAKE_CLONED/pancake/services/pancake_services/grants/testkit/dev_keys/dev_issuer_public.pem"

uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

### Terminal 3: Pancake
Pancake manages field lists, issues SD-JWT verifiable credentials, and tracks revocations.
```bash
cd /PATH_WHERE_PANCAKE_CLONED/pancake/services

# Load the issuer key and specify the Hub JWKS URL
export PANCAKE_ISSUER_KEY="$(cat pancake_services/grants/testkit/dev_keys/dev_issuer_private.pem)"
export HUB_JWKS_URL="http://127.0.0.1:8000/.well-known/jwks.json"

../.venv/bin/uvicorn "pancake_services.grants.app:create_app" --factory --port 8100
```

---

## 2. Testing the Lifecycle

You can use the provided `demo_e2e.sh` script to run this flow automatically, or run the following commands sequentially. We present two options for the flow: **Option A** (Direct to Edge Node) and **Option B** (Routed through the Hub).

### Step 1: Authenticate with AR Hub (Required for Both Options)
Get an RS256 JWT access token from the hub.
```bash
LOGIN_RES=$(curl -s -X POST "http://127.0.0.1:8000/users/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=test_user@gmail.com&password=test@12345")

export HUB_TOKEN=$(echo $LOGIN_RES | grep -oP '"access_token":"\K[^"]+')
```

---

### Option A: Direct AR Node API Calling
Use this if you want to test the Edge Node directly, completely bypassing the Hub's proxy features.

**1. Register a Field (Direct to 8001)**
```bash
REG_RES=$(curl -s -X POST "http://127.0.0.1:8001/register-field-boundary" \
  -H "accept: application/json" \
  -H "Content-Type: application/json" \
  -d '{"wkt": "POLYGON((-115.11287927627565 32.401601610730026,-115.11332988739014 32.39794898464963,-115.11252522468568 32.397414220539915,-115.11190295219423 32.397541114005094,-115.11085152626039 32.39765894349167,-115.10995030403139 32.39849280930963,-115.10892033576967 32.398909739330705,-115.1090168952942 32.401764751871625,-115.11287927627565 32.401601610730026))", "threshold": 95, "return_s2_indices": false}')
export GEOID=$(echo $REG_RES | grep -oP '"Geo Id":"\K[^"]+')
```

**2. Create a FieldList on Pancake**
```bash
LIST_RES=$(curl -s -X POST "http://127.0.0.1:8100/fieldlists" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"My Test Field\", \"geoids\": [\"$GEOID\"]}")
export LIST_ID=$(echo $LIST_RES | grep -oP '"list_id":"\K[^"]+')
```

**3. Issue the Grant on Pancake**
```bash
GRANT_RES=$(curl -s -X POST "http://127.0.0.1:8100/grants/issue" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"list_id\": \"$LIST_ID\", \"grantee_account\": \"test_user@gmail.com\", \"purpose\": \"eudr-due-diligence\", \"validity_days\": 1}")
export GRANT_JTI=$(echo $GRANT_RES | grep -oP '"jti":"\K[^"]+')
```

**4. Retrieve the Credential**
```bash
RCV_RES=$(curl -s -X GET "http://127.0.0.1:8100/grants/received" \
  -H "Authorization: Bearer $HUB_TOKEN")
export GRANT_TOKEN=$(echo $RCV_RES | grep -oP '"credential":"\K[^"]+' | head -n 1)
```

**5. Verify L1 Access (Direct to 8001)**
```bash
curl -X GET "http://127.0.0.1:8001/fetch-field-wkt/$GEOID" \
  -H "accept: application/json" \
  -H "X-Field-Grant: $GRANT_TOKEN"
```

**6. Revoke the Grant**
```bash
curl -s -X POST "http://127.0.0.1:8100/grants/revoke" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"jti\": \"$GRANT_JTI\"}"
```

**7. Verify L0 Fallback (Direct to 8001)**
```bash
curl -X GET "http://127.0.0.1:8001/fetch-field-wkt/$GEOID" \
  -H "accept: application/json" \
  -H "X-Field-Grant: $GRANT_TOKEN"
```

---

### Option B: Routing Through Hub Only
Use this flow if you want the Hub to act as the primary interface, automatically routing your requests to the correct regional Edge Node via port `8000`.

**1. Register a Field (Via Hub Port 8000 - No Auth Required)**
```bash
REG_RES=$(curl -s -X POST "http://127.0.0.1:8000/register-field-boundary" \
  -H "accept: application/json" \
  -H "Content-Type: application/json" \
  -d '{"wkt": "POLYGON((-119.70914483070375 36.621719124665596,-119.70914483070375 36.61996153472247,-119.7047245502472 36.61993568751255,-119.70475673675537 36.62173635574118,-119.70914483070375 36.621719124665596))", "threshold": 95, "return_s2_indices": false}')
export GEOID=$(echo $REG_RES | grep -oP '"Geo Id":"\K[^"]+')
```

**2. Create a FieldList on Pancake**
```bash
LIST_RES=$(curl -s -X POST "http://127.0.0.1:8100/fieldlists" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"My Hub-Routed Test Field\", \"geoids\": [\"$GEOID\"]}")
export LIST_ID=$(echo $LIST_RES | grep -oP '"list_id":"\K[^"]+')
```

**3. Issue the Grant on Pancake**
```bash
GRANT_RES=$(curl -s -X POST "http://127.0.0.1:8100/grants/issue" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"list_id\": \"$LIST_ID\", \"grantee_account\": \"test_user@gmail.com\", \"purpose\": \"eudr-due-diligence\", \"validity_days\": 1}")
export GRANT_JTI=$(echo $GRANT_RES | grep -oP '"jti":"\K[^"]+')
```

**4. Retrieve the Credential**
```bash
RCV_RES=$(curl -s -X GET "http://127.0.0.1:8100/grants/received" \
  -H "Authorization: Bearer $HUB_TOKEN")
export GRANT_TOKEN=$(echo $RCV_RES | grep -oP '"credential":"\K[^"]+' | head -n 1)
```

**5. Verify L1 Access (Via Hub Port 8000)**
```bash
curl -X GET "http://127.0.0.1:8000/fetch-field-wkt/$GEOID" \
  -H "accept: application/json" \
  -H "X-Field-Grant: $GRANT_TOKEN"
```
*(The Hub will seamlessly proxy this request and the `X-Field-Grant` header to the Edge Node. You should get `L1`.)*

**6. Revoke the Grant**
```bash
curl -s -X POST "http://127.0.0.1:8100/grants/revoke" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"jti\": \"$GRANT_JTI\"}"
```

**7. Verify L0 Fallback (Via Hub Port 8000)**
```bash
curl -X GET "http://127.0.0.1:8000/fetch-field-wkt/$GEOID" \
  -H "accept: application/json" \
  -H "X-Field-Grant: $GRANT_TOKEN"
```
*(The Hub proxies the request again. The Edge Node detects revocation, falls back to `L0`, and the Hub returns the safely masked result.)*
