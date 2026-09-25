"""
Numerical-answer support: parse "value + unit" answers, check a unit against a
named physical quantity, and grade a student's answer against a key with
automatic unit conversion.

Backed by `pint` for the unit algebra (prefixes, compound units, temperature
offsets). Everything else in the app talks to this module, never to pint
directly, so the registry quirks we patch here (see _registry) apply
everywhere.

A numerical question stores its key as `q["numeric"]`:

    {"value": 4.2, "value_text": "4.20", "unit": "m/s",
     "quantity": "velocity", "sig_figs": 3}

`value_text` is the key exactly as the author typed it; `sig_figs` defaults to
its significant-figure count and sets the grading tolerance (see
`tolerance()`). `quantity` is a key into QUANTITIES, or "other" for a unit the
catalog doesn't name — grading then only requires the student's unit to have
the key unit's dimension.

Grading rules (spec.md §4b):
  - the student's value is converted into the key's unit; correct iff it lies
    within half a unit of the key's last significant digit
  - a unit of the wrong dimension scores 0
  - a bare number with no unit, whose value would be right in the key's unit,
    earns NO_UNIT_CREDIT (partial credit). For a dimensionless key a bare
    number is a complete answer, read either as a plain number or in the key's
    unit ("75" or "0.75" both answer "75 %")
"""
from __future__ import annotations

import math
import re
import threading
from functools import lru_cache

NO_UNIT_CREDIT = 0.5
MAX_UNIT_LEN = 60
MAX_VALUE_LEN = 60

# name -> (label, dimension expression, suggested units). Units are listed in
# the spelling shown to people; each must parse (tests/test_units.py checks).
# Torque and energy share a dimension, as do angle and fraction — the stored
# quantity is what tells them apart for display; grading only needs the
# dimension, so either accepts the other's units.
QUANTITIES: dict[str, tuple[str, str, list[str]]] = {
    "length":         ("Length", "[length]", ["m", "cm", "mm", "km", "µm", "nm", "in", "ft", "mi"]),
    "area":           ("Area", "[length]**2", ["m²", "cm²", "mm²", "km²"]),
    "volume":         ("Volume", "[length]**3", ["m³", "L", "mL", "cm³"]),
    "time":           ("Time", "[time]", ["s", "ms", "µs", "ns", "min", "h"]),
    "mass":           ("Mass", "[mass]", ["kg", "g", "mg", "u"]),
    "velocity":       ("Velocity / speed", "[length]/[time]", ["m/s", "km/h", "cm/s", "mph", "ft/s"]),
    "acceleration":   ("Acceleration", "[length]/[time]**2", ["m/s²", "cm/s²", "ft/s²"]),
    "force":          ("Force", "[mass]*[length]/[time]**2", ["N", "kN", "mN", "dyn", "lbf"]),
    "momentum":       ("Momentum / impulse", "[mass]*[length]/[time]", ["kg·m/s", "N·s"]),
    "energy":         ("Energy / work / heat", "[mass]*[length]**2/[time]**2",
                       ["J", "kJ", "MJ", "eV", "keV", "MeV", "cal", "kcal", "kWh"]),
    "torque":         ("Torque", "[mass]*[length]**2/[time]**2", ["N·m"]),
    "power":          ("Power", "[mass]*[length]**2/[time]**3", ["W", "mW", "kW", "MW", "hp"]),
    "pressure":       ("Pressure", "[mass]/[length]/[time]**2", ["Pa", "kPa", "MPa", "atm", "bar", "mmHg", "psi"]),
    "density":        ("Density", "[mass]/[length]**3", ["kg/m³", "g/cm³", "g/mL"]),
    "frequency":      ("Frequency", "1/[time]", ["Hz", "kHz", "MHz", "GHz"]),
    "angle":          ("Angle", "", ["rad", "deg", "mrad"]),
    "temperature":    ("Temperature", "[temperature]", ["K", "°C", "°F"]),
    "amount":         ("Amount of substance", "[substance]", ["mol", "mmol"]),
    "concentration":  ("Concentration", "[substance]/[length]**3", ["mol/L", "M", "mM", "mol/m³"]),
    "charge":         ("Electric charge", "[current]*[time]", ["C", "mC", "µC", "nC", "pC"]),
    "current":        ("Electric current", "[current]", ["A", "mA", "µA", "kA"]),
    "voltage":        ("Voltage / potential", "[mass]*[length]**2/[time]**3/[current]", ["V", "mV", "µV", "kV"]),
    "resistance":     ("Resistance", "[mass]*[length]**2/[time]**3/[current]**2", ["Ω", "mΩ", "kΩ", "MΩ"]),
    "conductance":    ("Conductance", "[current]**2*[time]**3/[mass]/[length]**2", ["S", "mS", "µS"]),
    "resistivity":    ("Resistivity", "[mass]*[length]**3/[time]**3/[current]**2", ["Ω·m", "Ω·cm"]),
    "capacitance":    ("Capacitance", "[current]**2*[time]**4/[mass]/[length]**2", ["F", "mF", "µF", "nF", "pF"]),
    "inductance":     ("Inductance", "[mass]*[length]**2/[time]**2/[current]**2", ["H", "mH", "µH"]),
    "magnetic_field": ("Magnetic field", "[mass]/[time]**2/[current]", ["T", "mT", "µT"]),
    "magnetic_flux":  ("Magnetic flux", "[mass]*[length]**2/[time]**2/[current]", ["Wb", "mWb"]),
    "electric_field": ("Electric field", "[mass]*[length]/[time]**3/[current]", ["V/m", "N/C", "kV/m"]),
    "heat_capacity":  ("Heat capacity / entropy", "[mass]*[length]**2/[time]**2/[temperature]", ["J/K", "kJ/K"]),
    "specific_heat":  ("Specific heat", "[length]**2/[time]**2/[temperature]", ["J/(kg·K)", "J/(g·K)", "kJ/(kg·K)"]),
    "fraction":       ("Fraction / ratio / dimensionless", "", ["", "%"]),
}
OTHER = "other"

_registry_lock = threading.Lock()
_ureg = None


def _registry():
    """The shared pint registry, built once (it takes ~0.5 s)."""
    global _ureg
    with _registry_lock:
        if _ureg is None:
            import pint
            ureg = pint.UnitRegistry(autoconvert_offset_to_baseunit=True)
            # pint reads "AU" as absorbance; in a science-olympiad context it
            # is always the astronomical unit. (Redefining logs a warning.)
            import logging
            log = logging.getLogger("pint.util")
            prev = log.level
            log.setLevel(logging.ERROR)
            try:
                ureg.define("AU = astronomical_unit")
            finally:
                log.setLevel(prev)
            _ureg = ureg
        return _ureg


class UnitError(ValueError):
    """A unit or value couldn't be understood. Message is user-facing."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_VALUE_RE = re.compile(
    r"""^\s*(?P<sign>[+\-−]?)\s*
        (?P<mant>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)
        (?:\s*[eE]\s*(?P<e1>[+\-−]?\d+)
          |\s*(?:x|×|\*|·)\s*10\s*(?:\^|\*\*)?\s*\(?\s*(?P<e2>[+\-−]?\d+)\s*\)?)?
        \s*$""", re.X)
_FRACTION_RE = re.compile(r"^\s*(?P<sign>[+\-−]?)\s*(?P<n>\d+)\s*/\s*(?P<d>\d+)\s*$")


def _clean_value_text(text: str) -> str:
    s = str(text or "").strip()
    s = s.replace("$", "").replace("\\times", "×").replace("\\cdot", "·")
    s = re.sub(r"\^\{([^}]*)\}", r"^\1", s)
    return s.translate(_SUPERSCRIPTS)


def parse_value(text: str) -> float:
    """"4.20", "-1.5e3", "3.0 × 10^8", "2.5×10⁻³", "1,200", "3/4" -> float.
    Raises UnitError."""
    raw = str(text or "")
    if len(raw) > MAX_VALUE_LEN:
        raise UnitError("that number is too long")
    s = _clean_value_text(raw)
    if not s:
        raise UnitError("no number given")
    m = _FRACTION_RE.match(s)
    if m:
        d = int(m.group("d"))
        if d == 0:
            raise UnitError("can't divide by zero")
        v = int(m.group("n")) / d
        return -v if m.group("sign") in ("-", "−") else v
    m = _VALUE_RE.match(s)
    if not m:
        raise UnitError(f"\"{raw.strip()}\" isn't a number")
    try:
        v = float(m.group("mant").replace(",", ""))
        exp = m.group("e1") or m.group("e2")
        if exp:
            v *= 10.0 ** int(exp.replace("−", "-"))
    except (ValueError, OverflowError):
        raise UnitError(f"\"{raw.strip()}\" is out of range")
    if not math.isfinite(v):
        raise UnitError(f"\"{raw.strip()}\" is out of range")
    return -v if m.group("sign") in ("-", "−") else v


def sig_figs_of(text: str) -> int | None:
    """Significant figures of a number as written: "4.20" -> 3, "0.0042" -> 2,
    "1200" -> 4, "3.0×10^8" -> 2. None when it isn't a plain decimal (e.g. a
    fraction).

    Deliberately stricter than the textbook rule for whole numbers: every
    digit of "10" or "1200" counts, where the standard rule would call their
    trailing zeros insignificant. As a grading default the textbook reading
    is far too loose (a "10 ms" key would accept 5-15 ms), and question
    writers almost always mean the number as written. The sig-figs field
    can still be lowered per question."""
    s = _clean_value_text(text)
    m = _VALUE_RE.match(s)
    if not m:
        return None
    mant = m.group("mant").replace(",", "")
    has_point = "." in mant
    digits = mant.replace(".", "").lstrip("0")
    if not digits:
        # A zero value: the leading zero plus every zero after the point
        # ("0" -> 1, "0.00" -> 3), so tolerance() lands on the last written
        # decimal place.
        return 1 + (len(mant.split(".")[1]) if has_point else 0)
    return len(digits)


def _normalize_unit_text(unit: str) -> str:
    u = str(unit or "").strip()
    u = u.replace("$", "")
    u = re.sub(r"\\(?:text|mathrm|rm)\s*\{([^}]*)\}", r"\1", u)
    u = u.replace("\\Omega", "Ω").replace("\\mu", "µ").replace("\\circ", "°")
    u = u.replace("^\\circ", "°")
    u = re.sub(r"\^\{([^}]*)\}", r"^\1", u)
    u = u.replace("·", "*").replace("⋅", "*")
    # pint wants "degC"/"degF" in compound expressions; a lone "°C" parses too.
    u = re.sub(r"°\s*([CF])\b", r"deg\1", u)
    if u in ("deg C", "deg F"):
        u = u.replace(" ", "")
    return u


@lru_cache(maxsize=2048)
def _parse_unit_cached(unit_norm: str):
    return _registry().parse_units(unit_norm)


def parse_unit(unit: str):
    """Unit text -> pint Unit ("" -> dimensionless). Raises UnitError."""
    raw = str(unit or "")
    if len(raw) > MAX_UNIT_LEN:
        raise UnitError("that unit is too long")
    norm = _normalize_unit_text(raw)
    if not norm:
        return _registry().dimensionless
    try:
        return _parse_unit_cached(norm)
    except Exception:
        raise UnitError(f"\"{raw.strip()}\" isn't a unit this grader recognises")


def _dimensionality(dim_expr: str):
    ureg = _registry()
    return ureg.get_dimensionality(dim_expr) if dim_expr else ureg.dimensionless.dimensionality


def unit_matches_quantity(unit: str, quantity: str) -> bool:
    if quantity == OTHER or quantity not in QUANTITIES:
        return True
    return parse_unit(unit).dimensionality == _dimensionality(QUANTITIES[quantity][1])


def quantities_for_unit(unit: str) -> list[str]:
    """Catalog quantities whose dimension matches `unit`, best first: a
    quantity that lists this exact unit spelling ranks ahead of one that only
    shares the dimension (so "N·m" -> torque before energy, "J" -> energy)."""
    u = parse_unit(unit)
    dims = u.dimensionality
    exact, same_dim = [], []
    for name, (_label, dim_expr, units_) in QUANTITIES.items():
        if dims != _dimensionality(dim_expr):
            continue
        spellings = {str(x).replace("·", "*") for x in units_}
        (exact if _normalize_unit_text(unit) in spellings or unit in units_ else same_dim).append(name)
    return exact + same_dim


def infer_quantity(unit: str) -> str:
    try:
        found = quantities_for_unit(unit)
    except UnitError:
        return OTHER
    return found[0] if found else OTHER


def catalog() -> list[dict]:
    """For the UI: [{name, label, units}] plus the "other" escape hatch."""
    out = [{"name": n, "label": lbl, "units": list(us)} for n, (lbl, _d, us) in QUANTITIES.items()]
    out.append({"name": OTHER, "label": "Other (any unit)", "units": []})
    return out


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def legacy_sig_figs_of(text: str) -> int | None:
    """The pre-2026-09-25 default (textbook rule: a whole number's trailing
    zeros aren't significant). Only used by the state migration that
    upgrades keys saved with that default."""
    s = _clean_value_text(text)
    m = _VALUE_RE.match(s)
    if not m:
        return None
    mant = m.group("mant").replace(",", "")
    if "." in mant:
        return sig_figs_of(text)
    digits = mant.lstrip("0")
    if not digits:
        return 1
    return len(digits.rstrip("0") or digits[:1])


def make_key(value_text: str, unit: str, quantity: str | None = None,
             sig_figs: int | None = None) -> dict:
    """Validate and build a `numeric` key. Raises UnitError with a message
    that names the problem."""
    value = parse_value(value_text)
    parse_unit(unit)
    if not quantity:
        quantity = infer_quantity(unit)
    if quantity != OTHER and quantity not in QUANTITIES:
        raise UnitError(f"unknown quantity \"{quantity}\"")
    if not unit_matches_quantity(unit, quantity):
        raise UnitError(f"\"{unit}\" isn't a unit of {QUANTITIES[quantity][0].lower()}")
    if sig_figs is None or sig_figs == "":
        sig_figs = sig_figs_of(value_text) or 3
    try:
        sig_figs = int(sig_figs)
    except (TypeError, ValueError):
        raise UnitError("significant figures must be a whole number")
    if not 1 <= sig_figs <= 15:
        raise UnitError("significant figures must be between 1 and 15")
    return {"value": value, "value_text": str(value_text).strip(), "unit": str(unit or "").strip(),
            "quantity": quantity, "sig_figs": sig_figs}


def key_problem(numeric) -> str:
    """"" when `numeric` is a usable key, else a user-facing reason."""
    if not isinstance(numeric, dict):
        return "no numerical answer recorded"
    try:
        make_key(numeric.get("value_text") or repr(numeric.get("value")),
                 numeric.get("unit") or "", numeric.get("quantity"), numeric.get("sig_figs"))
    except UnitError as e:
        return str(e)
    return ""


def format_key(numeric: dict) -> str:
    """"4.20 m/s" — the display form mirrored into q["answer"]."""
    if not isinstance(numeric, dict):
        return ""
    txt = str(numeric.get("value_text") or numeric.get("value") or "").strip()
    unit = str(numeric.get("unit") or "").strip()
    if unit == "%":
        return f"{txt}%"
    return f"{txt} {unit}".strip()


def tolerance(numeric: dict) -> float:
    """Half a unit in the key's last significant digit, in the key's unit."""
    v = float(numeric["value"])
    n = int(numeric.get("sig_figs") or 3)
    if v == 0:
        return 0.5 * 10.0 ** (-(n - 1))
    e = math.floor(math.log10(abs(v)))
    return 0.5 * 10.0 ** (e - n + 1)


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def _convert(value: float, from_unit, to_unit) -> float:
    q = _registry().Quantity(value, from_unit)
    return float(q.to(to_unit).magnitude)


def grade(numeric: dict, value_text: str, unit_text: str) -> dict:
    """Grade one answer. Returns
    {status, credit, message, expected, low, high, key_unit, given_in_key_unit}
    where status is one of correct | no_unit | wrong_value | wrong_dimension
    | unparseable | blank, and credit is the fraction of points earned."""
    expected = format_key(numeric)
    try:
        key_unit = parse_unit(numeric.get("unit") or "")
        key_val = float(numeric["value"])
        tol = tolerance(numeric)
    except (UnitError, KeyError, TypeError, ValueError):
        return {"status": "unparseable", "credit": 0.0, "expected": expected,
                "message": "this question's answer key is incomplete"}
    base = {"expected": expected, "low": key_val - tol, "high": key_val + tol,
            "key_unit": numeric.get("unit") or "", "given_in_key_unit": None}

    def within(v: float) -> bool:
        return abs(v - key_val) <= tol * (1 + 1e-9) + 1e-300

    if not str(value_text or "").strip():
        return {**base, "status": "blank", "credit": 0.0, "message": "no answer"}
    try:
        v = parse_value(value_text)
    except UnitError as e:
        return {**base, "status": "unparseable", "credit": 0.0, "message": str(e)}

    key_dimless = key_unit.dimensionality == _registry().dimensionless.dimensionality
    if not str(unit_text or "").strip():
        if key_dimless:
            candidates = [v]
            try:
                candidates.append(_convert(v, _registry().dimensionless, key_unit))
            except Exception:
                pass
            hit = next((c for c in candidates if within(c)), None)
            if hit is not None:
                return {**base, "status": "correct", "credit": 1.0, "given_in_key_unit": hit,
                        "message": "correct"}
            return {**base, "status": "wrong_value", "credit": 0.0, "given_in_key_unit": candidates[-1],
                    "message": f"not within the accepted range"}
        if within(v):
            return {**base, "status": "no_unit", "credit": NO_UNIT_CREDIT, "given_in_key_unit": v,
                    "message": f"right number, but no unit (partial credit)"}
        return {**base, "status": "wrong_value", "credit": 0.0, "given_in_key_unit": v,
                "message": "no unit, and the number isn't within the accepted range"}

    try:
        u = parse_unit(unit_text)
    except UnitError as e:
        return {**base, "status": "unparseable", "credit": 0.0, "message": str(e)}
    if u.dimensionality != key_unit.dimensionality:
        return {**base, "status": "wrong_dimension", "credit": 0.0,
                "message": f"\"{unit_text}\" measures a different kind of quantity"}
    try:
        conv = _convert(v, u, key_unit)
    except Exception:
        return {**base, "status": "wrong_dimension", "credit": 0.0,
                "message": f"\"{unit_text}\" can't be converted to {numeric.get('unit')}"}
    if within(conv):
        return {**base, "status": "correct", "credit": 1.0, "given_in_key_unit": conv, "message": "correct"}
    return {**base, "status": "wrong_value", "credit": 0.0, "given_in_key_unit": conv,
            "message": "not within the accepted range"}


def check_unit(unit: str, quantity: str | None) -> dict:
    """Live input check for the editors and the student answer box:
    {ok, message}. Only says whether the unit parses and fits the quantity —
    never anything about the answer."""
    try:
        parse_unit(unit)
    except UnitError as e:
        return {"ok": False, "message": str(e)}
    if quantity and quantity != OTHER and quantity in QUANTITIES:
        if not unit_matches_quantity(unit, quantity):
            if not str(unit or "").strip():
                return {"ok": False, "message": "no unit given — this answer needs one"}
            return {"ok": False,
                    "message": f"\"{unit}\" isn't a unit of {QUANTITIES[quantity][0].lower()}"}
    return {"ok": True, "message": ""}


def key_from_answer_text(answer: str, quantity: str | None = None) -> dict | None:
    """Best-effort "4.2 m/s" -> numeric key, for promoting a free-response or
    imported answer. None when it doesn't split cleanly into number + unit."""
    s = _clean_value_text(answer)
    s = re.sub(r"^\s*(?:≈|~|about\s+|approx(?:imately|\.)?\s+)", "", s, flags=re.I)
    m = re.match(r"""^\s*(?P<num>[+\-−]?\s*(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)
                     (?:\s*[eE]\s*[+\-−]?\d+|\s*(?:x|×|\*|·)\s*10\s*(?:\^|\*\*)?\s*\(?\s*[+\-−]?\d+\s*\)?)?)
                     \s*(?P<unit>.*?)\s*\.?\s*$""", s, re.X)
    if not m:
        return None
    unit = m.group("unit").strip()
    try:
        return make_key(m.group("num").strip(), unit, quantity)
    except UnitError:
        return None
