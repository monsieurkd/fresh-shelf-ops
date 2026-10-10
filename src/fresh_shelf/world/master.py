"""Master data: the estate, the supply base and the fresh range.

These are the entities the source systems already agree on (they come from the
merchandising and store systems of record). Everything here is hand-authored to
be plausible rather than sampled from a distribution: an eight-store South
Australian group, a supply base that matches each department's real logistics
(daily bread, three-times-a-week seafood, a produce DC that runs six days), and
a range where shelf life — not price — is what decides whether an item can be
waste or not.

Numbers are per average store (``size_index`` 1.0) and per week; the simulation
scales them by store size, day of week, season, weather and promotion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from zlib import crc32

from ..config import Config

# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------
# size_index        relative fresh turnover vs an average store (drives demand)
# scan_compliance   how often a disposal actually gets scanned (D07)
# md_discipline     how systematically this store runs its markdown pass
# competitor        local price competition (shifts demand elasticity up)

# fmt: off
STORE_ROWS = [
    # code, name, suburb, postcode, lat, lon, format, size_index, opened,
    # scan_compliance, md_discipline, competitor_pressure
    ("S01", "Norwood Fresh Market",     "Norwood",       5067, -34.9205, 138.6327, "Metro",   1.45, "2009-03-16", 0.92, 1.10, 1.05),
    ("S02", "Glenelg Beach Market",     "Glenelg",       5045, -34.9800, 138.5150, "Metro",   1.30, "2012-11-05", 0.89, 1.05, 0.95),
    ("S03", "Marion Central",           "Oaklands Park", 5046, -35.0160, 138.5430, "Super",   1.55, "2004-08-23", 0.81, 0.92, 1.18),
    ("S04", "Prospect Village",         "Prospect",      5082, -34.8860, 138.5940, "Metro",   1.05, "2016-06-14", 0.88, 1.00, 1.02),
    ("S05", "Mount Barker Hills",       "Mount Barker",  5251, -35.0670, 138.8580, "Super",   0.92, "2015-02-09", 0.83, 0.95, 0.90),
    ("S06", "Gawler North",             "Gawler",        5118, -34.5990, 138.7450, "Super",   0.88, "2011-10-31", 0.78, 0.88, 0.88),
    ("S07", "Whyalla Foreshore",        "Whyalla",       5600, -33.0330, 137.5650, "Express", 0.62, "2013-05-20", 0.73, 0.80, 0.72),
    ("S08", "Victor Harbor Coast",      "Victor Harbor", 5211, -35.5510, 138.6240, "Express", 0.70, "2018-09-03", 0.76, 0.86, 0.80),
]
# fmt: on

# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------
# delivery_weekdays  ISO weekday numbers (1=Mon .. 7=Sun) the DC/store sends
# reliability        probability a line is filled to (at least) the ordered qty

# fmt: off
SUPPLIER_ROWS = [
    # code, name, type, region, lead_time_days, delivery_weekdays, min_order_value, reliability
    ("SUP-DC-PRO",  "SA Produce Distribution Centre", "Distribution centre", "Pooraka, SA",          1, (1, 2, 3, 4, 5, 6), 400.0, 0.93),
    ("SUP-VIR-VEG", "Virginia Fresh Growers",         "Local grower",        "Virginia, SA",         2, (1, 3, 5),          250.0, 0.90),
    ("SUP-LEN-FRU", "Lenswood Orchard Co",            "Local grower",        "Lenswood, SA",         3, (2, 5),             180.0, 0.95),
    ("SUP-RIV-CIT", "Riverland Citrus Group",         "Local grower",        "Renmark, SA",          3, (3,),               220.0, 0.97),
    ("SUP-BAK-OFF", "Bacchus Bake-off Supply",        "In-store bakery",     "Edinburgh, SA",        1, (1, 2, 3, 4, 5, 6, 7), 300.0, 0.98),
    ("SUP-ADL-SOUR","Adelaide Sourdough Co",          "Artisan bakery",      "Thebarton, SA",        2, (1, 3, 5),          150.0, 0.94),
    ("SUP-BAR-SML", "Barossa Smallgoods",             "Smallgoods",          "Tanunda, SA",          2, (1, 4),             300.0, 0.96),
    ("SUP-ADL-DIP", "Adelaide Dips & Deli Co",        "Deli manufacturer",   "Mile End, SA",         2, (2, 5),             200.0, 0.92),
    ("SUP-FLE-MLK", "Fleurieu Milk Company",          "Dairy processor",     "Myponga, SA",          2, (2, 5),             500.0, 0.99),
    ("SUP-BAR-CHS", "Barossa Cheese Makers",          "Cheese maker",        "Nuriootpa, SA",        3, (3,),               250.0, 0.95),
    ("SUP-PTL-SEA", "Port Lincoln Seafood",           "Seafood wholesaler",  "Port Lincoln, SA",     2, (2, 4, 6),          500.0, 0.88),
    ("SUP-KI-OYS",  "Kangaroo Island Oyster Co",      "Oyster farm",         "Kangaroo Island, SA",  3, (4,),               300.0, 0.91),
    ("SUP-ADL-FSH", "Adelaide Fish Co-op",            "Seafood co-op",       "Fulham, SA",           1, (1, 2, 3, 4, 5),    350.0, 0.86),
    ("SUP-SUN-BER", "Sundale Berry Farms",            "Berry grower",        "Hahndorf, SA",         2, (3, 6),             200.0, 0.89),
]
# fmt: on

DEPARTMENTS = {
    "P": "Produce",
    "B": "Bakery",
    "D": "Deli",
    "Y": "Dairy",
    "S": "Seafood",
}

#: Season profiles: monthly multipliers on base demand (Jan..Dec).
SEASON_PROFILES: dict[str, list[float]] = {
    "all_year": [1.0] * 12,
    "summer": [1.35, 1.30, 1.15, 0.95, 0.80, 0.70, 0.70, 0.75, 0.90, 1.05, 1.20, 1.35],
    "winter": [0.75, 0.78, 0.90, 1.05, 1.20, 1.35, 1.35, 1.30, 1.15, 1.00, 0.85, 0.78],
    "autumn": [0.95, 1.00, 1.15, 1.25, 1.15, 1.00, 0.90, 0.90, 1.00, 1.05, 1.00, 0.95],
    "spring": [0.90, 0.90, 1.00, 1.05, 1.00, 0.90, 1.00, 1.10, 1.30, 1.35, 1.20, 1.00],
    "christmas": [0.90, 0.90, 0.95, 1.00, 1.00, 1.00, 1.00, 1.00, 1.05, 1.10, 1.45, 2.60],
}

# ---------------------------------------------------------------------------
# The fresh range
# ---------------------------------------------------------------------------
# name, dept, category, unit, pack, retail, cost, weekly_units, life_days,
# life_at_delivery, supplier, season, heat, rain, markdown_responsiveness, promos_pa
#   unit                'ea' (each) or 'kg' (weighed, fractional demand)
#   pack                order multiple; deliveries arrive in these quantities
#   weekly_units        units sold per week at an average store
#   life_days           total shelf life from receipt
#   life_at_delivery    remaining life guaranteed at delivery (<= life_days)
#   heat / rain         demand multiplier per 5 degrees over 25C / per 10mm rain
#   md_responsiveness   how strongly a markdown moves units (0.5 stubborn, 1.4 flies)
#   promos_pa           catalogue promotions per year (drives the promo feed)

# fmt: off
ITEM_ROWS = [
    # ---- Produce -----------------------------------------------------------
    ("Bananas",                    "P", "Fruit",        "kg", 6.0,  4.50, 2.55, 480, 12, 8,  "SUP-DC-PRO",  "all_year",  0.030,  0.00, 1.15, 3),
    ("Fuji Apples",                "P", "Fruit",        "kg", 5.0,  5.20, 3.10, 260, 21, 12, "SUP-LEN-FRU", "autumn",    0.005,  0.00, 1.00, 2),
    ("Royal Gala Apples",          "P", "Fruit",        "kg", 5.0,  5.40, 3.20, 240, 21, 12, "SUP-LEN-FRU", "autumn",    0.005,  0.00, 1.00, 2),
    ("Packham Pears",              "P", "Fruit",        "kg", 5.0,  5.60, 3.30, 110, 18, 10, "SUP-LEN-FRU", "autumn",    0.005,  0.00, 0.95, 1),
    ("Navel Oranges",              "P", "Fruit",        "kg", 6.0,  3.90, 2.05, 210, 21, 14, "SUP-RIV-CIT", "winter",    0.010,  0.00, 1.00, 2),
    ("Strawberries 250g",          "P", "Berries",      "ea", 12.0, 4.50, 2.70, 145, 6,  4,  "SUP-SUN-BER", "summer",    0.020,  0.00, 1.30, 4),
    ("Blueberries 125g",           "P", "Berries",      "ea", 12.0, 4.20, 2.40, 120, 12, 8,  "SUP-SUN-BER", "summer",    0.015,  0.00, 1.25, 3),
    ("Raspberries 125g",           "P", "Berries",      "ea", 8.0,  5.50, 3.40, 55,  4,  2,  "SUP-SUN-BER", "summer",    0.020,  0.00, 1.35, 2),
    ("Seedless Grapes 500g",       "P", "Fruit",        "ea", 8.0,  5.90, 3.85, 130, 9,  6,  "SUP-DC-PRO",  "autumn",    0.015,  0.00, 1.15, 3),
    ("Avocado Hass",               "P", "Fruit",        "ea", 24.0, 1.80, 1.05, 640, 7,  4,  "SUP-DC-PRO",  "all_year",  0.020,  0.00, 1.35, 4),
    ("Mango Calypso",              "P", "Fruit",        "ea", 12.0, 3.50, 2.10, 90,  8,  5,  "SUP-DC-PRO",  "summer",    0.015,  0.00, 1.20, 2),
    ("Watermelon Seedless",        "P", "Fruit",        "kg", 3.0,  2.20, 1.15, 180, 14, 10, "SUP-DC-PRO",  "summer",    0.045, -0.010, 1.05, 3),
    ("Rockmelon",                  "P", "Fruit",        "kg", 3.0,  3.20, 1.80, 95,  10, 7,  "SUP-DC-PRO",  "summer",    0.035, -0.010, 1.05, 2),
    ("Tomatoes Truss",             "P", "Vegetables",   "kg", 4.0,  6.50, 3.90, 240, 7,  5,  "SUP-VIR-VEG", "summer",    0.025,  0.00, 1.20, 3),
    ("Cherry Tomatoes 250g",       "P", "Vegetables",   "ea", 12.0, 3.50, 1.95, 210, 8,  6,  "SUP-VIR-VEG", "all_year",  0.020,  0.00, 1.15, 3),
    ("Lebanese Cucumbers",         "P", "Vegetables",   "kg", 4.0,  5.90, 3.20, 165, 8,  5,  "SUP-VIR-VEG", "summer",    0.030,  0.00, 1.15, 2),
    ("Capsicum Red",               "P", "Vegetables",   "kg", 3.0,  8.90, 5.20, 130, 10, 7,  "SUP-VIR-VEG", "all_year",  0.020,  0.00, 1.10, 2),
    ("Baby Spinach 120g",          "P", "Leafy",        "ea", 12.0, 4.20, 2.30, 235, 5,  3,  "SUP-VIR-VEG", "all_year",  0.010,  0.00, 1.25, 3),
    ("Cos Lettuce",                "P", "Leafy",        "ea", 12.0, 3.20, 1.60, 330, 5,  3,  "SUP-VIR-VEG", "summer",    0.015,  0.00, 1.30, 3),
    ("Iceberg Lettuce",            "P", "Leafy",        "ea", 12.0, 2.90, 1.40, 290, 7,  5,  "SUP-VIR-VEG", "summer",    0.015,  0.00, 1.25, 3),
    ("Kale Bunch",                 "P", "Leafy",        "ea", 10.0, 4.50, 2.40, 75,  6,  4,  "SUP-VIR-VEG", "winter",    0.005,  0.00, 1.10, 1),
    ("Broccoli",                   "P", "Vegetables",   "kg", 4.0,  6.90, 3.80, 150, 9,  6,  "SUP-VIR-VEG", "winter",    0.010,  0.00, 1.05, 2),
    ("Cauliflower",                "P", "Vegetables",   "ea", 8.0,  4.50, 2.35, 95,  10, 7,  "SUP-VIR-VEG", "winter",    0.010,  0.00, 1.05, 2),
    ("Brussels Sprouts 500g",      "P", "Vegetables",   "ea", 8.0,  4.90, 2.80, 60,  8,  6,  "SUP-VIR-VEG", "winter",    0.005,  0.00, 0.95, 1),
    ("Carrots 1kg",                "P", "Vegetables",   "ea", 10.0, 2.30, 1.05, 300, 21, 14, "SUP-VIR-VEG", "all_year",  0.005,  0.00, 0.90, 2),
    ("Brushed Potatoes 2kg",       "P", "Vegetables",   "ea", 6.0,  4.80, 2.30, 240, 30, 21, "SUP-VIR-VEG", "winter",   -0.015,  0.010, 0.85, 3),
    ("Brown Onions 1kg",           "P", "Vegetables",   "ea", 10.0, 2.60, 1.15, 210, 30, 21, "SUP-VIR-VEG", "all_year", -0.005,  0.005, 0.85, 2),
    ("Mushrooms Cup 200g",         "P", "Vegetables",   "ea", 12.0, 4.10, 2.35, 185, 6,  4,  "SUP-VIR-VEG", "all_year",  0.005,  0.00, 1.15, 2),
    ("Asparagus Bunch",            "P", "Vegetables",   "ea", 10.0, 5.50, 3.10, 70,  6,  4,  "SUP-VIR-VEG", "spring",    0.005,  0.00, 1.15, 2),
    ("Snow Peas 200g",             "P", "Vegetables",   "ea", 10.0, 5.20, 3.00, 65,  5,  3,  "SUP-VIR-VEG", "spring",    0.005,  0.00, 1.20, 2),
    ("Coriander Bunch",            "P", "Herbs",        "ea", 12.0, 3.00, 1.40, 145, 5,  3,  "SUP-VIR-VEG", "all_year",  0.005,  0.00, 1.20, 1),
    ("Basil Bunch",                "P", "Herbs",        "ea", 12.0, 3.20, 1.50, 105, 5,  3,  "SUP-VIR-VEG", "summer",    0.010,  0.00, 1.20, 1),
    ("Continental Parsley Bunch",  "P", "Herbs",        "ea", 12.0, 3.00, 1.40, 85,  5,  3,  "SUP-VIR-VEG", "all_year",  0.005,  0.00, 1.15, 1),
    ("Salad Kit 350g",             "P", "Leafy",        "ea", 8.0,  5.50, 3.00, 120, 6,  4,  "SUP-VIR-VEG", "summer",    0.015,  0.00, 1.25, 3),

    # ---- Bakery (in-store bake-off lives 1-2 days) -------------------------
    ("White Sandwich Loaf 650g",   "B", "Bread",        "ea", 8.0,  2.60, 1.15, 420, 3,  2,  "SUP-BAK-OFF", "all_year",  0.000,  0.010, 1.30, 4),
    ("Wholemeal Loaf 650g",        "B", "Bread",        "ea", 8.0,  2.80, 1.25, 260, 3,  2,  "SUP-BAK-OFF", "all_year",  0.000,  0.010, 1.25, 3),
    ("Multigrain Loaf 650g",       "B", "Bread",        "ea", 8.0,  3.20, 1.45, 190, 3,  2,  "SUP-BAK-OFF", "all_year",  0.000,  0.010, 1.25, 2),
    ("Sourdough Loaf 700g",        "B", "Bread",        "ea", 6.0,  6.50, 3.40, 130, 4,  2,  "SUP-ADL-SOUR","all_year",  0.000,  0.010, 1.20, 2),
    ("Turkish Bread",              "B", "Bread",        "ea", 6.0,  4.50, 2.10, 95,  4,  3,  "SUP-ADL-SOUR","all_year",  0.000,  0.010, 1.20, 2),
    ("Bakehouse Rolls 6pk",        "B", "Bread",        "ea", 8.0,  3.90, 1.85, 155, 3,  2,  "SUP-BAK-OFF", "all_year",  0.000,  0.005, 1.25, 3),
    ("Hamburger Buns 6pk",         "B", "Bread",        "ea", 8.0,  3.60, 1.70, 140, 4,  3,  "SUP-BAK-OFF", "summer",    0.010,  0.005, 1.20, 2),
    ("Croissants 4pk",             "B", "Pastry",       "ea", 6.0,  5.50, 2.60, 105, 3,  2,  "SUP-BAK-OFF", "all_year", -0.005,  0.005, 1.30, 3),
    ("Apple Turnovers 4pk",        "B", "Pastry",       "ea", 6.0,  5.20, 2.40, 70,  3,  2,  "SUP-BAK-OFF", "autumn",   -0.005,  0.005, 1.25, 2),
    ("Cinnamon Donuts 6pk",        "B", "Pastry",       "ea", 6.0,  4.80, 2.10, 115, 3,  2,  "SUP-BAK-OFF", "all_year", -0.005,  0.005, 1.30, 4),
    ("Vanilla Slice 2pk",          "B", "Cake",         "ea", 6.0,  5.90, 2.75, 90,  4,  3,  "SUP-BAK-OFF", "all_year", -0.005,  0.005, 1.25, 3),
    ("Lamingtons 6pk",             "B", "Cake",         "ea", 4.0,  6.50, 3.10, 55,  5,  3,  "SUP-BAK-OFF", "all_year", -0.005,  0.005, 1.20, 2),
    ("Chocolate Mud Cake Slice",   "B", "Cake",         "ea", 6.0,  6.20, 2.95, 65,  5,  3,  "SUP-BAK-OFF", "all_year", -0.005,  0.005, 1.15, 1),
    ("Hot Cross Buns 6pk",         "B", "Seasonal",     "ea", 8.0,  5.50, 2.40, 180, 4,  3,  "SUP-BAK-OFF", "spring",    0.000,  0.005, 1.30, 2),
    ("Fruit Buns 6pk",             "B", "Bread",        "ea", 6.0,  4.90, 2.20, 60,  4,  3,  "SUP-BAK-OFF", "winter",    0.000,  0.005, 1.20, 1),
    ("Baguette",                   "B", "Bread",        "ea", 6.0,  3.80, 1.70, 120, 2,  2,  "SUP-ADL-SOUR","all_year",  0.000,  0.010, 1.35, 2),
    ("Pita Bread 6pk",             "B", "Bread",        "ea", 8.0,  3.40, 1.55, 135, 10, 7,  "SUP-BAK-OFF", "all_year",  0.000,  0.000, 1.10, 2),
    ("Gluten Free Loaf 500g",      "B", "Bread",        "ea", 6.0,  7.50, 3.90, 45,  12, 9,  "SUP-BAK-OFF", "all_year",  0.000,  0.000, 1.05, 1),

    # ---- Deli --------------------------------------------------------------
    ("Shaved Leg Ham",             "D", "Smallgoods",   "kg", 1.0,  18.90, 11.20, 58, 10, 7,  "SUP-BAR-SML", "christmas", 0.000,  0.000, 1.05, 3),
    ("Shaved Chicken Breast",      "D", "Smallgoods",   "kg", 1.0,  19.50, 12.10, 52,  5,  3,  "SUP-BAR-SML", "all_year",  0.000,  0.000, 1.05, 2),
    ("Hungarian Salami",           "D", "Smallgoods",   "kg", 1.0,  24.90, 15.40, 26, 30, 21, "SUP-BAR-SML", "christmas", 0.000,  0.000, 0.95, 2),
    ("Prosciutto",                 "D", "Smallgoods",   "kg", 0.5,  34.90, 22.50, 18, 21, 14, "SUP-BAR-SML", "christmas", 0.000,  0.000, 1.00, 2),
    ("Kabana",                     "D", "Smallgoods",   "kg", 1.0,  16.90, 9.80,  22, 28, 20, "SUP-BAR-SML", "all_year",  0.000,  0.000, 0.95, 1),
    ("Rotisserie Chicken",         "D", "Hot food",     "ea", 6.0,  12.50, 6.40,  150, 1,  1,  "SUP-BAR-SML", "all_year",  0.000,  0.000, 1.25, 3),
    ("Hummus 200g",                "D", "Dips",         "ea", 12.0, 4.50, 2.20,  125, 16, 12, "SUP-ADL-DIP", "all_year",  0.010,  0.000, 1.15, 3),
    ("Tzatziki 200g",              "D", "Dips",         "ea", 12.0, 4.60, 2.30,  95,  16, 12, "SUP-ADL-DIP", "summer",    0.010,  0.000, 1.15, 3),
    ("Beetroot Dip 200g",          "D", "Dips",         "ea", 12.0, 4.60, 2.30,  60,  16, 12, "SUP-ADL-DIP", "all_year",  0.005,  0.000, 1.10, 2),
    ("Dolomides 200g",             "D", "Deli",         "ea", 12.0, 5.20, 2.80,  70,  21, 15, "SUP-ADL-DIP", "all_year",  0.005,  0.000, 1.05, 2),
    ("Marinated Olives 200g",      "D", "Deli",         "ea", 12.0, 5.90, 3.20,  85,  30, 21, "SUP-ADL-DIP", "all_year",  0.005,  0.000, 1.05, 2),
    ("Potato Salad 400g",          "D", "Salads",       "ea", 8.0,  5.50, 2.70,  110, 5,  3,  "SUP-ADL-DIP", "summer",    0.020,  0.000, 1.20, 3),
    ("Coleslaw 400g",              "D", "Salads",       "ea", 8.0,  5.20, 2.50,  105, 5,  3,  "SUP-ADL-DIP", "summer",    0.020,  0.000, 1.20, 3),
    ("Greek Salad 350g",           "D", "Salads",       "ea", 8.0,  6.90, 3.60,  90,  4,  2,  "SUP-ADL-DIP", "summer",    0.020,  0.000, 1.25, 2),
    ("Pasta Salad 400g",           "D", "Salads",       "ea", 8.0,  5.80, 2.90,  85,  5,  3,  "SUP-ADL-DIP", "summer",    0.020,  0.000, 1.20, 2),
    ("Cheerios Cocktail",          "D", "Smallgoods",   "kg", 1.0,  14.90, 8.20,  24,  12, 8,  "SUP-BAR-SML", "all_year",  0.000,  0.000, 1.00, 1),
    ("Feta Marinated 200g",        "D", "Cheese",       "ea", 12.0, 6.90, 3.80,  75,  18, 12, "SUP-BAR-CHS", "summer",    0.010,  0.000, 1.10, 2),

    # ---- Dairy -------------------------------------------------------------
    ("Full Cream Milk 2L",         "Y", "Milk",         "ea", 12.0, 3.10, 2.05,  420, 12, 9,  "SUP-FLE-MLK", "all_year",  0.010,  0.000, 0.95, 3),
    ("Lite Milk 2L",               "Y", "Milk",         "ea", 12.0, 3.10, 2.05,  310, 12, 9,  "SUP-FLE-MLK", "all_year",  0.010,  0.000, 0.95, 3),
    ("Full Cream Milk 3L",         "Y", "Milk",         "ea", 6.0,  4.50, 3.05,  240, 12, 9,  "SUP-FLE-MLK", "all_year",  0.010,  0.000, 0.95, 3),
    ("Skim Milk 2L",               "Y", "Milk",         "ea", 12.0, 3.10, 2.05,  130, 12, 9,  "SUP-FLE-MLK", "all_year",  0.010,  0.000, 0.95, 2),
    ("Greek Yoghurt 1kg",          "Y", "Yoghurt",      "ea", 8.0,  6.20, 3.60,  185, 21, 15, "SUP-FLE-MLK", "all_year",  0.005,  0.000, 0.95, 3),
    ("Natural Yoghurt 1kg",        "Y", "Yoghurt",      "ea", 8.0,  5.60, 3.20,  140, 21, 15, "SUP-FLE-MLK", "all_year",  0.005,  0.000, 0.95, 2),
    ("Vanilla Yoghurt 1kg",        "Y", "Yoghurt",      "ea", 8.0,  5.80, 3.30,  120, 21, 15, "SUP-FLE-MLK", "all_year",  0.005,  0.000, 0.95, 2),
    ("Kids Yoghurt Pouch 6pk",     "Y", "Yoghurt",      "ea", 6.0,  5.50, 3.10,  165, 18, 12, "SUP-FLE-MLK", "all_year",  0.005,  0.000, 0.95, 3),
    ("Thickened Cream 600ml",      "Y", "Cream",        "ea", 12.0, 3.90, 2.35,  175, 14, 10, "SUP-FLE-MLK", "christmas", 0.005,  0.000, 1.00, 3),
    ("Sour Cream 300ml",           "Y", "Cream",        "ea", 12.0, 3.20, 1.85,  105, 16, 12, "SUP-FLE-MLK", "all_year",  0.005,  0.000, 1.00, 2),
    ("Custard 1L",                 "Y", "Cream",        "ea", 8.0,  4.20, 2.40,  70,  16, 12, "SUP-FLE-MLK", "winter",    0.000,  0.000, 1.00, 2),
    ("Tasty Cheese Block 500g",    "Y", "Cheese",       "ea", 8.0,  7.90, 4.90,  230, 45, 30, "SUP-BAR-CHS", "all_year",  0.000,  0.000, 0.90, 3),
    ("Colby Cheese Block 500g",    "Y", "Cheese",       "ea", 8.0,  7.90, 4.90,  120, 45, 30, "SUP-BAR-CHS", "all_year",  0.000,  0.000, 0.90, 2),
    ("Shredded Mozzarella 250g",   "Y", "Cheese",       "ea", 12.0, 5.50, 3.30,  165, 30, 21, "SUP-BAR-CHS", "all_year",  0.000,  0.000, 0.90, 3),
    ("Parmesan Wedge 200g",        "Y", "Cheese",       "ea", 12.0, 8.90, 5.60,  60,  60, 40, "SUP-BAR-CHS", "all_year",  0.000,  0.000, 0.85, 1),
    ("Feta Block 200g",            "Y", "Cheese",       "ea", 12.0, 6.50, 3.90,  95,  30, 21, "SUP-BAR-CHS", "summer",    0.005,  0.000, 0.95, 2),
    ("Haloumi 180g",               "Y", "Cheese",       "ea", 12.0, 7.20, 4.40,  80,  35, 25, "SUP-BAR-CHS", "summer",    0.005,  0.000, 0.95, 2),
    ("Salted Butter 250g",         "Y", "Butter",       "ea", 12.0, 5.90, 3.85,  260, 60, 45, "SUP-FLE-MLK", "all_year",  0.000,  0.000, 0.85, 3),
    ("Free Range Eggs 12pk",       "Y", "Eggs",         "ea", 12.0, 7.50, 4.80,  330, 28, 21, "SUP-FLE-MLK", "all_year",  0.005,  0.000, 0.95, 3),
    ("Free Range Eggs 6pk",        "Y", "Eggs",         "ea", 12.0, 4.30, 2.75,  170, 28, 21, "SUP-FLE-MLK", "all_year",  0.005,  0.000, 0.95, 2),

    # ---- Seafood -----------------------------------------------------------
    ("Cooked Prawns Medium",       "S", "Prawns",       "kg", 1.0, 32.90, 22.40, 26,  3,  2,  "SUP-PTL-SEA", "christmas", 0.010,  0.000, 1.05, 3),
    ("Green Prawns Medium",        "S", "Prawns",       "kg", 1.0, 28.90, 19.20, 22,  3,  2,  "SUP-PTL-SEA", "christmas", 0.010,  0.000, 1.05, 2),
    ("Atlantic Salmon Fillets",    "S", "Fish",         "kg", 1.0, 34.90, 24.50, 34,  3,  2,  "SUP-PTL-SEA", "all_year",  0.005,  0.000, 1.05, 3),
    ("Barramundi Fillets",         "S", "Fish",         "kg", 1.0, 32.90, 22.80, 24,  3,  2,  "SUP-PTL-SEA", "all_year",  0.005,  0.000, 1.05, 2),
    ("Snapper Whole",              "S", "Fish",         "kg", 1.0, 21.90, 14.20, 18,  3,  2,  "SUP-ADL-FSH", "summer",   0.005,  0.000, 1.00, 2),
    ("Flathead Fillets",           "S", "Fish",         "kg", 1.0, 38.90, 27.60, 14,  3,  2,  "SUP-ADL-FSH", "all_year",  0.005,  0.000, 1.00, 1),
    ("King George Whiting",        "S", "Fish",         "kg", 1.0, 46.90, 34.20, 12,  3,  2,  "SUP-ADL-FSH", "autumn",    0.005,  0.000, 0.95, 1),
    ("Squid Tubes",                "S", "Squid",        "kg", 1.0, 24.90, 16.40, 16,  3,  2,  "SUP-PTL-SEA", "summer",    0.010,  0.000, 1.05, 2),
    ("Pacific Oysters Natural",    "S", "Oysters",      "ea", 12.0, 18.90, 12.60, 32,  5,  3,  "SUP-KI-OYS",  "christmas", 0.005,  0.000, 1.00, 2),
    ("Smoked Salmon 200g",         "S", "Smoked",       "ea", 12.0, 14.50, 9.40,  85,  21, 15, "SUP-PTL-SEA", "christmas", 0.005,  0.000, 1.00, 2),
    ("Smoked Salmon 100g",         "S", "Smoked",       "ea", 12.0, 8.50,  5.30,  105, 21, 15, "SUP-PTL-SEA", "all_year",  0.005,  0.000, 1.00, 2),
    ("Mussels 1kg",                "S", "Shellfish",    "ea", 6.0,  12.90, 7.90,  30,  4,  2,  "SUP-PTL-SEA", "winter",    0.005,  0.000, 1.05, 2),
    ("Fish Fillets Crumbed 400g",  "S", "Fish",         "ea", 12.0, 11.50, 7.20,  95,  7,  5,  "SUP-ADL-FSH", "all_year",  0.000,  0.000, 1.05, 3),
    ("Hot Smoked Trout 250g",      "S", "Smoked",       "ea", 12.0, 15.90, 10.40, 38,  14, 10, "SUP-PTL-SEA", "all_year",  0.000,  0.000, 0.95, 1),
    ("Calamari Rings 500g",        "S", "Squid",        "ea", 12.0, 13.90, 8.60,  70,  7,  5,  "SUP-PTL-SEA", "summer",    0.010,  0.000, 1.05, 2),
]
# fmt: on


@dataclass(frozen=True)
class Store:
    code: str
    name: str
    suburb: str
    postcode: int
    latitude: float
    longitude: float
    format: str
    size_index: float
    opened: date
    scan_compliance: float
    markdown_discipline: float
    competitor_pressure: float

    @property
    def region(self) -> str:
        if self.format == "Express":
            return "Regional South Australia"
        return "Adelaide metropolitan"


@dataclass(frozen=True)
class Supplier:
    code: str
    name: str
    type: str
    region: str
    lead_time_days: int
    delivery_weekdays: tuple[int, ...]
    min_order_value: float
    reliability: float

    def delivers_on(self, weekday: int) -> bool:
        return weekday in self.delivery_weekdays


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    department: str
    category: str
    unit: str
    pack_size: float
    retail_price: float
    cost_price: float
    weekly_units: float
    shelf_life_days: int
    life_at_delivery_days: int
    supplier_code: str
    season_profile: str
    heat_sensitivity: float
    rain_sensitivity: float
    markdown_responsiveness: float
    promos_per_year: int

    @property
    def department_name(self) -> str:
        return DEPARTMENTS[self.department]

    @property
    def is_weighed(self) -> bool:
        return self.unit == "kg"

    @property
    def is_short_life(self) -> bool:
        return self.shelf_life_days <= 21

    @property
    def gross_margin_rate(self) -> float:
        return (self.retail_price - self.cost_price) / self.retail_price

    @property
    def is_markdown_eligible(self) -> bool:
        """Fresh departments mark down anything perishable except hot food.

        Hot food (rotisserie chicken) is a same-day disposal with food-safety
        constraints — it is disposed, never re-priced.
        """
        return self.category != "Hot food"

    def season_factor(self, month: int) -> float:
        return SEASON_PROFILES[self.season_profile][month - 1]


@dataclass(frozen=True)
class StoreItem:
    """Assortment: not every store carries every line (and range differs by size)."""

    store_code: str
    sku: str
    variety_index: float  # local demand index for this item at this store
    delivery_weekdays: tuple[int, ...]
    is_ranging: bool

    def delivers_on(self, weekday: int) -> bool:
        return weekday in self.delivery_weekdays


@dataclass(frozen=True)
class Masters:
    stores: list[Store]
    suppliers: list[Supplier]
    items: list[Item]
    #: (store_code, sku) -> StoreItem for every ranged line at every store
    assortment: dict[tuple[str, str], StoreItem] = field(default_factory=dict)

    @property
    def store_by_code(self) -> dict[str, Store]:
        return {s.code: s for s in self.stores}

    @property
    def item_by_sku(self) -> dict[str, Item]:
        return {i.sku: i for i in self.items}

    @property
    def supplier_by_code(self) -> dict[str, Supplier]:
        return {s.code: s for s in self.suppliers}

    def items_for_store(self, store_code: str) -> list[Item]:
        index = self.item_by_sku
        return [
            index[sku]
            for (code, sku), si in self.assortment.items()
            if code == store_code and si.is_ranging
        ]


def stores() -> list[Store]:
    return [
        Store(
            code=row[0],
            name=row[1],
            suburb=row[2],
            postcode=row[3],
            latitude=row[4],
            longitude=row[5],
            format=row[6],
            size_index=row[7],
            opened=date.fromisoformat(row[8]),
            scan_compliance=row[9],
            markdown_discipline=row[10],
            competitor_pressure=row[11],
        )
        for row in STORE_ROWS
    ]


def suppliers() -> list[Supplier]:
    return [
        Supplier(
            code=row[0],
            name=row[1],
            type=row[2],
            region=row[3],
            lead_time_days=row[4],
            delivery_weekdays=tuple(row[5]),
            min_order_value=row[6],
            reliability=row[7],
        )
        for row in SUPPLIER_ROWS
    ]


def _sku_for(department: str, index_in_department: int) -> str:
    return f"{department}{index_in_department:04d}"


def items() -> list[Item]:
    seen: dict[str, int] = {}
    out: list[Item] = []
    for row in ITEM_ROWS:
        dept = row[1]
        seen[dept] = seen.get(dept, 0) + 1
        out.append(
            Item(
                sku=_sku_for(dept, seen[dept]),
                name=row[0],
                department=dept,
                category=row[2],
                unit=row[3],
                pack_size=row[4],
                retail_price=row[5],
                cost_price=row[6],
                weekly_units=row[7],
                shelf_life_days=row[8],
                life_at_delivery_days=row[9],
                supplier_code=row[10],
                season_profile=row[11],
                heat_sensitivity=row[12],
                rain_sensitivity=row[13],
                markdown_responsiveness=row[14],
                promos_per_year=row[15],
            )
        )
    return out


def select_stores(all_stores: list[Store], selector: str | int | list[str] | None) -> list[Store]:
    """``stores: all`` (or None) keeps the estate; an int takes the first N
    (largest first, so a small world is still a representative one)."""
    if selector is None or selector == "all":
        return list(all_stores)
    if isinstance(selector, int):
        return sorted(all_stores, key=lambda s: -s.size_index)[:selector]
    wanted = {selector} if isinstance(selector, str) else set(selector)
    return [s for s in all_stores if s.code in wanted]


def _stable_unit(store_code: str, sku: str) -> float:
    """A per-(store, line) index in [0, 1) that does not move between runs.

    ``hash()`` is salted per process, so using it here would give every store a
    different local demand index on every run — the simulation would stop being
    reproducible, which is the one property the whole project rests on.
    """
    return (crc32(f"{store_code}:{sku}".encode()) % 10_000) / 10_000.0


def delivery_schedule(
    store: Store, item: Item, suppliers_by_code: dict[str, Supplier]
) -> tuple[int, ...]:
    """A store's delivery days for a line.

    The supplier's schedule, minus Sunday for everything except the bake-off
    (which is baked on site), and consolidated to three drops a week at the
    Express-format stores, which take a part load rather than a full one.
    """
    days = list(suppliers_by_code[item.supplier_code].delivery_weekdays)
    if item.supplier_code != "SUP-BAK-OFF" and 7 in days:
        days = [d for d in days if d != 7]
    if store.format == "Express" and len(days) > 3:
        days = [d for d in days if d in (1, 3, 5)] or days[:3]
    return tuple(sorted(days))


def assortment_for(
    store: Store, all_items: list[Item], suppliers_by_code: dict[str, Supplier]
) -> list[StoreItem]:
    """Which lines a store ranges, and how strongly they sell there.

    Range is not uniform: an Express store in Whyalla does not carry a
    $46/kg whole fish or the long-life long-tail, and the coastal stores sell
    far more seafood than the hills stores do. Modelling that matters because
    range differences *are* the story in a fresh department — the waste comes
    from the lines a store ranges but cannot turn over.
    """
    out: list[StoreItem] = []
    for item in all_items:
        if store.format == "Express":
            # Small stores drop the slow long-life long tail and the premium
            # seafood lines that do not turn over outside a metro store.
            if item.weekly_units < 40 and item.shelf_life_days >= 21:
                continue
            if item.department == "S" and item.weekly_units < 40:
                continue
        if item.department == "S" and store.size_index < 0.75 and item.cost_price > 30:
            continue

        variety = 0.78 + 0.44 * _stable_unit(store.code, item.sku)
        if store.code in {"S02", "S07", "S08"} and item.department == "S":
            variety *= 1.25  # coastal stores
        if store.code in {"S01", "S04"} and item.department in {"D", "B"}:
            variety *= 1.12  # the two food-led metro stores
        if store.code in {"S05", "S06"} and item.department == "P":
            variety *= 1.08  # hills stores cook at home more

        out.append(
            StoreItem(
                store_code=store.code,
                sku=item.sku,
                variety_index=round(variety, 4),
                delivery_weekdays=delivery_schedule(store, item, suppliers_by_code),
                is_ranging=True,
            )
        )
    return out


def build_masters(cfg: Config) -> Masters:
    """The estate for this run, honouring the ``world.stores`` scale knob."""
    all_items = items()
    chosen = select_stores(stores(), cfg.stores)
    suppliers_by_code = {s.code: s for s in suppliers()}
    assortment: dict[tuple[str, str], StoreItem] = {}
    for store in chosen:
        for si in assortment_for(store, all_items, suppliers_by_code):
            assortment[(store.code, si.sku)] = si
    return Masters(
        stores=chosen,
        suppliers=list(suppliers_by_code.values()),
        items=all_items,
        assortment=assortment,
    )
