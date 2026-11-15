"""
Pkomm Motors dealer management system (DMS).

This is the source system the pipeline reads from. It behaves like the
software a real dealership runs on:

- It records one business day at a time and hands over that day's export:
  the cars sold, the service jobs done, and the changes to the lot.
- Each day is generated from its own seed (the date itself), so any day can
  be produced again and comes out identical.
- Once a day has been exported, it is never written again. A closed day is
  closed.

The old generator rebuilt the last 90 days with new random numbers on every
run, which is why yesterday's sales kept changing (problem 001).

Run it from divisions/pkomm-motors:
    python -m sources.dms.dms                       # writes the standard history
    python -m sources.dms.dms 2026-11-14            # writes one more day
"""

from __future__ import annotations

import calendar
import math
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd
from faker import Faker

# The history the dashboard ships with. The next day after it is "today".
HISTORY_START = date(2026, 8, 16)
HISTORY_END = date(2026, 11, 13)

SOURCE_ROOT = os.path.join("data", "source")
REFERENCE_SEED = 7

LOCATIONS = [
    # location_id, city, state, manager, monthly target, opened
    ("LOC001", "Dallas", "TX", "Marcus Johnson", 1350000, "2018-03-15"),
    ("LOC002", "Chicago", "IL", "Sarah Mitchell", 1450000, "2017-06-01"),
    ("LOC003", "Atlanta", "GA", "David Reyes", 1200000, "2019-01-10"),
    ("LOC004", "Phoenix", "AZ", "Linda Park", 1100000, "2020-08-22"),
    ("LOC005", "Seattle", "WA", "James Okafor", 1300000, "2019-11-05"),
]

# Illinois does not allow car sales on Sundays, and most Texas dealers close
# on Sunday too. The service bays everywhere are closed on Sundays.
NO_SUNDAY_SALES = {"IL", "TX"}

MAKES_MODELS = [
    ("Toyota", "Camry", 28000), ("Toyota", "RAV4", 35000),
    ("Ford", "F-150", 45000), ("Ford", "Explorer", 38000),
    ("Honda", "Civic", 25000), ("Honda", "CR-V", 33000),
    ("BMW", "3 Series", 55000), ("BMW", "X5", 72000),
    ("Chevrolet", "Silverado", 42000), ("Chevrolet", "Equinox", 30000),
]

JOB_TYPES = [
    ("oil_change", 1.0, 89), ("tire_rotation", 0.5, 49),
    ("brake_replacement", 3.0, 350), ("engine_repair", 8.0, 950),
    ("transmission_service", 5.0, 650), ("ac_repair", 4.0, 480),
    ("battery_replacement", 1.0, 220),
]

# Every branch has one salesperson who sells far more than the others.
SELLER_WEIGHTS = [0.40, 0.25, 0.20, 0.15]
LOT_TARGET = 22          # cars a branch tries to keep on the lot
BASE_SALES_PER_DAY = 1.0  # per branch, on an ordinary weekday

SALES_COLUMNS = ["transaction_id", "vehicle_id", "employee_id", "location_id",
                 "sale_price", "sale_date", "financing_approved"]
JOB_COLUMNS = ["job_id", "vehicle_id", "technician_id", "location_id", "job_type",
               "estimated_hours", "actual_hours", "labor_revenue", "job_date"]
VEHICLE_COLUMNS = ["vehicle_id", "make", "model", "year", "list_price", "status",
                   "location_id", "received_date", "updated_at"]


class ClosedDayError(Exception):
    """Raised when something tries to write a day that was already exported."""


# ------------------------------------------------------------
# Reference data: branches and staff. Fixed, written once.
# ------------------------------------------------------------

def locations() -> pd.DataFrame:
    return pd.DataFrame(LOCATIONS, columns=["location_id", "city", "state", "manager_name",
                                            "monthly_target", "opened_date"])


def employees() -> pd.DataFrame:
    fake = Faker()
    fake.seed_instance(REFERENCE_SEED)
    rng = random.Random(REFERENCE_SEED)
    rows, n = [], 1
    for loc_id, *_ in LOCATIONS:
        for role, count in (("salesperson", 4), ("service_technician", 3)):
            for _ in range(count):
                rows.append({
                    "employee_id": f"EMP{n:03d}",
                    "full_name": fake.name(),
                    "role": role,
                    "location_id": loc_id,
                    "hire_date": (date(2025, 6, 1) - timedelta(days=rng.randint(180, 1800))).isoformat(),
                    "commission_rate": round(rng.uniform(0.02, 0.05), 3) if role == "salesperson" else 0.0,
                })
                n += 1
    return pd.DataFrame(rows)


# ------------------------------------------------------------
# State: what the dealership looks like at the start of a day
# ------------------------------------------------------------

@dataclass
class Car:
    vehicle_id: str
    make: str
    model: str
    year: int
    list_price: float
    location_id: str
    received_date: date
    status: str = "available"


@dataclass
class State:
    cars: dict = field(default_factory=dict)  # vehicle_id -> Car

    def on_lot(self, location_id: str) -> list:
        return sorted((c for c in self.cars.values()
                       if c.location_id == location_id and c.status == "available"),
                      key=lambda c: c.vehicle_id)

    def all_ids(self) -> list:
        return sorted(self.cars)


def _new_car(rng: random.Random, vehicle_id: str, location_id: str, received: date) -> Car:
    make, model, base = rng.choice(MAKES_MODELS)
    return Car(vehicle_id, make, model, rng.choice([2024, 2025, 2026]),
               float(base + rng.randint(-2000, 5000)), location_id, received)


def opening_inventory() -> State:
    """The lots on the evening before HISTORY_START."""
    rng = random.Random(REFERENCE_SEED)
    state, n = State(), 1
    for loc_id, *_ in LOCATIONS:
        for _ in range(LOT_TARGET):
            received = HISTORY_START - timedelta(days=rng.randint(1, 90))
            car = _new_car(rng, f"VEH{n:04d}", loc_id, received)
            state.cars[car.vehicle_id] = car
            n += 1
    return state


# ------------------------------------------------------------
# One business day
# ------------------------------------------------------------

def _poisson(rng: random.Random, mean: float) -> int:
    # Knuth's method; fine for the small means used here
    limit, k, p = math.exp(-mean), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _demand(day: date) -> float:
    """How busy a day is, compared with an ordinary weekday."""
    factor = {0: 0.9, 1: 1.0, 2: 1.0, 3: 1.05, 4: 1.25, 5: 1.7, 6: 0.8}[day.weekday()]
    last_day = calendar.monthrange(day.year, day.month)[1]
    if day.day > last_day - 3:
        factor *= 1.4  # month-end push to hit targets
    return factor


def _price_for(rng: random.Random, car: Car, day: date) -> float:
    """Cars that sit on the lot get discounted."""
    age = (day - car.received_date).days
    if age > 60:
        low, high = 0.88, 0.94
    elif age > 30:
        low, high = 0.93, 0.98
    else:
        low, high = 0.96, 1.02
    return round(car.list_price * rng.uniform(low, high), 2)


def _at(rng: random.Random, day: date, start_hour: int, end_hour: int) -> str:
    minutes = rng.randint(start_hour * 60, end_hour * 60 - 1)
    return datetime.combine(day, time(minutes // 60, minutes % 60, rng.randint(0, 59))).strftime("%Y-%m-%d %H:%M:%S")


def generate_day(day: date, state: State, staff: pd.DataFrame) -> dict:
    """Produces one day's export and moves the state forward to the next morning."""
    rng = random.Random(int(day.strftime("%Y%m%d")))
    ymd = day.strftime("%Y%m%d")
    sunday = day.weekday() == 6
    sales, jobs, vehicle_changes = [], [], []

    for loc_id, city, st, *_ in LOCATIONS:
        sellers = staff[(staff.location_id == loc_id) & (staff.role == "salesperson")].employee_id.tolist()
        techs = staff[(staff.location_id == loc_id) & (staff.role == "service_technician")].employee_id.tolist()

        # Sales
        if not (sunday and st in NO_SUNDAY_SALES):
            for _ in range(_poisson(rng, BASE_SALES_PER_DAY * _demand(day))):
                lot = state.on_lot(loc_id)
                if not lot:
                    break
                car = rng.choice(lot)
                seller = rng.choices(sellers, weights=SELLER_WEIGHTS)[0]
                sold_at = _at(rng, day, 10, 20)
                sales.append({
                    "transaction_id": f"TXN{ymd}{len(sales) + 1:02d}",
                    "vehicle_id": car.vehicle_id,
                    "employee_id": seller,
                    "location_id": loc_id,
                    "sale_price": _price_for(rng, car, day),
                    "sale_date": sold_at,
                    "financing_approved": rng.random() < 0.7,
                })
                car.status = "sold"
                vehicle_changes.append((car, sold_at))

        # Service bay
        if not sunday:
            fleet = state.all_ids()
            for _ in range(rng.randint(2, 5)):
                job_type, est, rate = rng.choice(JOB_TYPES)
                actual = round(est * rng.uniform(0.8, 1.4), 2)
                jobs.append({
                    "job_id": f"JOB{ymd}{len(jobs) + 1:02d}",
                    "vehicle_id": rng.choice(fleet),
                    "technician_id": rng.choice(techs),
                    "location_id": loc_id,
                    "job_type": job_type,
                    "estimated_hours": est,
                    "actual_hours": actual,
                    "labor_revenue": round(actual * rate, 2),
                    "job_date": _at(rng, day, 7, 18),
                })

        # New cars arrive on weekdays to keep the lot stocked
        if day.weekday() < 5:
            short = LOT_TARGET - len(state.on_lot(loc_id))
            for i in range(max(0, short) + rng.choice([0, 0, 1])):
                car = _new_car(rng, f"VEH{ymd}{loc_id[-1]}{i + 1:02d}", loc_id, day)
                state.cars[car.vehicle_id] = car
                vehicle_changes.append((car, _at(rng, day, 8, 10)))

    vehicles = [{
        "vehicle_id": c.vehicle_id, "make": c.make, "model": c.model, "year": c.year,
        "list_price": c.list_price, "status": c.status, "location_id": c.location_id,
        "received_date": c.received_date.isoformat(), "updated_at": ts,
    } for c, ts in vehicle_changes]

    return {
        "sales_transactions": pd.DataFrame(sales, columns=SALES_COLUMNS),
        "service_jobs": pd.DataFrame(jobs, columns=JOB_COLUMNS),
        "vehicles": pd.DataFrame(vehicles, columns=VEHICLE_COLUMNS),
    }


def opening_export(state: State) -> dict:
    """The lot as it stood before the first day, sent once as its own drop."""
    day = HISTORY_START - timedelta(days=1)
    vehicles = [{
        "vehicle_id": c.vehicle_id, "make": c.make, "model": c.model, "year": c.year,
        "list_price": c.list_price, "status": c.status, "location_id": c.location_id,
        "received_date": c.received_date.isoformat(), "updated_at": f"{day} 20:00:00",
    } for c in sorted(state.cars.values(), key=lambda c: c.vehicle_id)]
    return {
        "sales_transactions": pd.DataFrame(columns=SALES_COLUMNS),
        "service_jobs": pd.DataFrame(columns=JOB_COLUMNS),
        "vehicles": pd.DataFrame(vehicles, columns=VEHICLE_COLUMNS),
    }


# ------------------------------------------------------------
# Running the system
# ------------------------------------------------------------

def days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def replay(through: date):
    """
    Runs the dealership from the opening lot up to and including `through`,
    yielding (day, export) for each day. Every day comes out the same every
    time, so any day's export can be produced again without storing it.
    """
    staff = employees()
    state = opening_inventory()
    yield HISTORY_START - timedelta(days=1), opening_export(state)
    for d in days(HISTORY_START, through):
        yield d, generate_day(d, state, staff)


def export_for(day: date) -> dict:
    """One day's export on its own."""
    for d, export in replay(day):
        if d == day:
            return export
    raise ValueError(f"{day} is before the opening lot")


def write_day(root: str, day: date, export: dict) -> str:
    """Writes a day's files. A day that was already exported is never written again."""
    folder = os.path.join(root, f"day={day.isoformat()}")
    if os.path.exists(folder):
        raise ClosedDayError(f"{day} was already exported. The dealer system never rewrites a day.")
    os.makedirs(folder)
    for name, df in export.items():
        df.to_csv(os.path.join(folder, f"{name}.csv"), index=False)
    return folder


def write_reference(root: str) -> str:
    folder = os.path.join(root, "reference")
    os.makedirs(folder, exist_ok=True)
    locations().to_csv(os.path.join(folder, "locations.csv"), index=False)
    employees().to_csv(os.path.join(folder, "employees.csv"), index=False)
    return folder


def write_history(root: str = SOURCE_ROOT, through: date = HISTORY_END) -> list:
    """Writes every day up to `through` that has not been written yet."""
    write_reference(root)
    written = []
    for d, export in replay(through):
        if not os.path.exists(os.path.join(root, f"day={d.isoformat()}")):
            written.append(write_day(root, d, export))
    return written


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if argv:
        day = date.fromisoformat(argv[0])
        write_reference(SOURCE_ROOT)
        folder = write_day(SOURCE_ROOT, day, export_for(day))
        print(f"Exported {day} to {folder}")
    else:
        written = write_history()
        print(f"Exported {len(written)} new day(s) to {SOURCE_ROOT}")


if __name__ == "__main__":
    main()
