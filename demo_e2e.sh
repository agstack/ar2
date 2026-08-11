#!/bin/bash
# demo_e2e.sh
# End-to-end automated demo of the AgStack DPI Field Access Grant lifecycle.
# Architecture: Owner is the first grantee & Hub proxies all traffic.

set -e

AR_HUB_URL="http://127.0.0.1:8000"
PANCAKE_URL="http://127.0.0.1:8100"

JSON() {
  # usage: echo '{"a": "b"}' | JSON "['a']"
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)"
}

expect() {
  # usage: expect "Label" "actual" "expected"
  if [ "$2" = "$3" ]; then
    echo "  PASS: $1 = $2"
  else
    echo "  FAIL: $1 = $2 (wanted $3)"
    exit 1
  fi
}

echo "=========================================================="
echo " AgStack DPI Demo: 2-Persona Flow (Through Hub Gateway)"
echo "=========================================================="

echo ""
echo "1. Registering & Authenticating Personas (Farmer & Buyer)"

# Attempt registration (ignore failures if they already exist)
curl -s -X POST "$AR_HUB_URL/users/register" -H "Content-Type: application/json" \
  -d '{"first_name": "Flora", "last_name": "Farmer", "email": "farmer@demo.agstack.org", "phone": "+50400000001", "password": "Demo#Farmer1", "role": "farmer", "country": "USA"}' > /dev/null || true
curl -s -X POST "$AR_HUB_URL/users/register" -H "Content-Type: application/json" \
  -d '{"first_name": "Bob", "last_name": "Buyer", "email": "buyer@demo.agstack.org", "phone": "+50400000002", "password": "Demo#Buyer1", "role": "buyer", "country": "USA"}' > /dev/null || true

FARMER_LOGIN=$(curl -s -X POST "$AR_HUB_URL/users/login" -H "Content-Type: application/x-www-form-urlencoded" -d "username=farmer@demo.agstack.org&password=Demo%23Farmer1")
FARMER_TOKEN=$(echo "$FARMER_LOGIN" | JSON "['access_token']")
if [ -z "$FARMER_TOKEN" ] || [ "$FARMER_TOKEN" = "None" ]; then echo "Farmer login failed."; exit 1; fi

BUYER_LOGIN=$(curl -s -X POST "$AR_HUB_URL/users/login" -H "Content-Type: application/x-www-form-urlencoded" -d "username=buyer@demo.agstack.org&password=Demo%23Buyer1")
BUYER_TOKEN=$(echo "$BUYER_LOGIN" | JSON "['access_token']")
if [ -z "$BUYER_TOKEN" ] || [ "$BUYER_TOKEN" = "None" ]; then echo "Buyer login failed."; exit 1; fi

echo "  PASS: Both personas authenticated."
echo ""

echo "2. Farmer Registers a Field (Authenticated via Hub)"
WKT="POLYGON((-119.48387145996094 36.40810420514039,-119.48382854461671 36.40449287165274,-119.4750738143921 36.40452743066338,-119.4751811027527 36.408052369005034,-119.48387145996094 36.40810420514039))"

REG_RES=$(curl -s -X POST "$AR_HUB_URL/register-field-boundary" \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"wkt\": \"$WKT\", \"threshold\": 95, \"return_s2_indices\": false}")

GEOID=$(echo "$REG_RES" | JSON "['Geo Id']")
if [ "$GEOID" = "None" ]; then
    GEOID=$(echo "$REG_RES" | JSON "['detail']['matched geo ids'][0]")
fi
echo "  Registered Field GeoID: $GEOID"
echo ""

echo "2A. Identity Resolution - Farmer Registers Exact Duplicate"
WKT_DUPE="POLYGON((-119.48387145996094 36.40810420514039,-119.48382854461671 36.40449287165274,-119.4750738143921 36.40452743066338,-119.4751811027527 36.408052369005034,-119.48387145996094 36.40810420514039))"
REG_DUPE_RES=$(curl -s -X POST "$AR_HUB_URL/register-field-boundary" \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"wkt\": \"$WKT_DUPE\", \"threshold\": 95, \"return_s2_indices\": false}")
DUPE_MSG=$(echo "$REG_DUPE_RES" | JSON "['message']")
DUPE_GEOID=$(echo "$REG_DUPE_RES" | JSON "['Geo Id']")
expect "Exact Duplicate Message" "$DUPE_MSG" "Exact geometry already registered."
expect "Exact Duplicate GeoID Matches" "$DUPE_GEOID" "$GEOID"
echo ""

echo "2B. Identity Resolution - Farmer Registers Nested Subplot"
WKT_SUBPLOT="POLYGON((-119.4800 36.4060, -119.4800 36.4070, -119.4780 36.4070, -119.4780 36.4060, -119.4800 36.4060))"
REG_SUB_RES=$(curl -s -X POST "$AR_HUB_URL/register-field-boundary" \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"wkt\": \"$WKT_SUBPLOT\", \"threshold\": 95, \"return_s2_indices\": false}")
SUB_MSG=$(echo "$REG_SUB_RES" | JSON "['message']")
SUB_GEOID=$(echo "$REG_SUB_RES" | JSON "['Geo Id']")
expect "Subplot Registered Successfully" "$SUB_MSG" "Field Boundary registered successfully."
if [ "$SUB_GEOID" = "$GEOID" ]; then
    echo "  FAIL: Subplot GeoID was identical to parent!"
    exit 1
else
    echo "  PASS: Subplot GeoID is unique."
fi
echo ""

echo "3. Farmer Creates FieldList on Pancake"
LIST_RES=$(curl -s -X POST "$PANCAKE_URL/fieldlists" \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"Farmer Owned Field\", \"geoids\": [\"$GEOID\"]}")

LIST_ID=$(echo "$LIST_RES" | JSON "['list_id']")
echo "  Created ListID: $LIST_ID"
echo ""

echo "4. Farmer self-issues 'Owner Grant' (1 year validity)"
OWNER_ISSUE_RES=$(curl -s -X POST "$PANCAKE_URL/grants/issue" \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"list_id\": \"$LIST_ID\", \"grantee_account\": \"farmer@demo.agstack.org\", \"purpose\": \"owner\", \"validity_days\": 365}")

OWNER_GRANT_ID=$(echo "$OWNER_ISSUE_RES" | JSON "['jti']")
OWNER_RCV_RES=$(curl -s -X GET "$PANCAKE_URL/grants/received" -H "Authorization: Bearer $FARMER_TOKEN")
OWNER_CREDENTIAL=$(echo "$OWNER_RCV_RES" | python3 -c "import sys,json; print(next(g['credential'] for g in json.load(sys.stdin) if g['jti'] == '$OWNER_GRANT_ID'))")
echo "  Owner Grant Issued: $OWNER_GRANT_ID"
echo ""

echo "5. Farmer checks their own L1 Access with Owner Credential"
OWNER_FETCH=$(curl -s -X GET "$AR_HUB_URL/fetch-field-wkt/$GEOID" \
  -H "X-Field-Grant: $OWNER_CREDENTIAL")
expect "Farmer Owner Credential Level" "$(echo "$OWNER_FETCH" | JSON "['MaskingLevel']")" "L1"
echo ""

echo "6. Farmer issues 'EUDR Grant' to Buyer"
BUYER_ISSUE_RES=$(curl -s -X POST "$PANCAKE_URL/grants/issue" \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"list_id\": \"$LIST_ID\", \"grantee_account\": \"buyer@demo.agstack.org\", \"purpose\": \"eudr-due-diligence\", \"validity_days\": 1}")

BUYER_GRANT_ID=$(echo "$BUYER_ISSUE_RES" | JSON "['jti']")
BUYER_RCV=$(curl -s -X GET "$PANCAKE_URL/grants/received" -H "Authorization: Bearer $BUYER_TOKEN")
BUYER_CREDENTIAL=$(echo "$BUYER_RCV" | python3 -c "import sys,json; print(next(g['credential'] for g in json.load(sys.stdin) if g['jti'] == '$BUYER_GRANT_ID'))")
echo "  Buyer Grant Issued: $BUYER_GRANT_ID"
echo ""

echo "7. Buyer fetches field with their Grant (Expect L1)"
BUYER_FETCH=$(curl -s -X GET "$AR_HUB_URL/fetch-field-wkt/$GEOID" \
  -H "X-Field-Grant: $BUYER_CREDENTIAL")
expect "Buyer Credential Level" "$(echo "$BUYER_FETCH" | JSON "['MaskingLevel']")" "L1"
echo ""

echo "8. 3-Way Split Assertions"
FARMER_BARE_FETCH=$(curl -s -X GET "$AR_HUB_URL/fetch-field-wkt/$GEOID" -H "Authorization: Bearer $FARMER_TOKEN")
expect "Farmer Bare JWT Level" "$(echo "$FARMER_BARE_FETCH" | JSON "['MaskingLevel']")" "L0"

ANON_FETCH=$(curl -s -X GET "$AR_HUB_URL/fetch-field-wkt/$GEOID")
expect "Anonymous Level" "$(echo "$ANON_FETCH" | JSON "['MaskingLevel']")" "L0"
echo ""

echo "9. EUDR Export Check (Buyer with Grant)"
EUDR_RES=$(curl -s -w "%{http_code}" -o /tmp/eudr_res.json -X GET "$AR_HUB_URL/geoid/$GEOID/eudr-export" \
  -H "X-Field-Grant: $BUYER_CREDENTIAL")
expect "EUDR Success Code" "$EUDR_RES" "200"
echo ""

echo "10. Revoke Buyer Grant"
REVOKE_RES=$(curl -s -w "\n%{http_code}" -X POST "$PANCAKE_URL/grants/revoke" \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"jti\": \"$BUYER_GRANT_ID\"}")
REVOKE_HTTP=$(echo "$REVOKE_RES" | tail -n1)
REVOKE_BODY=$(echo "$REVOKE_RES" | sed '$d')
echo "  Revoke Response Code: $REVOKE_HTTP"
echo "  Revoke Response Body: $REVOKE_BODY"
if [ "$REVOKE_HTTP" != "200" ]; then
    echo "  FAIL: Revocation failed."
    exit 1
fi
echo "  Buyer grant $BUYER_GRANT_ID revoked."
echo ""

echo "11. Post-Revocation Checks"
sleep 2
BUYER_FETCH_REVOKED=$(curl -s -X GET "$AR_HUB_URL/fetch-field-wkt/$GEOID" \
  -H "X-Field-Grant: $BUYER_CREDENTIAL")
expect "Buyer Revoked Level" "$(echo "$BUYER_FETCH_REVOKED" | JSON "['MaskingLevel']")" "L0"

EUDR_REVOKED_RES=$(curl -s -w "%{http_code}" -o /tmp/eudr_rev.json -X GET "$AR_HUB_URL/geoid/$GEOID/eudr-export" \
  -H "X-Field-Grant: $BUYER_CREDENTIAL")
expect "EUDR Revoked Code" "$EUDR_REVOKED_RES" "401"

OWNER_FETCH_AFTER=$(curl -s -X GET "$AR_HUB_URL/fetch-field-wkt/$GEOID" \
  -H "X-Field-Grant: $OWNER_CREDENTIAL")
expect "Farmer Owner Credential Level (Intact)" "$(echo "$OWNER_FETCH_AFTER" | JSON "['MaskingLevel']")" "L1"

echo "13. Trace-Forward (Refusal without Credentials)"
TF_REF_RES=$(curl -s -w "\n%{http_code}" -X POST "$AR_HUB_URL/traceforward" \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"seed_geoid\": \"$GEOID\", \"scope\": \"eudr-due-diligence\"}")
TF_REF_HTTP=$(echo "$TF_REF_RES" | tail -n1)
expect "Trace-Forward properly blocked (403)" "$TF_REF_HTTP" "403"
echo ""

echo "14. Trace-Forward Tier 1 (Owner Grant)"
TF_RES=$(curl -s -w "\n%{http_code}" -X POST "$AR_HUB_URL/traceforward" \
  -H "Authorization: Bearer $FARMER_TOKEN" \
  -H "X-Grant-Token: $OWNER_CREDENTIAL" \
  -H "Content-Type: application/json" \
  -d "{\"seed_geoid\": \"$GEOID\", \"scope\": \"eudr-due-diligence\"}")
TF_HTTP=$(echo "$TF_RES" | tail -n1)
expect "Tier 1 Trace-Forward Success Code" "$TF_HTTP" "200"
echo ""

echo "=========================================================="
echo " ALL EXPECTATIONS PASSED. DEMO COMPLETE."
echo "=========================================================="
exit 0
