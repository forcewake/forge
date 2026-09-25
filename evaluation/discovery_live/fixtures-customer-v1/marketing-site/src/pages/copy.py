"""Landing-page copy blocks (marketing-site)."""

LANDING_HERO = "Ship refunds customers actually understand."
LANDING_SUB = "Checkout, billing and shipping working as one platform."

PRICING_TIERS = {
    "starter": "$0/mo",
    "business": "$499/mo",
    "enterprise": "talk to us",
}

CAMPAIGN_UTM_TEMPLATE = "utm_source={source}&utm_campaign={campaign}"


def render_hero(campaign: str) -> str:
    """The hero block for one campaign."""
    return f"{LANDING_HERO} — {LANDING_SUB} [{CAMPAIGN_UTM_TEMPLATE.format(source='site', campaign=campaign)}]"
