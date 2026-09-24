# orders-api

The Orders service. The writable target of the refund-request task:
refund request handling lives in `src/checkout.py` and
`src/orders/handlers/refunds.py`.

Money-side refund policy (thresholds, approval routing) is NOT owned
here — it is consumed from the billing-policy service over the policy
client. See `src/checkout.py`'s module docstring before reintroducing
any local threshold.
