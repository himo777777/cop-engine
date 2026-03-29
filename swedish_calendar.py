"""
Svenska röda dagar, halvdagar och klämdagar.
Beräknar alla helgdagar dynamiskt — inga externa beroenden.
"""

from datetime import date, timedelta


def _easter(year: int) -> date:
    """Beräkna påskdagen med Gauss algoritm (Anonymous Gregorian)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def get_swedish_holidays(year: int) -> dict:
    """Alla svenska röda dagar för ett år.
    Returns: {"2026-01-01": "Nyårsdagen", ...}
    """
    easter = _easter(year)

    # Midsommardagen = lördag mellan 20-26 juni
    jun20 = date(year, 6, 20)
    midsummer_sat = jun20 + timedelta(days=(5 - jun20.weekday()) % 7)
    midsummer_eve = midsummer_sat - timedelta(days=1)

    # Alla helgons dag = lördag mellan 31 okt - 6 nov
    oct31 = date(year, 10, 31)
    all_saints = oct31 + timedelta(days=(5 - oct31.weekday()) % 7)

    holidays = {
        date(year, 1, 1).isoformat(): "Nyårsdagen",
        date(year, 1, 6).isoformat(): "Trettondedag jul",
        (easter - timedelta(days=2)).isoformat(): "Långfredagen",
        (easter - timedelta(days=1)).isoformat(): "Påskafton",
        easter.isoformat(): "Påskdagen",
        (easter + timedelta(days=1)).isoformat(): "Annandag påsk",
        date(year, 5, 1).isoformat(): "Första maj",
        (easter + timedelta(days=39)).isoformat(): "Kristi himmelsfärdsdag",
        date(year, 6, 6).isoformat(): "Nationaldagen",
        midsummer_eve.isoformat(): "Midsommarafton",
        midsummer_sat.isoformat(): "Midsommardagen",
        all_saints.isoformat(): "Alla helgons dag",
        date(year, 12, 24).isoformat(): "Julafton",
        date(year, 12, 25).isoformat(): "Juldagen",
        date(year, 12, 26).isoformat(): "Annandag jul",
        date(year, 12, 31).isoformat(): "Nyårsafton",
    }
    return holidays


def get_special_days(year: int) -> dict:
    """Halvdagar och specialdagar."""
    easter = _easter(year)
    jun20 = date(year, 6, 20)
    midsummer_sat = jun20 + timedelta(days=(5 - jun20.weekday()) % 7)
    midsummer_eve = midsummer_sat - timedelta(days=1)

    return {
        date(year, 12, 24).isoformat(): {"name": "Julafton", "type": "half_day", "close_at": "12:00"},
        date(year, 12, 31).isoformat(): {"name": "Nyårsafton", "type": "half_day", "close_at": "12:00"},
        midsummer_eve.isoformat(): {"name": "Midsommarafton", "type": "half_day", "close_at": "12:00"},
        (easter - timedelta(days=1)).isoformat(): {"name": "Påskafton", "type": "half_day", "close_at": "12:00"},
    }


def get_bridge_days(year: int) -> list:
    """Klämdagar — vardagar mellan röd dag och helg."""
    holidays = get_swedish_holidays(year)
    holiday_dates = {date.fromisoformat(d) for d in holidays}
    bridge = []

    for hd in holiday_dates:
        # Om röd dag = torsdag → fredag = klämdag
        if hd.weekday() == 3:  # Torsdag
            friday = hd + timedelta(days=1)
            if friday not in holiday_dates and friday.weekday() == 4:
                bridge.append(friday.isoformat())
        # Om röd dag = tisdag → måndag = klämdag
        if hd.weekday() == 1:
            monday = hd - timedelta(days=1)
            if monday not in holiday_dates and monday.weekday() == 0:
                bridge.append(monday.isoformat())

    return sorted(set(bridge))


def is_reduced_staffing_day(date_str: str) -> dict:
    """Kontrollera om ett datum kräver reducerad bemanning."""
    d = date.fromisoformat(date_str)
    year = d.year
    holidays = get_swedish_holidays(year)
    specials = get_special_days(year)
    bridges = get_bridge_days(year)

    if date_str in holidays:
        is_half = date_str in specials
        return {
            "is_holiday": True,
            "name": holidays[date_str],
            "staffing_level": "minimal",
            "is_half_day": is_half,
            "close_at": specials[date_str]["close_at"] if is_half else None,
        }

    if date_str in bridges:
        return {
            "is_holiday": False,
            "name": "Klämdag",
            "staffing_level": "reduced",
            "is_half_day": False,
            "close_at": None,
        }

    # Helg
    if d.weekday() >= 5:
        return {
            "is_holiday": False,
            "name": "Helg",
            "staffing_level": "minimal",
            "is_half_day": False,
            "close_at": None,
        }

    return {
        "is_holiday": False,
        "name": None,
        "staffing_level": "normal",
        "is_half_day": False,
        "close_at": None,
    }


# Storhelger med dubbeljour
MAJOR_HOLIDAYS = {"Julafton", "Juldagen", "Annandag jul", "Nyårsdagen", "Nyårsafton",
                  "Påskafton", "Påskdagen", "Annandag påsk", "Midsommarafton", "Midsommardagen"}


def get_holiday_staffing_rules(date_str: str) -> dict:
    """Bemanningsregler för specifikt datum."""
    info = is_reduced_staffing_day(date_str)
    holidays = get_swedish_holidays(date.fromisoformat(date_str).year)
    holiday_name = holidays.get(date_str, "")
    is_major = holiday_name in MAJOR_HOLIDAYS

    if info["is_holiday"]:
        return {
            "op_rooms_open": 0,
            "min_avd_staff": 2,
            "double_call": is_major,
            "call_type": "holiday",
            "staffing_level": info["staffing_level"],
        }

    if info["staffing_level"] == "reduced":
        return {
            "op_rooms_open": 1,
            "min_avd_staff": 2,
            "double_call": False,
            "call_type": "normal",
            "staffing_level": "reduced",
        }

    return {
        "op_rooms_open": None,  # Normal
        "min_avd_staff": None,
        "double_call": False,
        "call_type": "normal",
        "staffing_level": "normal",
    }
