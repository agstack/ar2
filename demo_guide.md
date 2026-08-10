# Complete Zero-to-Hero AgStack DPI Manual Testing Guide

This guide is starting from absolute scratch. It walks you through setting up the cryptographic keys, starting all three servers correctly, and executing every step of the end-to-end data sharing lifecycle using `curl` commands, including Buyer EUDR grants and revocation.

---

## Part 1: Setting up the Servers

You will need to open **three separate terminal windows**, one for each server.

### Terminal 1: Setup and Start Pancake (The Issuer)
Pancake acts as the credential issuer. It generates the cryptographic keys and signs the grants.
```bash
# 1. Navigate to Pancake
cd ~/pancake

# 2. Setup python environment (if not already done)
python3 -m venv venv
source venv/bin/activate
pip install -r services/requirements.txt

# 3. Cryptographic Keys Configuration
> [!IMPORTANT]
> **Issuer Keys Configuration**
> For security, cryptographic keys are intentionally excluded from the `.env` configuration. You must export them in your shell session before running the services.
> 
> Create a shell script (e.g., `demo_env.sh`) containing:

> # we reuse the pre-packaged testkit keys for local testing:
> export PANCAKE_ISSUER_KEY=$(cat ~/pancake/services/pancake_services/grants/testkit/dev_keys/dev_issuer_private.pem)
> export AR_TRUSTED_ISSUER_PUBKEY=$(cat ~/pancake/services/pancake_services/grants/testkit/dev_keys/dev_issuer_public.pem)

> *(Note: For a secure live demo, generate a dedicated keypair using `pancake_services.grants.issuer.generate_keypair_pem()` instead).*

# 4. Load the PRIVATE key from your environment script
source /path/to/your/demo_env.sh

# 5. Start the Pancake server on port 8100
cd services/
uvicorn pancake_services.grants.app:create_app --factory --host 0.0.0.0 --port 8100
```

### Terminal 2: Setup and Start the Node (ar2)
The Node holds the actual sensitive data and verifies credentials before granting access.
```bash
# 1. Navigate to the Node
cd ~/ar2

# 2. Setup python environment
python3 -m venv ar2-env
source ar2-env/bin/activate
pip install -r requirements.txt

# 3. Load the PUBLIC key from your environment script (Ensure demo_env.sh is sourced!)
source /path/to/your/demo_env.sh

# 4. Start the Node server on port 8001
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

### Terminal 3: Setup and Start the Hub Gateway (ar2-hub)
The Hub acts as the public facing proxy and handles user registration/login.
```bash
# 1. Navigate to the Hub
cd ~/ar2-hub

# 2. Setup python environment
python3 -m venv ar2-hub-env
source ar2-hub-env/bin/activate
pip install -r requirements.txt

# 3. Start the Hub Gateway on port 8000
uvicorn hub_main:app --host 0.0.0.0 --port 8000 --reload

```

---

## Part 2: The E2E Data Flow

Open a **fourth terminal** to run these commands sequentially. 

### Step 1: Register and Login as Farmer & Buyer

First, we create both the Farmer and Buyer accounts on the Hub.

**Register Farmer & Buyer:**
```bash
curl -s -X POST "http://127.0.0.1:8000/users/register" \
  -H "Content-Type: application/json" \
  -d '{"first_name": "Flora", "last_name": "Farmer", "email": "farmer@demo.agstack.org", "phone": "+50400000001", "password": "Demo#Farmer1", "role": "farmer", "country": "USA"}'

curl -s -X POST "http://127.0.0.1:8000/users/register" \
  -H "Content-Type: application/json" \
  -d '{"first_name": "Bob", "last_name": "Buyer", "email": "buyer@demo.agstack.org", "phone": "+50400000002", "password": "Demo#Buyer1", "role": "buyer", "country": "USA"}'
```

**Login Farmer:**
```bash
curl -s -X POST "http://127.0.0.1:8000/users/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=farmer@demo.agstack.org&password=Demo%23Farmer1"
```
> [!IMPORTANT]
> Copy the Farmer's `access_token` and replace `<FARMER_JWT>` in the commands below.

**Login Buyer:**
```bash
curl -s -X POST "http://127.0.0.1:8000/users/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=buyer@demo.agstack.org&password=Demo%23Buyer1"
```
> [!IMPORTANT]
> Copy the Buyer's `access_token` and replace `<BUYER_JWT>` in the commands below.

### Step 2: Register a Field Boundary

The farmer registers their farm's geometry.
```bash
curl -s -X POST "http://127.0.0.1:8000/register-field-boundary" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <FARMER_JWT>" \
  -d '{"wkt": "POLYGON((-119.48387145996094 36.40810420514039,-119.48382854461671 36.40449287165274,-119.4750738143921 36.40452743066338,-119.4751811027527 36.408052369005034,-119.48387145996094 36.40810420514039))", "threshold": 95, "return_s2_indices": false}'
```
> [!IMPORTANT]
> Copy the `Geo Id`. Replace `<GEOID>` in the commands below.

### Step 3: Bundle the Field into a List on Pancake

```bash
curl -s -X POST "http://127.0.0.1:8100/fieldlists" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <FARMER_JWT>" \
  -d '{"name": "Farmer Owned Field", "geoids": ["<GEOID>"]}'
```
> [!IMPORTANT]
> Copy the `list_id`. Replace `<LIST_ID>` in the commands below.

### Step 4: Farmer Self-Issues an "Owner Grant"

Mint a Verifiable Credential confirming ownership.
```bash
curl -s -X POST "http://127.0.0.1:8100/grants/issue" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <FARMER_JWT>" \
  -d '{"list_id": "<LIST_ID>", "grantee_account": "farmer@demo.agstack.org", "purpose": "owner", "validity_days": 365}'
```

Retrieve the Farmer's credential:
```bash
curl -s -X GET "http://127.0.0.1:8100/grants/received" \
  -H "Authorization: Bearer <FARMER_JWT>"
```
> [!IMPORTANT]
> Find the `credential` field and copy the ENTIRE long string. We will refer to this as `<OWNER_CREDENTIAL>`.

### Step 5: Farmer issues an "EUDR Grant" to the Buyer

Now the Farmer mints a credential strictly for the Buyer for EUDR compliance purposes.
```bash
curl -s -X POST "http://127.0.0.1:8100/grants/issue" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <FARMER_JWT>" \
  -d '{"list_id": "<LIST_ID>", "grantee_account": "buyer@demo.agstack.org", "purpose": "eudr-due-diligence", "validity_days": 1}'
```
> [!TIP]
> Notice the `jti` in the response! This is the unique ID for the Buyer's grant. Save this as `<BUYER_GRANT_ID>` for the revocation step later.

Retrieve the Buyer's credential (using the Buyer's JWT):
```bash
curl -s -X GET "http://127.0.0.1:8100/grants/received" \
  -H "Authorization: Bearer <BUYER_JWT>"
```
> [!IMPORTANT]
> Find the `credential` field in the response and copy the ENTIRE long string. We will refer to this as `<BUYER_CREDENTIAL>`.

---

## Part 3: Data Access Control & Revocation Checks

### Test A: Owner Access
The Farmer provides their cryptographic credential proving they own the field.
```bash
curl -s -X GET "http://127.0.0.1:8000/fetch-field-wkt/<GEOID>" \
  -H "X-Field-Grant: <OWNER_CREDENTIAL>"
```
*Expected:* `"MaskingLevel": "L1"`

### Test B: Buyer Access
The Buyer fetches the field using their granted credential.
```bash
curl -s -X GET "http://127.0.0.1:8000/fetch-field-wkt/<GEOID>" \
  -H "X-Field-Grant: <BUYER_CREDENTIAL>"
```
*Expected:* `"MaskingLevel": "L1"`

### Test C: Anonymous / Bare Access (3-way split)
```bash
# Bare JWT (logged in, but no field grant)
curl -s -X GET "http://127.0.0.1:8000/fetch-field-wkt/<GEOID>" -H "Authorization: Bearer <FARMER_JWT>"
# Completely Anonymous
curl -s -X GET "http://127.0.0.1:8000/fetch-field-wkt/<GEOID>"
```
*Expected:* Both should return `"MaskingLevel": "L0"`

### Test D: EUDR Export (Buyer with Grant)
```bash
curl -s -X GET "http://127.0.0.1:8000/geoid/<GEOID>/eudr-export" \
  -H "X-Field-Grant: <BUYER_CREDENTIAL>"
```
*Expected:* `HTTP 200` with export data.

### Test E: Revoke the Buyer Grant
The Farmer revokes the grant using the `<BUYER_GRANT_ID>` (`jti`) minted in Step 5.
```bash
curl -i -s -X POST "http://127.0.0.1:8100/grants/revoke" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <FARMER_JWT>" \
  -d '{"jti": "<BUYER_GRANT_ID>"}'
```
*Expected:* `HTTP 200 OK`

### Test F: Post-Revocation Checks
Verify the Buyer is now blocked, but the Farmer still has access.
```bash
# Buyer Fetch Field (Should be L0)
curl -s -X GET "http://127.0.0.1:8000/fetch-field-wkt/<GEOID>" \
  -H "X-Field-Grant: <BUYER_CREDENTIAL>"

# Buyer EUDR Export (Should be 401 Unauthorized)
curl -i -s -X GET "http://127.0.0.1:8000/geoid/<GEOID>/eudr-export" \
  -H "X-Field-Grant: <BUYER_CREDENTIAL>"

# Farmer Fetch Field (Should still be L1)
curl -s -X GET "http://127.0.0.1:8000/fetch-field-wkt/<GEOID>" \
  -H "X-Field-Grant: <OWNER_CREDENTIAL>"
```

### Test G: MEAL Audit Report
Verify that Pancake's tamper-evident ledger logged the entire lifecycle.
```bash
curl -s -X GET "http://127.0.0.1:8100/audit/<GEOID>/report" \
  -H "Authorization: Bearer <FARMER_JWT>"
```
*Expected:* `HTTP 200 OK` with full signed provenance chain and `"all_chains_valid": true`.
