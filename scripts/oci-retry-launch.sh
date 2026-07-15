#!/usr/bin/env bash
#
# Auto-retry launcher for the Oracle Ampere A1 Flex "cityarts-v2" instance.
#
# Oracle's Always Free A1 capacity is chronically exhausted; this loops the
# launch call across all availability domains until one succeeds, then prints
# the instance OCID + public IP and exits. Failed attempts cost nothing.
#
# ---------------------------------------------------------------------------
# ONE-TIME SETUP (do these first, they can't be scripted from here):
#
#   1. Install the OCI CLI:
#        winget install Oracle.OracleCLI      # (Windows), then reopen shell
#      or:  bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)"
#
#   2. Create an API key + config:
#        oci setup config
#      It asks for your User OCID, Tenancy OCID, region (all in the console
#      under Profile -> My profile / Tenancy), and generates a key pair. Then
#      upload the generated public key: console -> Profile -> My profile ->
#      API keys -> Add API key -> paste ~/.oci/oci_api_key_public.pem.
#      Verify with:  oci iam region list      (should print JSON, no error)
#
#   3. Point SSH_KEY below at the PUBLIC key you want baked into the box
#      (the .pub whose private half you'll SSH with).
# ---------------------------------------------------------------------------

set -u

# --- things you must set --------------------------------------------------
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa.pub}"   # public key to install on the box
DISPLAY_NAME="${DISPLAY_NAME:-cityarts-v2}"
OCPUS="${OCPUS:-4}"                            # 4/24 = full Always Free A1; drop to 2/12 or 1/6 if capacity keeps failing
MEM_GB="${MEM_GB:-24}"
SLEEP="${SLEEP:-90}"                          # seconds between attempts
SHAPE="VM.Standard.A1.Flex"
OS="Canonical Ubuntu"
OS_VERSION="24.04"
# --------------------------------------------------------------------------

command -v oci >/dev/null || { echo "ERROR: oci CLI not found — do step 1 above."; exit 1; }
[ -f "$SSH_KEY" ] || { echo "ERROR: SSH public key not found at $SSH_KEY (set SSH_KEY=...)"; exit 1; }

echo "==> Discovering tenancy details…"
TENANCY=$(oci iam compartment list --query 'data[0]."compartment-id"' --raw-output 2>/dev/null)
[ -n "$TENANCY" ] || TENANCY=$(grep -E '^tenancy' "$HOME/.oci/config" 2>/dev/null | head -1 | sed 's/.*= *//')
[ -n "$TENANCY" ] || { echo "ERROR: could not read tenancy OCID from OCI config."; exit 1; }
COMPARTMENT="$TENANCY"   # root compartment (matches where cityarts-v2 was being created)

# Image: newest aarch64 Ubuntu 24.04 in this compartment
IMAGE=$(oci compute image list -c "$COMPARTMENT" \
  --operating-system "$OS" --operating-system-version "$OS_VERSION" \
  --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC \
  --query 'data[0].id' --raw-output)
[ -n "$IMAGE" ] && [ "$IMAGE" != "null" ] || { echo "ERROR: no $OS $OS_VERSION aarch64 image found."; exit 1; }

# Subnet: reuse the existing one (same VCN as the old box, so port 8080 rule carries over)
SUBNET=$(oci network subnet list -c "$COMPARTMENT" --query 'data[0].id' --raw-output)
[ -n "$SUBNET" ] && [ "$SUBNET" != "null" ] || { echo "ERROR: no subnet found in compartment."; exit 1; }

# All availability domains to rotate through
mapfile -t ADS < <(oci iam availability-domain list --query 'data[].name' --raw-output | tr -d '[],"' | sed '/^\s*$/d' | sed 's/^ *//')
[ "${#ADS[@]}" -gt 0 ] || { echo "ERROR: could not list availability domains."; exit 1; }

echo "    tenancy   : ${TENANCY:0:25}…"
echo "    image     : ${IMAGE:0:25}…"
echo "    subnet    : ${SUBNET:0:25}…"
echo "    ADs       : ${ADS[*]}"
echo "    shape     : $SHAPE  ${OCPUS} OCPU / ${MEM_GB} GB"
echo "==> Retrying every ${SLEEP}s until capacity is found. Ctrl-C to stop."
echo

attempt=0
while true; do
  for AD in "${ADS[@]}"; do
    attempt=$((attempt+1))
    printf '[%s] attempt %d — %s … ' "$(date +%H:%M:%S)" "$attempt" "$AD"
    OUT=$(oci compute instance launch \
      --compartment-id "$COMPARTMENT" \
      --availability-domain "$AD" \
      --shape "$SHAPE" \
      --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM_GB}" \
      --image-id "$IMAGE" \
      --subnet-id "$SUBNET" \
      --assign-public-ip true \
      --display-name "$DISPLAY_NAME" \
      --ssh-authorized-keys-file "$SSH_KEY" \
      --wait-for-state RUNNING \
      2>&1)
    RC=$?
    if [ $RC -eq 0 ]; then
      ID=$(echo "$OUT" | grep -o '"id": *"[^"]*"' | head -1 | sed 's/.*"id": *"\([^"]*\)".*/\1/')
      echo "SUCCESS"
      echo
      echo "Instance is RUNNING: $ID"
      echo "Fetching public IP…"
      VNIC=$(oci compute instance list-vnics --instance-id "$ID" --query 'data[0]."public-ip"' --raw-output)
      echo "  PUBLIC IP: $VNIC"
      echo
      echo "Next: set the GitHub secret ORACLE_HOST to $VNIC, then run scripts/setup.sh on the box."
      exit 0
    fi
    if echo "$OUT" | grep -qiE "out of (host )?capacity|InternalError"; then
      echo "no capacity"
    else
      echo "UNEXPECTED ERROR (stopping):"
      echo "$OUT"
      exit 1
    fi
  done
  sleep "$SLEEP"
done
