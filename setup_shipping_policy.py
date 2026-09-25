#!/usr/bin/env python3
"""
One-time script: Create an eBay fulfillment policy for USPS Ground Advantage
calculated shipping, then update .env with the new policy ID.
"""

import os
import sys
import requests
from pathlib import Path
from dotenv import load_dotenv

ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(ENV_PATH)

EBAY_TOKEN = os.environ.get("EBAY_TOKEN", "")
EBAY_SANDBOX = os.environ.get("EBAY_SANDBOX", "false").lower() == "true"
BASE_URL = "https://api.sandbox.ebay.com" if EBAY_SANDBOX else "https://api.ebay.com"

HEADERS = {
    "Authorization": f"Bearer {EBAY_TOKEN}",
    "Content-Type": "application/json",
    "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
}


def create_calculated_policy():
    """Create a USPS Ground Advantage calculated shipping fulfillment policy."""
    url = f"{BASE_URL}/sell/account/v1/fulfillment_policy"

    payload = {
        "name": "USPS Ground Advantage - Calculated",
        "marketplaceId": "EBAY_US",
        "handlingTime": {"value": 1, "unit": "DAY"},
        "shippingOptions": [
            {
                "optionType": "DOMESTIC",
                "costType": "CALCULATED",
                "shippingServices": [
                    {
                        "shippingCarrierCode": "USPS",
                        "shippingServiceCode": "USPSParcel",  # eBay code for USPS Ground Advantage
                        "freeShipping": False,
                        "sortOrder": 1,
                    }
                ],
            }
        ],
    }

    print(f"[setup] Creating calculated shipping policy on {'SANDBOX' if EBAY_SANDBOX else 'PRODUCTION'}...")
    print(f"[setup] URL: {url}")

    resp = requests.post(url, json=payload, headers=HEADERS)

    # If USPSGroundAdvantage fails, retry with USPSGround (legacy code)
    if resp.status_code not in (200, 201):
        resp_text = resp.text
        if "UNKNOWN_SHIPPING_SERVICE" in resp_text or "USPSGroundAdvantage" in resp_text:
            print("[setup] USPSGroundAdvantage not recognized, retrying with USPSGround...")
            payload["name"] = "USPS Ground - Calculated"
            payload["shippingOptions"][0]["shippingServices"][0]["shippingServiceCode"] = "USPSGround"
            resp = requests.post(url, json=payload, headers=HEADERS)

    if resp.status_code in (200, 201):
        data = resp.json()
        policy_id = data.get("fulfillmentPolicyId")
        print(f"[setup] Policy created! ID: {policy_id}")
        print(f"[setup] Name: {data.get('name')}")
        return policy_id
    else:
        print(f"[setup] FAILED ({resp.status_code}):")
        print(resp.text)
        return None


def update_env(policy_id):
    """Replace EBAY_FULFILLMENT_POLICY in .env with the new policy ID."""
    content = ENV_PATH.read_text()
    lines = content.splitlines()
    updated = False

    for i, line in enumerate(lines):
        if line.startswith("EBAY_FULFILLMENT_POLICY="):
            old_val = line.split("=", 1)[1]
            lines[i] = f"EBAY_FULFILLMENT_POLICY={policy_id}"
            updated = True
            print(f"[setup] Updated .env: {old_val} → {policy_id}")
            break

    if not updated:
        lines.append(f"EBAY_FULFILLMENT_POLICY={policy_id}")
        print(f"[setup] Added EBAY_FULFILLMENT_POLICY={policy_id} to .env")

    ENV_PATH.write_text("\n".join(lines) + "\n")


def main():
    if not EBAY_TOKEN or EBAY_TOKEN == "YOUR_EBAY_TOKEN_HERE":
        print("[setup] ERROR: No EBAY_TOKEN set in .env. Cannot create policy.")
        sys.exit(1)

    policy_id = create_calculated_policy()
    if not policy_id:
        print("[setup] Policy creation failed. .env unchanged.")
        sys.exit(1)

    update_env(policy_id)
    print("[setup] Done! Restart the app to pick up the new policy.")


if __name__ == "__main__":
    main()
