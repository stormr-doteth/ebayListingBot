"""One-time script: create eBay sandbox business policies and print their IDs."""
import os
import json
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

TOKEN = os.environ.get("EBAY_TOKEN", "")
SANDBOX = os.environ.get("EBAY_SANDBOX", "false").lower() == "true"
BASE = "https://api.sandbox.ebay.com" if SANDBOX else "https://api.ebay.com"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "Content-Language": "en-US",
    "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
}

# 1. Fulfillment (shipping) policy
print("Creating fulfillment policy...")
fulfillment = requests.post(f"{BASE}/sell/account/v1/fulfillment_policy", headers=headers, json={
    "name": "Standard Shipping",
    "marketplaceId": "EBAY_US",
    "handlingTime": {"value": 1, "unit": "DAY"},
    "shippingOptions": [{
        "optionType": "DOMESTIC",
        "costType": "FLAT_RATE",
        "shippingServices": [{
            "shippingServiceCode": "USPSFirstClass",
            "shippingCost": {"value": "4.99", "currency": "USD"},
            "sortOrder": 1,
            "freeShipping": False,
        }],
    }],
})
print(f"  Status: {fulfillment.status_code}")
print(f"  Response: {fulfillment.text}")
fulfillment_id = fulfillment.json().get("fulfillmentPolicyId", "FAILED")

# 2. Payment policy
print("\nCreating payment policy...")
payment = requests.post(f"{BASE}/sell/account/v1/payment_policy", headers=headers, json={
    "name": "Immediate Pay",
    "marketplaceId": "EBAY_US",
    "immediatePay": True,
    "paymentMethods": [{"paymentMethodType": "PERSONAL_CHECK"}],
})
print(f"  Status: {payment.status_code}")
print(f"  Response: {payment.text}")
payment_id = payment.json().get("paymentPolicyId", "FAILED")

# 3. Return policy
print("\nCreating return policy...")
returns = requests.post(f"{BASE}/sell/account/v1/return_policy", headers=headers, json={
    "name": "30 Day Returns",
    "marketplaceId": "EBAY_US",
    "returnsAccepted": True,
    "returnPeriod": {"value": 30, "unit": "DAY"},
    "refundMethod": "MONEY_BACK",
    "returnShippingCostPayer": "BUYER",
})
print(f"  Status: {returns.status_code}")
print(f"  Response: {returns.text}")
return_id = returns.json().get("returnPolicyId", "FAILED")

print("\n" + "=" * 50)
print("Add these to your .env file:")
print(f"EBAY_FULFILLMENT_POLICY={fulfillment_id}")
print(f"EBAY_PAYMENT_POLICY={payment_id}")
print(f"EBAY_RETURN_POLICY={return_id}")
