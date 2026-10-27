from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd
from faker import Faker

from src.utils.date_helpers import parse_yyyy_mm_dd, to_iso_dt

fake = Faker()


def _seed_for_day(run_date: date) -> None:
    """One seed per day: each day's data is different, and rerunning a day
    gives exactly the same files, so any day can be replayed."""
    seed = int(run_date.strftime("%Y%m%d"))
    Faker.seed(seed)
    random.seed(seed)
    np.random.seed(seed % (2**32))


@dataclass
class VolumeConfig:
    customers_per_day: Tuple[int, int] = (80, 140)
    products_total: Tuple[int, int] = (80, 140)
    orders_per_day: Tuple[int, int] = (300, 700)
    items_per_order: Tuple[int, int] = (1, 4)

    # Day 2 late update rates
    order_update_rate: float = 0.20
    payment_fix_rate: float = 0.40
    shipment_update_rate: float = 0.30
    new_shipments_rate: float = 0.15
    customer_update_rate: float = 0.05


ORDER_CHANNELS = ["WEB", "MOBILE", "MARKETPLACE"]
PAYMENT_METHODS = ["CARD", "PAYPAL", "APPLE_PAY"]
PAYMENT_PROVIDERS = ["STRIPE", "ADYEN", "PAYPAL_SIM"]
CARRIERS = ["UPS", "FEDEX", "USPS"]
CATEGORIES = ["Electronics", "Home", "Fashion", "Beauty", "Sports", "Grocery"]
BRANDS = ["Acme", "Nova", "Zenith", "Pioneer", "Orbit", "Vertex"]

ORDER_FLOW = ["PLACED", "CONFIRMED", "SHIPPED", "DELIVERED"]

# How far back a late change can reach (payment retries, status changes)
LATE_UPDATE_WINDOW_DAYS = 7


def _safe_email() -> str:
    return fake.user_name() + "@example.com"


def _rand_ts(run_date: date, min_hour: int = 0, max_hour: int = 23) -> str:
    hour = random.randint(min_hour, max_hour)
    minute = random.randint(0, 59)
    second = random.randint(0, 59)
    return to_iso_dt(run_date, hour, minute, second)


def _max_numeric_id(series: pd.Series, prefix: str) -> int:
    """
    Extract max numeric portion from IDs like 'C000123' or 'O0000123'.
    Returns 0 if empty.
    """
    if series is None or len(series) == 0:
        return 0
    nums = series.astype(str).str.replace(prefix, "", regex=False).astype(int)
    return int(nums.max()) if len(nums) else 0


def _next_status(cur: str) -> str:
    if cur not in ORDER_FLOW:
        return cur
    idx = ORDER_FLOW.index(cur)
    if idx >= len(ORDER_FLOW) - 1:
        return cur
    # Move forward by 1 step (realistic)
    return ORDER_FLOW[idx + 1]


def generate_products(run_date: date, cfg: VolumeConfig) -> pd.DataFrame:
    n = random.randint(*cfg.products_total)
    rows = []
    for i in range(1, n + 1):
        created = run_date - timedelta(days=random.randint(5, 60))
        updated = created + timedelta(days=random.randint(0, 10))
        if updated > run_date:
            updated = run_date
        base_price = round(random.uniform(5.0, 500.0), 2)
        rows.append(
            {
                "product_id": f"P{i:05d}",
                "created_at": to_iso_dt(created, 9, 0, 0),
                "updated_at": _rand_ts(updated),
                "product_name": f"{random.choice(BRANDS)} {random.choice(['Pro', 'Max', 'Lite', 'Plus'])} {random.choice(['Kit', 'Bundle', 'Item', 'Pack'])}",
                "category": random.choice(CATEGORIES),
                "brand": random.choice(BRANDS),
                "base_price": base_price,
                "active_flag": "Y",
            }
        )
    return pd.DataFrame(rows)


def generate_customers(run_date: date, cfg: VolumeConfig, start_id: int = 1) -> pd.DataFrame:
    n = random.randint(*cfg.customers_per_day)
    rows = []
    for i in range(start_id, start_id + n):
        created = run_date - timedelta(days=random.randint(0, 30))
        updated = created + timedelta(days=random.randint(0, 5))
        if updated > run_date:
            updated = run_date
        rows.append(
            {
                "customer_id": f"C{i:06d}",
                "created_at": to_iso_dt(created, 10, 0, 0),
                "updated_at": _rand_ts(updated),
                "first_name": fake.first_name(),
                "last_name": fake.last_name(),
                "email": _safe_email(),
                "phone": fake.msisdn()[:10],
                "city": fake.city(),
                "state": fake.state_abbr(),
                "country": "US",
                "customer_segment": random.choice(["New", "Regular", "VIP"]),
            }
        )
    return pd.DataFrame(rows)


def generate_orders_items_payments_shipments(
    run_date: date,
    cfg: VolumeConfig,
    customers: pd.DataFrame,
    products: pd.DataFrame,
    start_order_id: int = 1,
    start_order_item_id: int = 1,
    start_payment_id: int = 1,
    start_shipment_id: int = 1,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    n_orders = random.randint(*cfg.orders_per_day)

    customer_ids = customers["customer_id"].tolist()
    product_ids = products["product_id"].tolist()
    product_price_map: Dict[str, float] = dict(zip(products["product_id"], products["base_price"]))

    orders_rows: List[dict] = []
    items_rows: List[dict] = []
    payments_rows: List[dict] = []
    shipments_rows: List[dict] = []

    order_item_id = start_order_item_id
    payment_id = start_payment_id
    shipment_id = start_shipment_id

    for oid in range(start_order_id, start_order_id + n_orders):
        order_id = f"O{oid:07d}"
        cust_id = random.choice(customer_ids)
        created_at = _rand_ts(run_date, 0, 23)
        updated_at = created_at

        status = random.choices(
            population=["PLACED", "CONFIRMED", "SHIPPED", "DELIVERED"],
            weights=[0.35, 0.35, 0.20, 0.10],
            k=1,
        )[0]

        orders_rows.append(
            {
                "order_id": order_id,
                "created_at": created_at,
                "updated_at": updated_at,
                "customer_id": cust_id,
                "order_status": status,
                "order_channel": random.choice(ORDER_CHANNELS),
                "currency": "USD",
                "shipping_city": fake.city(),
                "shipping_state": fake.state_abbr(),
                "shipping_country": "US",
            }
        )

        # Items
        n_items = random.randint(*cfg.items_per_order)
        chosen_products = random.sample(product_ids, k=min(n_items, len(product_ids)))
        order_total = 0.0
        for pid in chosen_products:
            qty = random.randint(1, 3)
            unit_price = round(float(product_price_map[pid]) * random.uniform(0.90, 1.05), 2)
            line_amount = round(qty * unit_price, 2)
            order_total += line_amount
            items_rows.append(
                {
                    "order_item_id": f"OI{order_item_id:09d}",
                    "order_id": order_id,
                    "product_id": pid,
                    "quantity": qty,
                    "unit_price": unit_price,
                    "line_amount": line_amount,
                    "created_at": created_at,
                }
            )
            order_item_id += 1

        # Payment (one per order for v1)
        pay_status = random.choices(
            population=["CAPTURED", "FAILED", "AUTHORIZED"],
            weights=[0.85, 0.08, 0.07],
            k=1,
        )[0]
        payments_rows.append(
            {
                "payment_id": f"PAY{payment_id:09d}",
                "order_id": order_id,
                "created_at": created_at,
                "updated_at": created_at,
                "payment_status": pay_status,
                "payment_method": random.choice(PAYMENT_METHODS),
                "amount": round(order_total, 2),
                "currency": "USD",
                "provider": random.choice(PAYMENT_PROVIDERS),
            }
        )
        payment_id += 1

        # Shipment (only for SHIPPED/DELIVERED same day)
        if status in ["SHIPPED", "DELIVERED"]:
            shipped_at = created_at
            delivered_at = None
            ship_status = "IN_TRANSIT"
            if status == "DELIVERED":
                delivered_at = _rand_ts(run_date, 12, 23)
                ship_status = "DELIVERED"

            shipments_rows.append(
                {
                    "shipment_id": f"S{shipment_id:09d}",
                    "order_id": order_id,
                    "created_at": created_at,
                    "updated_at": created_at,
                    "carrier": random.choice(CARRIERS),
                    "shipping_status": ship_status,
                    "shipped_at": shipped_at,
                    "delivered_at": delivered_at,
                    "estimated_delivery_date": (run_date + timedelta(days=random.randint(2, 5))).isoformat(),
                }
            )
            shipment_id += 1

    return (
        pd.DataFrame(orders_rows),
        pd.DataFrame(items_rows),
        pd.DataFrame(payments_rows),
        pd.DataFrame(shipments_rows),
    )



# ---- Stable schemas (used when a file is empty) ----
SCHEMA_COLUMNS = {
    "customers": ["customer_id","created_at","updated_at","first_name","last_name","email","phone","city","state","country","customer_segment"],
    "products": ["product_id","created_at","updated_at","product_name","category","brand","base_price","active_flag"],
    "orders": ["order_id","created_at","updated_at","customer_id","order_status","order_channel","currency","shipping_city","shipping_state","shipping_country"],
    "order_items": ["order_item_id","order_id","product_id","quantity","unit_price","line_amount","created_at"],
    "payments": ["payment_id","order_id","created_at","updated_at","payment_status","payment_method","amount","currency","provider"],
    "shipments": ["shipment_id","order_id","created_at","updated_at","carrier","shipping_status","shipped_at","delivered_at","estimated_delivery_date"],
}

def _read_csv_safe(p: Path, table_name: str):
    """
    Read CSV safely.
    If file is empty (0 bytes) or has no parseable columns, return empty df with expected columns.
    """
    import pandas as pd
    if not p.exists():
        return None
    if p.stat().st_size == 0:
        return pd.DataFrame(columns=SCHEMA_COLUMNS.get(table_name, []))
    try:
        df = pd.read_csv(p)
        # If columns are missing for any reason, force schema
        if df.shape[1] == 0:
            return pd.DataFrame(columns=SCHEMA_COLUMNS.get(table_name, []))
        return df
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=SCHEMA_COLUMNS.get(table_name, []))



STATE_KEYS = {
    "customers": "customer_id",
    "products": "product_id",
    "orders": "order_id",
    "order_items": "order_item_id",
    "payments": "payment_id",
    "shipments": "shipment_id",
}


def _read_state(source_root: Path, up_to: date) -> Optional[Dict[str, pd.DataFrame]]:
    """
    The store's database as it stood at the end of `up_to`: every earlier day's
    drop, keeping the newest version of each record. Each day only sends what
    was new or changed, so the previous day's file alone is not the full store
    (day 2 sends no products, for example).
    """
    days = sorted(
        d for d in source_root.glob("day=*")
        if d.is_dir() and parse_yyyy_mm_dd(d.name[4:]) <= up_to
    )
    if not days:
        return None
    tables: Dict[str, pd.DataFrame] = {}
    for name, key in STATE_KEYS.items():
        parts = [df for df in (_read_csv_safe(d / f"{name}.csv", name) for d in days) if df is not None and len(df)]
        if not parts:
            tables[name] = pd.DataFrame(columns=SCHEMA_COLUMNS.get(name, []))
            continue
        df = pd.concat(parts, ignore_index=True)
        if "updated_at" in df.columns:
            df = df.sort_values("updated_at", kind="stable")
        tables[name] = df.drop_duplicates(subset=[key], keep="last").reset_index(drop=True)
    return tables if tables else None




def _apply_late_updates(run_date: date, cfg: VolumeConfig, prev: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """
    Create *update rows only* for upsert tables. These rows will show up in Day 2 drops.
    We do NOT re-send full Day 1 history; we send only changed rows (realistic CDC-like drops).
    """
    updates: Dict[str, pd.DataFrame] = {}
    pick = random.randint(0, 2**31 - 1)  # follows the day's seed, so picks differ each day
    recent_from = (run_date - timedelta(days=LATE_UPDATE_WINDOW_DAYS)).isoformat()

    def recent(df: pd.DataFrame) -> pd.DataFrame:
        """Late changes only happen to recent records, like a payment retried within the week."""
        return df[df["created_at"].astype(str) >= recent_from] if len(df) else df

    # Customers: small portion change segment/city with newer updated_at
    cust = prev.get("customers", pd.DataFrame()).copy()
    if len(cust) > 0:
        k = max(1, int(len(cust) * cfg.customer_update_rate))
        sample = cust.sample(n=k, random_state=pick).copy()
        sample["customer_segment"] = sample["customer_segment"].apply(lambda _: random.choice(["Regular", "VIP"]))
        sample["city"] = sample["city"].apply(lambda _: fake.city())
        sample["updated_at"] = sample["updated_at"].apply(lambda _: _rand_ts(run_date))
        updates["customers"] = sample

    # Orders: some status progresses
    orders = recent(prev.get("orders", pd.DataFrame())).copy()
    orders = orders[~orders["order_status"].isin(["DELIVERED", "CANCELLED"])] if len(orders) else orders
    if len(orders) > 0:
        k = max(1, int(len(orders) * cfg.order_update_rate))
        sample = orders.sample(n=k, random_state=pick + 1).copy()
        sample["order_status"] = sample["order_status"].apply(_next_status)
        sample["updated_at"] = sample["updated_at"].apply(lambda _: _rand_ts(run_date))
        updates["orders"] = sample

    # Payments: some FAILED/AUTHORIZED become CAPTURED
    pay = recent(prev.get("payments", pd.DataFrame())).copy()
    if len(pay) > 0:
        candidates = pay[pay["payment_status"].isin(["FAILED", "AUTHORIZED"])].copy()
        if len(candidates) > 0:
            k = max(1, int(len(candidates) * cfg.payment_fix_rate))
            sample = candidates.sample(n=min(k, len(candidates)), random_state=pick + 2).copy()
            sample["payment_status"] = "CAPTURED"
            sample["updated_at"] = sample["updated_at"].apply(lambda _: _rand_ts(run_date))
            updates["payments"] = sample

    # Shipments: some IN_TRANSIT become DELIVERED with delivered_at
    ship = recent(prev.get("shipments", pd.DataFrame())).copy()
    if len(ship) > 0:
        candidates = ship[ship["shipping_status"] == "IN_TRANSIT"].copy()
        if len(candidates) > 0:
            k = max(1, int(len(candidates) * cfg.shipment_update_rate))
            sample = candidates.sample(n=min(k, len(candidates)), random_state=pick + 3).copy()
            sample["shipping_status"] = "DELIVERED"
            sample["delivered_at"] = sample["delivered_at"].apply(lambda _: _rand_ts(run_date, 10, 23))
            sample["updated_at"] = sample["updated_at"].apply(lambda _: _rand_ts(run_date))
            updates["shipments"] = sample

    return updates


def _create_new_shipments_for_progressed_orders(
    run_date: date,
    cfg: VolumeConfig,
    prev: Dict[str, pd.DataFrame],
    order_updates: pd.DataFrame,
    start_shipment_id: int,
) -> pd.DataFrame:
    """
    Create NEW shipment rows on Day 2 for some orders that got updated to SHIPPED.
    """
    prev_ship = prev.get("shipments", pd.DataFrame())
    prev_has_ship = set(prev_ship["order_id"].tolist()) if len(prev_ship) else set()

    candidates = order_updates[order_updates["order_status"].isin(["SHIPPED", "DELIVERED"])].copy()
    candidates = candidates[~candidates["order_id"].isin(prev_has_ship)]
    if len(candidates) == 0:
        return pd.DataFrame()

    k = max(1, int(len(candidates) * cfg.new_shipments_rate))
    chosen = candidates.sample(n=min(k, len(candidates)), random_state=random.randint(0, 2**31 - 1)).copy()

    rows = []
    sid = start_shipment_id
    for _, r in chosen.iterrows():
        shipped_at = _rand_ts(run_date, 8, 18)
        ship_status = "IN_TRANSIT"
        delivered_at = None
        if r["order_status"] == "DELIVERED":
            delivered_at = _rand_ts(run_date, 12, 23)
            ship_status = "DELIVERED"

        rows.append(
            {
                "shipment_id": f"S{sid:09d}",
                "order_id": r["order_id"],
                "created_at": shipped_at,
                "updated_at": _rand_ts(run_date),
                "carrier": random.choice(CARRIERS),
                "shipping_status": ship_status,
                "shipped_at": shipped_at,
                "delivered_at": delivered_at,
                "estimated_delivery_date": (run_date + timedelta(days=random.randint(1, 4))).isoformat(),
            }
        )
        sid += 1

    return pd.DataFrame(rows)


def write_day_files(out_dir: Path, tables: Dict[str, pd.DataFrame]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_csv(out_dir / f"{name}.csv", index=False)


def main() -> None:
    run_date_str = os.getenv("RUN_DATE", "").strip()
    if not run_date_str:
        raise SystemExit("Set RUN_DATE like: RUN_DATE=2026-01-02 python -m src.ingestion.generate_source_data")

    run_date = parse_yyyy_mm_dd(run_date_str)
    _seed_for_day(run_date)
    cfg = VolumeConfig()

    prev_date_str = os.getenv("PREV_DATE", "").strip()
    if not prev_date_str:
        prev_date_str = (run_date - timedelta(days=1)).isoformat()

    out_dir = Path("data/source") / f"day={run_date_str}"
    prev = _read_state(Path("data/source"), parse_yyyy_mm_dd(prev_date_str))

    if prev is None:
        # Day 1 behavior (fresh snapshot)
        products = generate_products(run_date, cfg)
        customers = generate_customers(run_date, cfg, start_id=1)

        orders, order_items, payments, shipments = generate_orders_items_payments_shipments(
            run_date=run_date,
            cfg=cfg,
            customers=customers,
            products=products,
            start_order_id=1,
            start_order_item_id=1,
            start_payment_id=1,
            start_shipment_id=1,
        )

        write_day_files(
            out_dir,
            {
                "customers": customers,
                "products": products,
                "orders": orders,
                "order_items": order_items,
                "payments": payments,
                "shipments": shipments,
            },
        )
        print(f"Generated source data for {run_date_str} in {out_dir} (fresh day)")
        return

    # Day 2+ behavior: new rows + update rows
    prev_customers = prev["customers"]
    prev_products = prev["products"]
    prev_orders = prev["orders"]
    prev_order_items = prev["order_items"]
    prev_payments = prev["payments"]
    prev_shipments = prev["shipments"]

    # New customers continue IDs
    max_c = _max_numeric_id(prev_customers["customer_id"], "C")
    new_customers = generate_customers(run_date, cfg, start_id=max_c + 1)

    # Products: keep same catalog for now (or could change later). For realism, send none as "new".
    # We will not resend full product snapshot daily. Keep empty unless we add product updates later.
    new_products = pd.DataFrame(columns=prev_products.columns)

    # New orders continue IDs and use customers = (prev + new)
    all_customers = pd.concat([prev_customers, new_customers], ignore_index=True)
    max_o = _max_numeric_id(prev_orders["order_id"], "O")
    max_oi = _max_numeric_id(prev_order_items["order_item_id"], "OI")
    max_pay = _max_numeric_id(prev_payments["payment_id"], "PAY")
    max_ship = _max_numeric_id(prev_shipments["shipment_id"], "S") if len(prev_shipments) else 0

    new_orders, new_order_items, new_payments, new_shipments = generate_orders_items_payments_shipments(
        run_date=run_date,
        cfg=cfg,
        customers=all_customers,
        products=prev_products,
        start_order_id=max_o + 1,
        start_order_item_id=max_oi + 1,
        start_payment_id=max_pay + 1,
        start_shipment_id=max_ship + 1,
    )

    # Late updates (update rows only)
    updates = _apply_late_updates(run_date, cfg, prev)
    order_updates = updates.get("orders", pd.DataFrame(columns=prev_orders.columns))
    next_sid = max(max_ship, _max_numeric_id(new_shipments["shipment_id"], "S") if len(new_shipments) else 0) + 1
    extra_shipments = _create_new_shipments_for_progressed_orders(run_date, cfg, prev, order_updates, next_sid)

    # Combine shipments: new shipments from new orders + extra shipments for progressed orders + shipment updates
    shipment_updates = updates.get("shipments", pd.DataFrame(columns=prev_shipments.columns))
    payments_updates = updates.get("payments", pd.DataFrame(columns=prev_payments.columns))
    customer_updates = updates.get("customers", pd.DataFrame(columns=prev_customers.columns))

    # Write Day 2 drops (new rows + update rows)
    write_day_files(
        out_dir,
        {
            "customers": pd.concat([new_customers, customer_updates], ignore_index=True),
            "products": new_products,
            "orders": pd.concat([new_orders, order_updates], ignore_index=True),
            "order_items": new_order_items,  # append-only
            "payments": pd.concat([new_payments, payments_updates], ignore_index=True),
            "shipments": pd.concat([new_shipments, shipment_updates, extra_shipments], ignore_index=True),
        },
    )

    print(f"Generated source data for {run_date_str} in {out_dir} (incremental day)")
    print(f"New customers: {len(new_customers):,}, Customer updates: {len(customer_updates):,}")
    print(f"New orders: {len(new_orders):,}, Order updates: {len(order_updates):,}")
    print(f"New order_items: {len(new_order_items):,}")
    print(f"New payments: {len(new_payments):,}, Payment updates: {len(payments_updates):,}")
    print(f"New shipments: {len(new_shipments):,}, Shipment updates: {len(shipment_updates):,}, Extra shipments: {len(extra_shipments):,}")


if __name__ == "__main__":
    main()
