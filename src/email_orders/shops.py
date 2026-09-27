"""Fictional shop, customer and product name pools.

Every name below is invented for this demo. Domains use the `.invalid`
TLD, which RFC 2606 reserves for exactly this purpose (addresses that must
never resolve to a real host), so nothing here can collide with or
resemble a real shop.
"""
from __future__ import annotations

SHOPS = {
    "northbridge": {
        "display_name": "Northbridge Outfitters",
        "from_addr": "orders@northbridge-outfitters.invalid",
        "currency": "USD",
        "symbol": "$",
    },
    "lumen": {
        "display_name": "Lumen & Co",
        "from_addr": "no-reply@lumenandco.invalid",
        "currency": "EUR",
        "symbol": "€",  # euro sign
    },
    "cedarfern": {
        "display_name": "Cedar Fern Market",
        "from_addr": "confirm@cedarfernmarket.invalid",
        "currency": "GBP",
        "symbol": "£",  # pound sign
    },
    "pico": {
        "display_name": "Pico & Bramble",
        "from_addr": "receipts@picoandbramble.invalid",
        "currency": "USD",
        "symbol": "$",
    },
}

CUSTOMERS = [
    "Adaeze Okoro",
    "Frantisek Novak",
    "Marisol Pena",
    "Ingrid Solvberg",
    "Declan O'Malley",
    "Wei Cheng Tan",
    "Priya Raghunathan",
    "Tomasz Wisniewski",
]

PRODUCTS = [
    ("Trail Runner Backpack 28L", 64.00),
    ("Merino Wool Beanie", 18.50),
    ("Insulated Steel Bottle 750ml", 22.00),
    ("Packable Rain Shell", 89.00),
    ("Ceramic Pour-Over Set", 34.90),
    ("Linen Table Runner", 27.50),
    ("Cast Iron Trivet", 15.00),
    ("Beeswax Food Wraps (3-pack)", 12.25),
    ("Reclaimed Oak Cutting Board", 41.00),
    ("Wool Blend Throw", 58.00),
]

IRRELEVANT_SUBJECTS = [
    ("Northbridge Outfitters", "orders@northbridge-outfitters.invalid",
     "Your autumn newsletter is here"),
    ("Lumen & Co", "marketing@lumenandco.invalid",
     "20% off everything this weekend only"),
]
