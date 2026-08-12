#!/usr/bin/env bash
set -euo pipefail

HUB="http://127.0.0.1:8000"
PANCAKE="http://127.0.0.1:8100"

echo "=== Trace-Forward E2E Tests ==="

# Generate unique emails to avoid conflicts
RAND=$RANDOM
FARMER_EMAIL="farmer_${RAND}@demo.agstack.org"
FARMER_PASS="Demo#Farmer1"
REGULATOR_EMAIL="regulator_${RAND}@demo.agstack.org"
REGULATOR_PASS="Demo#Regulator1"

# 1. Register Farmer
curl -s -X POST $HUB/users/register \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$FARMER_EMAIL\", \"password\": \"$FARMER_PASS\", \"first_name\": \"Flora\", \"last_name\": \"Farmer\", \"phone\": \"+504100${RAND}\", \"role\": \"farmer\", \"country\": \"USA\"}" > /dev/null

FARMER_TOKEN=$(curl -s -X POST $HUB/users/login \
  -d "username=$FARMER_EMAIL&password=$FARMER_PASS" | jq -r .access_token)

# 2. Register Regulator (has trace-forward capability)
curl -s -X POST $HUB/users/register \
  -H "Content-Type: application/json" \
  -d "{\"email\": \"$REGULATOR_EMAIL\", \"password\": \"$REGULATOR_PASS\", \"first_name\": \"Rachel\", \"last_name\": \"Regulator\", \"phone\": \"+504200${RAND}\", \"role\": \"authority\", \"country\": \"USA\"}" > /dev/null

REGULATOR_TOKEN=$(curl -s -X POST $HUB/users/login \
  -d "username=$REGULATOR_EMAIL&password=$REGULATOR_PASS" | jq -r .access_token)

# 3. Register a field as Farmer to get a seed GeoID
WKT="POLYGON((-119.483871 36.408104,-119.483828 36.404492,-119.475073 36.404527,-119.475181 36.408052,-119.483871 36.408104))"
GEOID=$(curl -s -X POST $HUB/register-field-boundary \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"wkt\": \"$WKT\", \"threshold\": 95}" | jq -r '."Geo Id" // ."matched geo ids"[0]')

echo "Registered GeoID: $GEOID"

# 4. Register a FieldList in Pancake
LIST_ID=$(curl -s -X POST $PANCAKE/fieldlists \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"Trace-Forward Farm\", \"geoids\": [\"$GEOID\"]}" | jq -r .list_id)

echo "Registered ListID: $LIST_ID"

# 5. Read Authority credentials from testkit
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
AUTH_TOKEN=$(cat "$SCRIPT_DIR/../app/tests/testkit/dev_keys/valid_authority.sdjwt")
REVOKED_AUTH_TOKEN=$(cat "$SCRIPT_DIR/../app/tests/testkit/dev_keys/revoked_authority.sdjwt")

# 5.1 Negative Assertion (No Auth Token) - Should 403
echo "--- Running Trace-Forward (No Grant, No Authority) ---"
# Using REGULATOR_TOKEN here so they pass Gate A (capabilities) but hit Gate B (token missing)
T1_RESP=$(curl -s -w "\n%{http_code}" -X POST $HUB/traceforward \
  -H "Authorization: Bearer $REGULATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"seed_geoid\": \"$GEOID\", \"scope\": \"demo-recall\"}")

T1_BODY=$(echo "$T1_RESP" | head -n -1)
T1_CODE=$(echo "$T1_RESP" | tail -n 1)

if [ "$T1_CODE" != "403" ]; then
    echo "No Auth Failed: Expected 403, got $T1_CODE"
    echo "$T1_BODY"
    exit 1
fi
echo "Trace-Forward blocked correctly without credentials."

# 5.1.b Tier 1 Trace-Forward (With Owner Grant) - Should succeed, NO holder_account
echo "--- Running Tier 1 Trace-Forward (With Owner Grant) ---"
OWNER_ISSUE_RES=$(curl -s -X POST $PANCAKE/grants/issue \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"list_id\": \"$LIST_ID\", \"grantee_account\": \"$FARMER_EMAIL\", \"purpose\": \"owner\", \"validity_days\": 365}")

OWNER_GRANT_ID=$(echo "$OWNER_ISSUE_RES" | jq -r .jti)
OWNER_RCV_RES=$(curl -s -X GET $PANCAKE/grants/received -H "Authorization: Bearer $FARMER_TOKEN")
OWNER_CREDENTIAL=$(echo "$OWNER_RCV_RES" | python3 -c "import sys,json; print(next((g['credential'] for g in json.load(sys.stdin) if g['jti'] == '$OWNER_GRANT_ID'), ''))")

if [ -z "$OWNER_CREDENTIAL" ]; then
    echo "Failed to fetch owner credential"
    exit 1
fi

T1_GRANT_RESP=$(curl -s -w "\n%{http_code}" -X POST $HUB/traceforward \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "X-Grant-Token: $OWNER_CREDENTIAL" \
  -H "Content-Type: application/json" \
  -d "{\"seed_geoid\": \"$GEOID\", \"scope\": \"demo-recall\"}")

T1_GRANT_BODY=$(echo "$T1_GRANT_RESP" | head -n -1)
T1_GRANT_CODE=$(echo "$T1_GRANT_RESP" | tail -n 1)

if [ "$T1_GRANT_CODE" != "200" ]; then
    echo "Tier 1 (Owner Grant) Failed with status $T1_GRANT_CODE"
    echo "$T1_GRANT_BODY"
    exit 1
fi

if echo "$T1_GRANT_BODY" | grep -q "holder_account"; then
    echo "Tier 1 (Owner Grant) Failed: holder_account leaked"
    exit 1
fi
echo "Tier 1 Trace-Forward successful (holder_account absent)."


# 5.2 Negative Assertion (Revoked Credential) - Should 403
echo "--- Running Trace-Forward (Revoked Grant) ---"
# Issue a padding grant so that TEMP_ISSUE gets index > 1 (preventing collision with valid_authority.sdjwt which uses idx 1)
curl -s -X POST $PANCAKE/grants/issue \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"list_id\": \"$LIST_ID\", \"grantee_account\": \"$FARMER_EMAIL\", \"purpose\": \"temporary\", \"validity_days\": 1}" > /dev/null

# Issue the temporary grant we will actually revoke
TEMP_ISSUE=$(curl -s -X POST $PANCAKE/grants/issue \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"list_id\": \"$LIST_ID\", \"grantee_account\": \"$FARMER_EMAIL\", \"purpose\": \"temporary\", \"validity_days\": 1}")

TEMP_GRANT_ID=$(echo "$TEMP_ISSUE" | jq -r .jti)

# Revoke it
REVOKE_RES=$(curl -s -w "\n%{http_code}" -X POST "$PANCAKE/grants/revoke" \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"jti\": \"$TEMP_GRANT_ID\"}")

REVOKE_CODE=$(echo "$REVOKE_RES" | tail -n 1)
if [ "$REVOKE_CODE" != "200" ]; then
    echo "Failed to revoke temporary grant! Got $REVOKE_CODE"
    echo "$REVOKE_RES"
    exit 1
fi

# Wait for status list to be updated
sleep 2

# Fetch the revoked credential
TEMP_RCV=$(curl -s -X GET $PANCAKE/grants/received -H "Authorization: Bearer $FARMER_TOKEN")
REVOKED_GRANT=$(echo "$TEMP_RCV" | python3 -c "import sys,json; print(next((g['credential'] for g in json.load(sys.stdin) if g['jti'] == '$TEMP_GRANT_ID'), ''))")

T1_REVOKED_RESP=$(curl -s -w "\n%{http_code}" -X POST $HUB/traceforward \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "X-Grant-Token: $REVOKED_GRANT" \
  -H "Content-Type: application/json" \
  -d "{\"seed_geoid\": \"$GEOID\", \"scope\": \"demo-recall\"}")

T1_REVOKED_BODY=$(echo "$T1_REVOKED_RESP" | head -n -1)
T1_REVOKED_CODE=$(echo "$T1_REVOKED_RESP" | tail -n 1)

if [ "$T1_REVOKED_CODE" != "403" ]; then
    echo "Revoked Credential Failed: Expected 403, got $T1_REVOKED_CODE"
    echo "$T1_REVOKED_BODY"
    exit 1
fi
echo "Revoked Credential blocked correctly."

# 6. Run Tier 3 Trace-Forward (With Auth Token) - Should succeed WITH holder_account
echo "--- Running Tier 3 Trace-Forward (With Auth) ---"
T3_RESP=$(curl -s -w "\n%{http_code}" -X POST $HUB/traceforward \
  -H "Authorization: Bearer $REGULATOR_TOKEN" \
  -H "X-Authority-Token: $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"seed_geoid\": \"$GEOID\", \"scope\": \"demo-recall\"}")

T3_BODY=$(echo "$T3_RESP" | head -n -1)
T3_CODE=$(echo "$T3_RESP" | tail -n 1)

if [ "$T3_CODE" != "200" ]; then
    echo "Tier 3 Failed with status $T3_CODE"
    echo "$T3_BODY"
    exit 1
fi

if ! echo "$T3_BODY" | grep -q "holder_account"; then
    echo "Tier 3 Failed: holder_account missing"
    exit 1
fi
echo "Tier 3 Trace-Forward successful (holder_account present)."

# 7. Retrieving MEAL Audit Chain
echo "--- Retrieving MEAL Audit Chain ---"
AUDIT_RESP=$(curl -s -w "\n%{http_code}" -X GET $PANCAKE/audit/$GEOID/report \
  -H "Authorization: Bearer $FARMER_TOKEN")

AUDIT_BODY=$(echo "$AUDIT_RESP" | head -n -1)
AUDIT_CODE=$(echo "$AUDIT_RESP" | tail -n 1)

if [ "$AUDIT_CODE" != "200" ]; then
    echo "Audit Failed with status $AUDIT_CODE"
    echo "$AUDIT_BODY"
    exit 1
fi

# Assert traceforward.disclosure is present in events
if ! echo "$AUDIT_BODY" | grep -q "traceforward.disclosure"; then
    echo "Audit Failed: traceforward.disclosure packet missing"
    exit 1
fi
echo "Audit chain retrieved and verified."

echo "E2E PASS"
