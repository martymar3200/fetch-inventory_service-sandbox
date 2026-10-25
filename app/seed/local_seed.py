"""
Wipes the LOCAL inventory_service database and seeds it in one of two modes.

    blank - required system values + one System Admin user
    demo  - blank + business lists, a demo facility, ~1,300 trays and
            ~15,000 items, all shelved with status "In"

Deterministic: a fixed random seed produces the same data on every run.
Refuses to run unless APP_ENVIRONMENT is local or debug.

Usage (inside the inventory-api container, from /code):
    python -m app.seed.local_seed blank|demo [--yes]

Normally called through `./helper.sh seed blank|demo`, which also restarts
the API so it reconnects to the recreated database.
"""

import random
import sys
from datetime import datetime, timedelta, timezone

import psycopg2

from app.config.config import get_settings
from app.seed.wipe_db import _parse_dsn


ADMIN_EMAIL = "admin@example.com"
ADMIN_FIRST_NAME = "System"
ADMIN_LAST_NAME = "Admin"

RANDOM_SEED = 2026
# Fixed reference date so generated accession/shelved dates are stable.
BASE_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)

TARGET_TRAY_ITEMS = 14_700
TARGET_NON_TRAY_ITEMS = 300

# --- Required system values (both modes) ---

CONTAINER_TYPES = ["Tray", "Non-Tray"]

BARCODE_TYPES = [
    ("Item", r"^\d{10}[0-9A]$"),
    ("Tray", r"^[A-Z]{2}\d{5,6}$"),
    ("Shelf", r"^\d{5,6}$"),
]

SIDE_ORIENTATIONS = ["Left", "Right"]

OWNER_TIERS = [(1, "Organization"), (2, "Department"), (3, "Division"), (4, "Individual")]

SYSTEM_SETTINGS = [
    (
        "shelf_position_auto_assign_direction",
        "low_to_high",
        "Direction for auto-assigning shelf positions during shelving. Options: low_to_high, high_to_low",
    ),
    (
        "allow_child_owner_shelving",
        "false",
        "Allow child owner items to be shelved on parent owner shelves",
    ),
]

# --- Business lists (demo mode) ---

REQUEST_TYPES = ["General Delivery", "ILL (InterLibrary Loan)", "Scan and Deliver"]
PRIORITIES = ["Low", "Medium", "High"]
MEDIA_TYPES = ["Book/Volume", "Archival Box", "Microfilm", "Flat File"]

# short_name: (name, height, width, is_tray, typical items per tray)
# Inches, depth 18 for all. DS/RS dimensions are estimates.
SIZE_CLASSES = {
    "AL": ("AL - Paperbacks (low)", 6.25, 5.75, True, 18),
    "AH": ("AH - Paperbacks", 8.25, 5.75, True, 18),
    "BL": ("BL - Short hardcovers, trade books", 8.25, 6.75, True, 15),
    "BH": ("BH - Academic monographs (octavo)", 10.25, 6.75, True, 15),
    "CL": ("CL - Wide monographs, journals", 10.25, 8.00, True, 12),
    "CH": ("CH - Tall journals, small folios", 12.25, 8.00, True, 12),
    "DL": ("DL - Wide journals, quarto", 12.25, 9.75, True, 10),
    "DH": ("DH - Bound periodicals, oversize", 14.25, 9.75, True, 10),
    "EL": ("EL - Art books, sheet music, atlases", 14.25, 12.25, True, 7),
    "EH": ("EH - Extra-tall folios, registers", 16.25, 12.25, True, 7),
    "DS": ("DS - Document Storage Box", 10.00, 5.00, False, 0),
    "RS": ("RS - Record Storage Box", 10.00, 12.00, False, 0),
}
SIZE_CLASS_DEPTH = 18

# Max containers per shelf by shelf width, from the facility data in
# fixtures/types/client_shelf_types.json (DS uses DS1).
SHELF_CAPACITY = {
    "40": {"AL": 12, "AH": 12, "BL": 10, "BH": 10, "CL": 8, "CH": 8,
           "DL": 9, "DH": 9, "EL": 9, "EH": 9, "DS": 16, "RS": 6},
    "53": {"AL": 16, "AH": 16, "BL": 14, "BH": 14, "CL": 12, "CH": 12,
           "DL": 15, "DH": 15, "EL": 12, "EH": 12, "DS": 23, "RS": 8},
}

# (name, tier level, parent name)
OWNERS = [
    ("Demo Library", 1, None),
    ("Demo Archive", 1, None),
    ("Demo Library - General Collections", 2, "Demo Library"),
    ("Demo Library - Special Collections", 2, "Demo Library"),
    ("Demo Archive - Manuscripts", 2, "Demo Archive"),
    ("Demo Archive - Photographs", 2, "Demo Archive"),
    ("General Collections - Serials", 3, "Demo Library - General Collections"),
]

# (name, address, owners served)
DELIVERY_LOCATIONS = [
    ("Main Reading Room", "1 Demo Way, Washington, DC 20001",
     ["Demo Library", "Demo Library - General Collections", "General Collections - Serials"]),
    ("Special Collections Reading Room", "1 Demo Way, Room 200, Washington, DC 20001",
     ["Demo Library", "Demo Library - Special Collections"]),
    ("Archive Reading Room", "2 Demo Way, Washington, DC 20001",
     ["Demo Archive", "Demo Archive - Manuscripts", "Demo Archive - Photographs"]),
]

BUILDING_NAME = "Demo Facility"
MODULES = ["1", "2"]
AISLES_PER_MODULE = 3
LADDERS_PER_SIDE = 10
SHELVES_PER_LADDER = 6
FIRST_SHELF_BARCODE = 100001

# Ladders cycle through these, so ~1 in 6 ladders hold non-tray boxes.
LADDER_SIZE_CYCLE = ["AL", "AH", "BL", "BH", "DS", "CL", "CH", "DL", "DH", "RS", "EL", "EH"]
# Share of positions filled on a shelf chosen to hold stock.
SHELF_FILL_RATE = 0.9


def _guard_environment():
    env = get_settings().APP_ENVIRONMENT
    if env not in ("local", "debug"):
        print(f"Refusing to run: APP_ENVIRONMENT is '{env}' (must be local or debug).", file=sys.stderr)
        sys.exit(1)


def _confirm(mode):
    print(f"This will DELETE ALL DATA in the local inventory database and seed '{mode}'.")
    answer = input("Type 'yes' to continue: ").strip().lower()
    if answer != "yes":
        print("Cancelled.")
        sys.exit(1)


def _recreate_database():
    dsn = _parse_dsn(get_settings().DATABASE_URL)
    db_name = dsn.pop("_dbname") or "inventory_service"
    conn = psycopg2.connect(**dict(dsn, dbname="postgres"))
    conn.autocommit = True  # DDL outside a transaction
    try:
        cur = conn.cursor()
        # WITH (FORCE) drops the API's open connections (pg14+).
        cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        conn.close()
    print(f"Recreated database: {db_name}")


def _migrate():
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    print("Migrations complete.")


def _seed_system_values(session):
    from app.models.barcode_types import BarcodeType
    from app.models.container_types import ContainerType
    from app.models.owner_tiers import OwnerTier
    from app.models.side_orientations import SideOrientation
    from app.models.system_settings import SystemSetting

    session.add_all([ContainerType(type=t) for t in CONTAINER_TYPES])
    session.add_all([BarcodeType(name=n, allowed_pattern=p) for n, p in BARCODE_TYPES])
    session.add_all([SideOrientation(name=n) for n in SIDE_ORIENTATIONS])
    session.add_all([OwnerTier(level=lvl, name=n) for lvl, n in OWNER_TIERS])
    session.add_all([SystemSetting(key=k, value=v, description=d) for k, v, d in SYSTEM_SETTINGS])
    session.commit()
    print("Seeded required system values.")


def _seed_business_lists(session):
    from app.models.media_types import MediaType
    from app.models.priorities import Priority
    from app.models.request_types import RequestType
    from app.models.shelf_types import ShelfType
    from app.models.size_class import SizeClass

    session.add_all([RequestType(type=t) for t in REQUEST_TYPES])
    session.add_all([Priority(value=v) for v in PRIORITIES])
    session.add_all([MediaType(name=n) for n in MEDIA_TYPES])

    size_classes = {}
    for short, (name, height, width, _, _) in SIZE_CLASSES.items():
        size_classes[short] = SizeClass(
            name=name, short_name=short, height=height, width=width, depth=SIZE_CLASS_DEPTH
        )
    session.add_all(size_classes.values())
    session.flush()

    for shelf_width, capacities in SHELF_CAPACITY.items():
        session.add_all([
            ShelfType(type=shelf_width, size_class_id=size_classes[short].id, max_capacity=cap)
            for short, cap in capacities.items()
        ])
    session.commit()
    print("Seeded request types, priorities, media types, size classes and shelf types.")


def _seed_owners_and_delivery(session):
    from app.models.delivery_locations import DeliveryLocation
    from app.models.owner_delivery_locations import OwnerDeliveryLocation
    from app.models.owner_tiers import OwnerTier
    from app.models.owners import Owner

    tiers = {t.level: t.id for t in session.query(OwnerTier).all()}
    owners = {}
    for name, level, parent in OWNERS:
        owner = Owner(
            name=name,
            owner_tier_id=tiers[level],
            parent_owner_id=owners[parent].id if parent else None,
        )
        session.add(owner)
        session.flush()
        owners[name] = owner

    for name, address, served in DELIVERY_LOCATIONS:
        location = DeliveryLocation(name=name, address=address)
        session.add(location)
        session.flush()
        session.add_all([
            OwnerDeliveryLocation(owner_id=owners[o].id, delivery_location_id=location.id)
            for o in served
        ])
    session.commit()
    print(f"Seeded {len(owners)} owners and {len(DELIVERY_LOCATIONS)} delivery locations.")
    return owners


def _seed_locations(session, owners):
    """Builds the facility and returns its shelves with their positions."""
    from app.models.aisles import Aisle
    from app.models.barcode_types import BarcodeType
    from app.models.barcodes import Barcode
    from app.models.buildings import Building
    from app.models.container_types import ContainerType
    from app.models.ladders import Ladder
    from app.models.modules import Module
    from app.models.shelf_positions import ShelfPosition
    from app.models.shelf_types import ShelfType
    from app.models.shelves import Shelf
    from app.models.side_orientations import SideOrientation
    from app.models.sides import Side
    from app.models.size_class import SizeClass

    shelf_barcode_type = session.query(BarcodeType).filter_by(name="Shelf").one()
    container_types = {c.type: c.id for c in session.query(ContainerType).all()}
    orientations = {o.name: o.id for o in session.query(SideOrientation).all()}
    size_class_ids = {s.short_name: s.id for s in session.query(SizeClass).all()}
    shelf_types = {
        (st.type, st.size_class_id): st for st in session.query(ShelfType).all()
    }
    owner_cycle = [o.id for o in owners.values()]

    building = Building(name=BUILDING_NAME)
    session.add(building)
    session.flush()

    shelves = []
    ladder_index = 0
    side_index = 0
    for module_number in MODULES:
        module = Module(building_id=building.id, module_number=module_number)
        session.add(module)
        session.flush()
        for aisle_number in range(1, AISLES_PER_MODULE + 1):
            aisle = Aisle(module_id=module.id, aisle_number=aisle_number)
            session.add(aisle)
            session.flush()
            for orientation in SIDE_ORIENTATIONS:
                side = Side(aisle_id=aisle.id, side_orientation_id=orientations[orientation])
                session.add(side)
                session.flush()
                # One owner per side, cycling so every owner gets space.
                owner_id = owner_cycle[side_index % len(owner_cycle)]
                side_index += 1
                for ladder_number in range(1, LADDERS_PER_SIDE + 1):
                    ladder = Ladder(side_id=side.id, ladder_number=ladder_number)
                    session.add(ladder)
                    session.flush()
                    # One size class and shelf width per ladder.
                    short = LADDER_SIZE_CYCLE[ladder_index % len(LADDER_SIZE_CYCLE)]
                    shelf_width = "53" if ladder_index % 2 == 0 else "40"
                    ladder_index += 1
                    shelf_type = shelf_types[(shelf_width, size_class_ids[short])]
                    is_tray = SIZE_CLASSES[short][3]
                    for shelf_number in range(1, SHELVES_PER_LADDER + 1):
                        barcode = Barcode(
                            value=str(FIRST_SHELF_BARCODE + len(shelves)),
                            type_id=shelf_barcode_type.id,
                        )
                        session.add(barcode)
                        session.flush()
                        shelf = Shelf(
                            barcode_id=barcode.id,
                            ladder_id=ladder.id,
                            shelf_number=shelf_number,
                            shelf_type_id=shelf_type.id,
                            container_type_id=container_types["Tray" if is_tray else "Non-Tray"],
                            owner_id=owner_id,
                            height=SIZE_CLASSES[short][1],
                            width=int(shelf_width),
                            depth=SIZE_CLASS_DEPTH,
                            available_space=shelf_type.max_capacity,
                        )
                        shelves.append((shelf, short, is_tray, shelf_type.max_capacity))
                    session.add_all([s[0] for s in shelves[-SHELVES_PER_LADDER:]])
    session.flush()

    positions = {}
    for shelf, _, _, capacity in shelves:
        shelf_positions = [
            ShelfPosition(shelf_id=shelf.id, position_number=n) for n in range(1, capacity + 1)
        ]
        session.add_all(shelf_positions)
        positions[shelf.id] = shelf_positions
    session.commit()

    print(
        f"Seeded 1 building, {len(MODULES)} modules, {ladder_index} ladders, "
        f"{len(shelves)} shelves, {sum(len(p) for p in positions.values())} shelf positions."
    )
    return shelves, positions


def _random_dates(rng):
    accession_dt = BASE_DATE - timedelta(days=rng.randint(30, 730))
    shelved_dt = accession_dt + timedelta(days=rng.randint(1, 14))
    return accession_dt, shelved_dt


def _seed_inventory(session, rng, shelves, positions):
    from app.models.barcode_types import BarcodeType
    from app.models.barcodes import Barcode
    from app.models.container_types import ContainerType
    from app.models.items import Item, ItemStatus
    from app.models.media_types import MediaType
    from app.models.non_tray_items import NonTrayItem, NonTrayItemStatus
    from app.models.shelves import Shelf
    from app.models.size_class import SizeClass
    from app.models.trays import Tray

    barcode_types = {b.name: b.id for b in session.query(BarcodeType).all()}
    container_types = {c.type: c.id for c in session.query(ContainerType).all()}
    media_types = {m.name: m.id for m in session.query(MediaType).all()}
    size_class_ids = {s.short_name: s.id for s in session.query(SizeClass).all()}

    def items_per_tray(short):
        typical = SIZE_CLASSES[short][4]
        return max(1, typical + rng.randint(-3, 3))

    def tray_media(short):
        if short in ("EL", "EH"):
            return rng.choice(["Book/Volume", "Book/Volume", "Flat File"])
        if short == "AL":
            return rng.choice(["Book/Volume", "Book/Volume", "Microfilm"])
        return "Book/Volume"

    # --- Trays and their items ---
    tray_slots = _pick_slots(rng, shelves, positions, True, TARGET_TRAY_ITEMS, items_per_tray)
    tray_counters = {}
    item_number = 0
    trays = []
    items = []
    for shelf, short, position, item_count in tray_slots:
        tray_counters[short] = tray_counters.get(short, 0) + 1
        tray_barcode = Barcode(value=f"{short}{tray_counters[short]:06d}", type_id=barcode_types["Tray"])
        accession_dt, shelved_dt = _random_dates(rng)
        media_type_id = media_types[tray_media(short)]
        tray = Tray(
            barcode=tray_barcode,
            container_type_id=container_types["Tray"],
            owner_id=shelf.owner_id,
            size_class_id=size_class_ids[short],
            media_type_id=media_type_id,
            shelf_position_id=position.id,
            shelf_position_proposed_id=position.id,
            accession_dt=accession_dt,
            shelved_dt=shelved_dt,
            scanned_for_accession=True,
            scanned_for_verification=True,
            scanned_for_shelving=True,
            collection_accessioned=True,
            collection_verified=True,
        )
        trays.append(tray)
        for _ in range(item_count):
            item_number += 1
            items.append(Item(
                barcode=Barcode(
                    value=f"30{item_number:08d}{item_number % 10}", type_id=barcode_types["Item"]
                ),
                tray=tray,
                status=ItemStatus.In,
                owner_id=shelf.owner_id,
                size_class_id=size_class_ids[short],
                container_type_id=container_types["Tray"],
                media_type_id=media_type_id,
                title=f"Demo Title {item_number:05d}",
                volume=f"v.{rng.randint(1, 12)}" if rng.random() < 0.3 else None,
                accession_dt=accession_dt,
                scanned_for_accession=True,
                scanned_for_verification=True,
            ))
    session.add_all(trays)
    session.add_all(items)
    session.flush()
    print(f"Seeded {len(trays)} trays holding {len(items)} items.")

    # --- Non-tray items (one per position) ---
    non_tray_slots = _pick_slots(rng, shelves, positions, False, TARGET_NON_TRAY_ITEMS, lambda _: 1)
    non_tray_items = []
    for number, (shelf, short, position, _) in enumerate(non_tray_slots, start=1):
        accession_dt, shelved_dt = _random_dates(rng)
        non_tray_items.append(NonTrayItem(
            barcode=Barcode(value=f"40{number:08d}{number % 10}", type_id=barcode_types["Item"]),
            status=NonTrayItemStatus.In,
            owner_id=shelf.owner_id,
            size_class_id=size_class_ids[short],
            container_type_id=container_types["Non-Tray"],
            media_type_id=media_types["Archival Box"],
            shelf_position_id=position.id,
            shelf_position_proposed_id=position.id,
            accession_dt=accession_dt,
            shelved_dt=shelved_dt,
            scanned_for_accession=True,
            scanned_for_verification=True,
            scanned_for_shelving=True,
        ))
    session.add_all(non_tray_items)
    session.flush()
    print(f"Seeded {len(non_tray_items)} non-tray items.")

    # --- Available space = capacity - occupied ---
    occupied = {}
    for shelf, _, _, _ in tray_slots + non_tray_slots:
        occupied[shelf.id] = occupied.get(shelf.id, 0) + 1
    for shelf, _, _, capacity in shelves:
        session.query(Shelf).filter_by(id=shelf.id).update(
            {"available_space": capacity - occupied.get(shelf.id, 0)}
        )
    session.commit()
    filled = sum(1 for s in shelves if s[0].id in occupied)
    print(f"Updated available space ({filled} of {len(shelves)} shelves hold stock).")


def _pick_slots(rng, shelves, positions, is_tray, target, units_per_position):
    """
    Picks shelves at random (so stock is spread across the facility) and
    fills ~90% of their positions until `target` units are placed.
    Returns (shelf, short_name, position, units) in location order, so
    barcodes run sequentially through the building.
    """
    candidates = [(i, s) for i, s in enumerate(shelves) if s[2] == is_tray]
    rng.shuffle(candidates)

    slots = []
    placed = 0
    for location_index, (shelf, short, _, _) in candidates:
        if placed >= target:
            break
        for position in positions[shelf.id]:
            if placed >= target:
                break
            if rng.random() < SHELF_FILL_RATE:
                units = units_per_position(short)
                slots.append((location_index, position.position_number, shelf, short, position, units))
                placed += units
    slots.sort(key=lambda s: (s[0], s[1]))
    return [s[2:] for s in slots]


def seed(mode):
    from app.seed.run_bootstrap_admin import _import_all_models, bootstrap_admin

    _recreate_database()
    _import_all_models()
    _migrate()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.seed.seed_permissions_adhoc import seed_new_permissions

    seed_new_permissions()
    # Batched INSERT..RETURNING ("insertmanyvalues") rejects the trays/items
    # id columns as declared in the models, so insert row by row.
    engine = create_engine(get_settings().DATABASE_URL, use_insertmanyvalues=False)
    session = Session(engine)
    try:
        _seed_system_values(session)
        bootstrap_admin(ADMIN_EMAIL, ADMIN_FIRST_NAME, ADMIN_LAST_NAME)
        if mode == "demo":
            rng = random.Random(RANDOM_SEED)
            _seed_business_lists(session)
            owners = _seed_owners_and_delivery(session)
            shelves, positions = _seed_locations(session, owners)
            _seed_inventory(session, rng, shelves, positions)
    finally:
        session.close()
        engine.dispose()
    print(f"Done. Seeded '{mode}'. Log in as {ADMIN_EMAIL}.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in ("blank", "demo"):
        print("Usage: python -m app.seed.local_seed blank|demo [--yes]", file=sys.stderr)
        sys.exit(1)

    _guard_environment()
    if "--yes" not in args:
        _confirm(args[0])
    seed(args[0])
