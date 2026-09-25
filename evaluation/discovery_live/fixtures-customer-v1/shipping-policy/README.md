# shipping-policy

Shipping owns the confirmation rules for every outbound shipment,
including refund-driven reversals. The rules live in
`src/shipping/confirmation.py`. NOTE: that file carries BOTH the
current S-2026-03 policy and the superseded S-2024-08 text it replaced
(regulators require the superseded block to stay visible for two
fiscal years) — a reader must not quote a recipient constant without
checking which revision block it belongs to.
