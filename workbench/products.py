"""Explicit inventory products; a discounted Plus card is a separate product."""
PRODUCTS = {
    'plus_half': {'label': '半价 Plus', 'redeem_enabled': True},
    'plus': {'label': 'Plus', 'redeem_enabled': True},
    'pro100': {'label': 'Pro 100 (5×)', 'redeem_enabled': False},
    'pro200': {'label': 'Pro 200 (10×)', 'redeem_enabled': False},
    'pro500': {'label': 'Pro 500 (25×)', 'redeem_enabled': False},
}
