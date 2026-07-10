#!/bin/bash
# demo_e2e.sh
# End-to-end automated demo of the AgStack DPI Field Access Grant lifecycle.

set -e

AR_HUB_URL="http://127.0.0.1:8000"
AR_NODE_URL="http://127.0.0.1:8001"
PANCAKE_URL="http://127.0.0.1:8100"

echo "========================================"
echo "          VERSION 1: DIRECT TO AR NODE  "
echo "========================================"

echo "1. Logging into AR Hub"
LOGIN_RES=$(curl -s -X POST "$AR_HUB_URL/users/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=test_user@gmail.com&password=test@12345")

HUB_TOKEN=$(echo "$LOGIN_RES" | grep -oP '"access_token":\s*"\K[^"]+')
if [ -z "$HUB_TOKEN" ]; then
    echo "Login failed. Ensure AR Hub is running on $AR_HUB_URL."
    exit 1
fi
echo "Successfully logged in. Hub Token acquired."
echo ""

echo "2. Registering Field on AR Node (Direct)"
WKT="POLYGON((-115.11287927627565 32.401601610730026,-115.11332988739014 32.39794898464963,-115.11252522468568 32.397414220539915,-115.11190295219423 32.397541114005094,-115.11085152626039 32.39765894349167,-115.10995030403139 32.39849280930963,-115.10892033576967 32.398909739330705,-115.1090168952942 32.401764751871625,-115.11287927627565 32.401601610730026))"

REG_RES=$(curl -s -X POST "$AR_NODE_URL/register-field-boundary" \
  -H "accept: application/json" \
  -H "Content-Type: application/json" \
  -d "{\"wkt\": \"$WKT\", \"threshold\": 95, \"return_s2_indices\": false}")

GEOID=$(echo "$REG_RES" | grep -oP '"Geo Id":\s*"\K[^"]+')
echo "Registered Field. GeoID: $GEOID"
echo ""

echo "3. Creating FieldList on Pancake"
LIST_RES=$(curl -s -X POST "$PANCAKE_URL/fieldlists" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"Automated Test Field\", \"geoids\": [\"$GEOID\"]}")

LIST_ID=$(echo "$LIST_RES" | grep -oP '"list_id":\s*"\K[^"]+')
echo "Created ListID: $LIST_ID"
echo ""

echo "4. Issuing Grant on Pancake"
ISSUE_RES=$(curl -s -X POST "$PANCAKE_URL/grants/issue" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"list_id\": \"$LIST_ID\", \"grantee_account\": \"test_user@gmail.com\", \"purpose\": \"eudr-due-diligence\", \"validity_days\": 1}")

GRANT_ID=$(echo "$ISSUE_RES" | grep -oP '"jti":\s*"\K[^"]+')
echo "Issued Grant ID: $GRANT_ID"
echo ""

echo "5. Retrieving SD-JWT Credential"
RCV_RES=$(curl -s -X GET "$PANCAKE_URL/grants/received" \
  -H "Authorization: Bearer $HUB_TOKEN")

GRANT_TOKEN=$(echo "$RCV_RES" | grep -oP '"credential":\s*"\K[^"]+' | head -n 1)
echo "Successfully fetched SD-JWT grant credential from Pancake."
echo ""

echo "6. Fetch Field with Grant (Expect L1 - Direct)"
L1_RESPONSE=$(curl -s -X GET "$AR_NODE_URL/fetch-field-wkt/$GEOID" \
  -H "accept: application/json" \
  -H "X-Field-Grant: $GRANT_TOKEN")
echo "$L1_RESPONSE" | grep -o '"MaskingLevel":"[^"]*"' || true
echo ""

echo "7. Revoke Grant on Pancake"
curl -s -X POST "$PANCAKE_URL/grants/revoke" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"jti\": \"$GRANT_ID\"}" > /dev/null
echo "Grant $GRANT_ID revoked in the StatusList2021 registry."
echo ""

echo "8. Fetch Field with Revoked Grant (Expect L0 - Direct)"
L0_RESPONSE=$(curl -s -X GET "$AR_NODE_URL/fetch-field-wkt/$GEOID" \
  -H "accept: application/json" \
  -H "X-Field-Grant: $GRANT_TOKEN")
echo "$L0_RESPONSE" | grep -o '"MaskingLevel":"[^"]*"' || true
echo ""


echo "========================================"
echo "          VERSION 2: THROUGH HUB ONLY   "
echo "========================================"

echo "1. Registering Field via AR Hub (No Auth)"
WKT2="POLYGON((-119.70914483070375 36.621719124665596,-119.70914483070375 36.61996153472247,-119.7047245502472 36.61993568751255,-119.70475673675537 36.62173635574118,-119.70914483070375 36.621719124665596))"

REG_RES2=$(curl -s -X POST "$AR_HUB_URL/register-field-boundary" \
  -H "accept: application/json" \
  -H "Content-Type: application/json" \
  -d "{\"wkt\": \"$WKT2\", \"threshold\": 95, \"return_s2_indices\": false}")

GEOID2=$(echo "$REG_RES2" | grep -oP '"Geo Id":\s*"\K[^"]+')
echo "Registered Field via Hub. GeoID: $GEOID2"
echo ""

echo "2. Creating FieldList on Pancake"
LIST_RES2=$(curl -s -X POST "$PANCAKE_URL/fieldlists" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"Hub Routed Test Field\", \"geoids\": [\"$GEOID2\"]}")

LIST_ID2=$(echo "$LIST_RES2" | grep -oP '"list_id":\s*"\K[^"]+')
echo "Created ListID: $LIST_ID2"
echo ""

echo "3. Issuing Grant on Pancake"
ISSUE_RES2=$(curl -s -X POST "$PANCAKE_URL/grants/issue" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"list_id\": \"$LIST_ID2\", \"grantee_account\": \"test_user@gmail.com\", \"purpose\": \"eudr-due-diligence\", \"validity_days\": 1}")

GRANT_ID2=$(echo "$ISSUE_RES2" | grep -oP '"jti":\s*"\K[^"]+')
echo "Issued Grant ID: $GRANT_ID2"
echo ""

echo "4. Retrieving SD-JWT Credential"
RCV_RES2=$(curl -s -X GET "$PANCAKE_URL/grants/received" \
  -H "Authorization: Bearer $HUB_TOKEN")

GRANT_TOKEN2=$(echo "$RCV_RES2" | grep -oP '"credential":\s*"\K[^"]+' | head -n 1)
echo "Successfully fetched SD-JWT grant credential from Pancake."
echo ""

echo "5. Fetch Field with Grant (Expect L1 - Routed Through Hub)"
L1_RESPONSE2=$(curl -s -X GET "$AR_HUB_URL/fetch-field-wkt/$GEOID2" \
  -H "accept: application/json" \
  -H "X-Field-Grant: $GRANT_TOKEN2")
echo "$L1_RESPONSE2" | grep -o '"MaskingLevel":"[^"]*"' || true
echo ""

echo "6. Revoke Grant on Pancake"
curl -s -X POST "$PANCAKE_URL/grants/revoke" \
  -H "Authorization: Bearer $HUB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"jti\": \"$GRANT_ID2\"}" > /dev/null
echo "Grant $GRANT_ID2 revoked in the StatusList2021 registry."
echo ""

echo "7. Fetch Field with Revoked Grant (Expect L0 - Routed Through Hub)"
L0_RESPONSE2=$(curl -s -X GET "$AR_HUB_URL/fetch-field-wkt/$GEOID2" \
  -H "accept: application/json" \
  -H "X-Field-Grant: $GRANT_TOKEN2")
echo "$L0_RESPONSE2" | grep -o '"MaskingLevel":"[^"]*"' || true
echo ""

echo "Demo complete."
