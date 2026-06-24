"""
Event registry for the Science Olympiad question-bank pipeline.

Each Event holds everything that is specific to one Sci-Oly event: where its
PDFs live, how to identify them in `scioly_tests.json`, what topics make sense
for question classification, and the keyword weights for that classification.

The pipeline code (`build_question_bank.py`, `review_app.py`,
`download_event.py`) is otherwise event-agnostic — adding a new event means
adding one entry here, nothing else.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).parent

# Serialise concurrent custom-event registrations. Two browser tabs hitting
# "Register a new event" milliseconds apart used to race on the file write;
# the lock + atomic-write keeps the JSON consistent.
import threading as _threading
import os as _os
_custom_events_lock = _threading.Lock()

# Where per-event DATA (PDFs, images, generated markdown, state files) lives
# — separate from REPO_ROOT (the app CODE) so data can be redirected to a
# bigger/separate disk without touching the deployed code tree. Defaults to
# REPO_ROOT so every existing deployment keeps working with zero config;
# only set DATA_ROOT once you've actually moved data there (see README's
# "Maintaining the server" migration runbook before setting this on a box
# that already has data under REPO_ROOT).
DATA_ROOT = Path(_os.environ.get("DATA_ROOT") or REPO_ROOT)
CUSTOM_EVENTS_FILE = DATA_ROOT / "events_custom.json"


def relative_data_path(p: Path) -> str:
    """For display only — never used to reconstruct a real path. Shows a
    path relative to DATA_ROOT instead of the absolute filesystem location
    (e.g. on the events landing page), so the server's real directory
    layout isn't exposed to anyone who can view that page. Falls back to
    the bare name if `p` isn't actually under DATA_ROOT, rather than
    leaking the absolute path in that edge case either."""
    try:
        return str(Path(p).relative_to(DATA_ROOT))
    except ValueError:
        return Path(p).name


@dataclass(frozen=True)
class Event:
    slug: str                                       # url + directory slug
    name: str                                       # display name
    event_match: tuple[str, ...]                    # lowercase scioly.org event-name substrings
    topics: tuple[str, ...]
    topic_keywords: dict[str, list[tuple[str, int]]]
    # PDF filename prefix on disk. Optional — if left blank, derived from
    # event_match[0] by stripping spaces and lowercasing (e.g. "circuit lab"
    # → "circuitlab"). Override only for the special cases where scioly.org
    # names the PDFs differently than the event itself: Anatomy & Physiology
    # PDFs are `anatomy_*.pdf`, not `anatomyphysiology_*.pdf`.
    filename_prefix: str = ""
    foci: tuple[str, ...] = ()                      # rotating sub-topics (e.g. Endocrine, Nervous)
    wiki_page: str = ""                             # MediaWiki page name; default = name with _
    # Soft-delete flag for custom events. The web app never removes an
    # event's directory/PDFs/state file — "deleting" an event just hides it
    # from the landing page via this flag, fully reversible. Built-ins can't
    # be archived (see is_builtin() guard at every call site).
    archived: bool = False
    cover_markers: tuple[str, ...] = (
        "team name", "team number", "team #", "names:", "score:",
        "final score", "do not flip", "do not turn",
        "use the following constants", "instructions:",
        "all numerical answers", "competitor name",
    )

    def __post_init__(self):
        # Auto-fill filename_prefix from event_match when not explicitly set.
        # frozen=True means we route through object.__setattr__.
        if not self.filename_prefix:
            if self.event_match:
                derived = self.event_match[0].replace(" ", "").lower()
            else:
                # No event_match either (e.g. local-only event with uploaded PDFs)
                # — fall back to the slug.
                derived = self.slug
            object.__setattr__(self, "filename_prefix", derived)

    @property
    def base_dir(self) -> Path:
        return DATA_ROOT / self.slug

    @property
    def image_dir(self) -> Path:
        return self.base_dir / "images"

    @property
    def texts_dir(self) -> Path:
        return self.base_dir / "texts"

    @property
    def state_file(self) -> Path:
        return self.base_dir / ".qbank_state.json"

    @property
    def out_md(self) -> Path:
        return self.base_dir / "question_bank.md"

    @property
    def jobs_file(self) -> Path:
        return self.base_dir / ".qbank_jobs.json"

    @property
    def jobs_dir(self) -> Path:
        return self.base_dir / ".qbank_jobs"

    @property
    def wiki_url(self) -> str:
        page = self.wiki_page or self.name.replace(" ", "_")
        return f"https://scioly.org/wiki/index.php/{page}"


# ---------------------------------------------------------------------------
# Circuit Lab
# ---------------------------------------------------------------------------

_CIRCUIT_LAB_TOPICS = (
    "Basic Electrical Concepts",
    "Series & Parallel Circuits",
    "Kirchhoff's Laws",
    "Power & Energy",
    "Capacitors",
    "Inductors & Magnetism",
    "AC Circuits & Phasors",
    "Semiconductors & Diodes",
    "Measurement & Lab Skills",
    "Circuit Diagrams & Symbols",
    "Other / General",
)

_CIRCUIT_LAB_KEYWORDS: dict[str, list[tuple[str, int]]] = {
    "Basic Electrical Concepts": [
        ("ohm's law", 4), ("v=ir", 4), ("v = ir", 4),
        ("coulomb's law", 4), ("coulomb", 3),
        ("conductor", 2), ("insulator", 2),
        ("electric field", 3), ("potential difference", 3),
        ("resistivity", 3), ("drift velocity", 3),
        ("electron", 2), ("proton", 2), ("conventional current", 3),
        ("si unit", 2), ("charge", 2),
    ],
    "Series & Parallel Circuits": [
        ("series circuit", 4), ("parallel circuit", 4),
        ("series-parallel", 4), ("equivalent resistance", 4),
        ("in series", 3), ("in parallel", 3),
        ("voltage divider", 3), ("current divider", 3),
        ("total resistance", 3), ("combined resistance", 3),
        ("resistors in series", 4), ("resistors in parallel", 4),
        ("net resistance", 3), ("branch", 2),
    ],
    "Kirchhoff's Laws": [
        ("kirchhoff", 5), ("kvl", 5), ("kcl", 5),
        ("loop equation", 4), ("node equation", 4),
        ("mesh analysis", 4), ("nodal analysis", 4),
        ("junction rule", 4), ("voltage rule", 4),
        ("current rule", 3), ("superposition", 3),
        ("thevenin", 4), ("norton", 4),
        ("at a junction", 3), ("sum of currents", 3),
        ("sum of voltages", 3), ("closed loop", 3),
    ],
    "Power & Energy": [
        ("power dissipated", 4), ("p = iv", 4), ("p=iv", 4),
        ("p = i²r", 4), ("p=i²r", 4), ("p = v²/r", 4), ("p=v²/r", 4),
        ("watt", 3), ("kilowatt", 3), ("joule", 3),
        ("energy consumed", 3), ("heat generated", 3),
        ("efficiency", 3), ("power factor", 3),
        ("power rating", 3), ("electrical energy", 3),
    ],
    "Capacitors": [
        ("capacitor", 4), ("capacitance", 4), ("farad", 4),
        ("rc circuit", 4), ("time constant", 4),
        ("dielectric", 4), ("parallel plate", 3),
        ("microfarad", 4), ("nanofarad", 4), ("picofarad", 4),
        ("rc time", 3), ("charging", 2), ("discharging", 2),
    ],
    "Inductors & Magnetism": [
        ("inductor", 4), ("inductance", 4), ("henry", 4),
        ("solenoid", 4), ("magnetic flux", 4),
        ("faraday", 3), ("lenz", 4), ("rl circuit", 4),
        ("tesla", 3), ("magnetic field", 3), ("flux density", 4),
        ("back emf", 4), ("self-inductance", 4), ("mutual inductance", 4),
        ("permeability", 4), ("toroid", 4), ("coil", 2),
    ],
    "AC Circuits & Phasors": [
        ("alternating current", 4), ("ac circuit", 4), ("ac voltage", 4),
        ("angular frequency", 4), ("impedance", 4), ("reactance", 4),
        ("phasor", 4), ("rlc circuit", 4), ("resonance", 4),
        ("resonant frequency", 4), ("phase angle", 4),
        ("capacitive reactance", 4), ("inductive reactance", 4),
        ("rms voltage", 4), ("rms current", 4), ("sinusoidal", 3),
        ("hertz", 3), ("frequency", 2), ("ac", 2),
        ("60 hz", 3), ("50 hz", 3), ("peak voltage", 3),
    ],
    "Semiconductors & Diodes": [
        ("diode", 4), ("semiconductor", 4), ("transistor", 4),
        ("led", 3), ("light-emitting diode", 4), ("rectifier", 4),
        ("forward bias", 4), ("reverse bias", 4),
        ("p-n junction", 4), ("depletion", 4),
        ("doping", 4), ("n-type", 4), ("p-type", 4),
        ("zener", 4), ("mosfet", 4), ("bjt", 4),
        ("logic gate", 3), ("op-amp", 4), ("operational amplifier", 4),
        ("emitter", 3), ("collector", 3),
    ],
    "Measurement & Lab Skills": [
        ("multimeter", 4), ("oscilloscope", 4), ("ammeter", 4),
        ("voltmeter", 4), ("galvanometer", 4), ("ohmmeter", 4),
        ("breadboard", 4), ("waveform", 3), ("probe", 2),
        ("laboratory", 3), ("calibrate", 3), ("safety", 2),
        ("measurement", 2), ("instrument", 2),
    ],
    "Circuit Diagrams & Symbols": [
        ("schematic", 3), ("circuit diagram", 3), ("circuit symbol", 3),
        ("draw the circuit", 3), ("identify the component", 2),
        ("ground symbol", 3), ("component symbol", 3),
    ],
}


# ---------------------------------------------------------------------------
# Thermodynamics
# ---------------------------------------------------------------------------

_THERMO_TOPICS = (
    "Temperature & Heat",
    "Specific Heat & Calorimetry",
    "Thermal Expansion",
    "Heat Transfer",
    "Phase Changes & Latent Heat",
    "Laws of Thermodynamics",
    "Gas Laws & Ideal Gases",
    "Entropy & Free Energy",
    "Heat Engines & Refrigerators",
    "Insulation & Building Science",
    "Lab Skills & Measurement",
    "Other / General",
)

_THERMO_KEYWORDS: dict[str, list[tuple[str, int]]] = {
    "Temperature & Heat": [
        ("absolute zero", 4), ("kelvin scale", 4), ("celsius", 3),
        ("fahrenheit", 3), ("rankine", 3),
        ("thermal equilibrium", 4), ("internal energy", 3),
        ("kinetic theory", 4), ("average kinetic energy", 4),
        ("temperature scale", 3),
        ("0 k", 3), ("273", 2),
    ],
    "Specific Heat & Calorimetry": [
        ("specific heat", 5), ("heat capacity", 4), ("calorimeter", 5),
        ("calorimetry", 5), ("q = mc", 4), ("q=mc", 4),
        ("water equivalent", 4), ("heat lost", 3), ("heat gained", 3),
        ("j/(kg", 3), ("j/(g", 3), ("cal/g", 3),
        ("calorie", 3), ("kilocalorie", 3),
        ("final temperature", 3), ("mixing", 2),
    ],
    "Thermal Expansion": [
        ("thermal expansion", 5), ("coefficient of expansion", 5),
        ("linear expansion", 5), ("volumetric expansion", 5),
        ("bimetallic", 5), ("expansion joint", 4),
        ("alpha", 2), ("delta l", 3), ("ΔL", 3),
        ("contract", 2), ("expand", 2),
    ],
    "Heat Transfer": [
        ("conduction", 4), ("convection", 4), ("radiation", 4),
        ("thermal conductivity", 5), ("r-value", 5), ("u-value", 4),
        ("stefan-boltzmann", 5), ("emissivity", 5),
        ("blackbody", 4), ("black body", 4),
        ("fourier's law", 5), ("newton's law of cooling", 5),
        ("convection coefficient", 4), ("radiative", 3),
        ("conductive", 3), ("convective", 3),
        ("heat flux", 4), ("thermal resistance", 4),
        ("w/(m·k)", 3), ("w/m²", 3),
    ],
    "Phase Changes & Latent Heat": [
        ("latent heat", 5), ("phase change", 5), ("phase diagram", 5),
        ("heat of fusion", 5), ("heat of vaporization", 5),
        ("melting point", 4), ("boiling point", 4),
        ("sublimation", 4), ("condensation", 4), ("evaporation", 3),
        ("freezing", 3), ("melting", 3), ("vaporiz", 3),
        ("triple point", 5), ("critical point", 4),
        ("steam", 2), ("ice", 2),
    ],
    "Laws of Thermodynamics": [
        ("first law", 4), ("second law", 4), ("third law", 4),
        ("zeroth law", 5), ("law of thermodynamics", 5),
        ("conservation of energy", 4), ("clausius", 4),
        ("kelvin-planck", 5), ("kelvin planck", 5),
        ("perpetual motion", 4),
        ("ΔU", 3), ("dU", 2),
    ],
    "Gas Laws & Ideal Gases": [
        ("ideal gas", 5), ("pv = nrt", 5), ("pv=nrt", 5),
        ("boyle's law", 5), ("charles's law", 5), ("charles' law", 5),
        ("gay-lussac", 5), ("avogadro", 4), ("dalton's law", 4),
        ("partial pressure", 4), ("mole", 3),
        ("gas constant", 4), ("stp", 3),
        ("isothermal", 5), ("isobaric", 5), ("isochoric", 5),
        ("isovolumetric", 5), ("adiabatic", 5),
        ("real gas", 3), ("van der waals", 5),
    ],
    "Entropy & Free Energy": [
        ("entropy", 5), ("gibbs free energy", 5), ("helmholtz", 4),
        ("ΔS", 3), ("ΔG", 3),
        ("disorder", 3), ("spontaneous", 3),
        ("reversible", 3), ("irreversible", 3),
    ],
    "Heat Engines & Refrigerators": [
        ("heat engine", 5), ("carnot", 5), ("carnot cycle", 5),
        ("otto cycle", 5), ("diesel cycle", 5), ("rankine cycle", 5),
        ("stirling", 5),
        ("efficiency", 3), ("coefficient of performance", 5),
        ("cop", 4), ("refrigerator", 4), ("heat pump", 5),
        ("working fluid", 3), ("reservoir", 3),
        ("hot reservoir", 4), ("cold reservoir", 4),
    ],
    "Insulation & Building Science": [
        ("insulation", 4), ("fiberglass", 3), ("foam", 2),
        ("attic", 3), ("wall assembly", 3),
        ("hvac", 4), ("furnace", 3), ("air conditioner", 4),
        ("thermos", 3), ("dewar", 4), ("vacuum flask", 4),
        ("vapor barrier", 3),
    ],
    "Lab Skills & Measurement": [
        ("thermometer", 4), ("thermocouple", 5), ("rtd", 4),
        ("ir camera", 4), ("infrared thermometer", 4),
        ("pyrometer", 4),
        ("bunsen burner", 3), ("hot plate", 3),
        ("data logger", 3), ("graduated cylinder", 2),
        ("calibrate", 3), ("safety goggles", 2),
        ("error analysis", 3), ("uncertainty", 3),
    ],
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

EVENTS: dict[str, Event] = {}


def get_event(slug: str) -> Event:
    if slug not in EVENTS:
        raise SystemExit(
            f"Unknown event {slug!r}. "
            f"Known events: {', '.join(sorted(EVENTS))}"
        )
    return EVENTS[slug]


# Reserved for any event hardcoded directly into the EVENTS literal above —
# currently empty; every event ships via _seed_default_events() below instead,
# so it's editable/archivable like any other event from the start.
_BUILTIN_SLUGS = frozenset(EVENTS.keys())


# ---------------------------------------------------------------------------
# User-defined events (registered via the web UI)
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def is_builtin(slug: str) -> bool:
    return slug in _BUILTIN_SLUGS


def _event_to_dict(ev: Event) -> dict:
    return {
        "slug":            ev.slug,
        "name":            ev.name,
        "event_match":     list(ev.event_match),
        "filename_prefix": ev.filename_prefix,
        "topics":          list(ev.topics),
        "topic_keywords":  ev.topic_keywords,
        "foci":            list(ev.foci),
        "wiki_page":       ev.wiki_page,
        "archived":        ev.archived,
    }


def _dict_to_event(d: dict) -> Event:
    return Event(
        slug=d["slug"],
        name=d.get("name") or d["slug"],
        event_match=tuple(d.get("event_match") or ()),
        # Empty string lets __post_init__ derive from event_match[0]
        filename_prefix=d.get("filename_prefix") or "",
        topics=tuple(d.get("topics") or ("Other / General",)),
        topic_keywords=d.get("topic_keywords") or {},
        foci=tuple(d.get("foci") or ()),
        wiki_page=d.get("wiki_page") or "",
        archived=bool(d.get("archived", False)),
    )


def _save_custom_events() -> None:
    with _custom_events_lock:
        data = {"events": [_event_to_dict(ev) for slug, ev in EVENTS.items()
                           if slug not in _BUILTIN_SLUGS]}
        # Atomic write: tempfile + os.replace so a crashed half-write never
        # leaves the JSON unreadable.
        tmp = CUSTOM_EVENTS_FILE.with_suffix(CUSTOM_EVENTS_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _os.replace(tmp, CUSTOM_EVENTS_FILE)


def _load_custom_events() -> None:
    if not CUSTOM_EVENTS_FILE.exists():
        return
    try:
        data = json.loads(CUSTOM_EVENTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    for entry in data.get("events") or []:
        slug = (entry.get("slug") or "").strip().lower()
        if not slug or slug in EVENTS:
            continue
        try:
            EVENTS[slug] = _dict_to_event({**entry, "slug": slug})
        except Exception:
            continue


_DEFAULT_EVENT_SEEDS: tuple[dict, ...] = (
    dict(slug="circuit_lab", name="Circuit Lab", event_match=["circuit lab"],
         topics=list(_CIRCUIT_LAB_TOPICS), topic_keywords=_CIRCUIT_LAB_KEYWORDS),
    dict(slug="thermodynamics", name="Thermodynamics", event_match=["thermodynamics"],
         topics=list(_THERMO_TOPICS), topic_keywords=_THERMO_KEYWORDS),
)


def _seed_default_events() -> None:
    """Registers the default events (with their curated topic_keywords) into
    the normal custom-event registry on first run, or whenever
    events_custom.json doesn't already have them. After this they're
    indistinguishable from any other event: editable, archivable, persisted
    normally. Never overwrites a slug that's already present, so a prior
    edit or archive (already loaded by _load_custom_events() by the time
    this runs) is never clobbered."""
    added = False
    for seed in _DEFAULT_EVENT_SEEDS:
        if seed["slug"] in EVENTS:
            continue
        EVENTS[seed["slug"]] = _dict_to_event(seed)
        added = True
    if added:
        _save_custom_events()


def add_custom_event(
    slug: str,
    name: str,
    filename_prefix: str = "",
    event_match: list[str] | None = None,
    wiki_page: str = "",
    topics: list[str] | None = None,
    topic_keywords: dict | None = None,
    foci: list[str] | None = None,
) -> Event:
    """Register a new event at runtime and persist it to events_custom.json.

    `filename_prefix` is optional. When omitted (or empty), Event.__post_init__
    derives it from event_match[0] by stripping spaces and lowercasing — that
    matches scioly.org's PDF naming convention for the vast majority of events.
    Provide an explicit prefix only when the PDFs on scioly.org are named
    differently than the event itself (e.g. Anatomy & Physiology → "anatomy").
    """
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError(
            "slug must start with a letter and contain only "
            "lowercase letters, digits, and underscores"
        )
    if slug in EVENTS:
        raise ValueError(f"slug {slug!r} is already registered")
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    filename_prefix = (filename_prefix or "").strip().lower()
    if filename_prefix and (" " in filename_prefix or "/" in filename_prefix):
        raise ValueError("filename_prefix must contain no spaces or slashes")

    # Always ensure "Other / General" is present so classify_topic() has a home
    topics = list(topics or [])
    topics = [t.strip() for t in topics if t and t.strip()]
    if "Other / General" not in topics:
        topics.append("Other / General")

    foci_clean = tuple(f.strip() for f in (foci or []) if f and f.strip())
    event_match_t = tuple((s or "").strip().lower() for s in (event_match or []) if (s or "").strip())
    # When filename_prefix wasn't supplied, we still need *something* on disk to
    # match against. Empty event_match + empty prefix is fine — Event.__post_init__
    # falls back to the slug, which works for upload-only events.
    ev = Event(
        slug=slug,
        name=name,
        event_match=event_match_t,
        filename_prefix=filename_prefix,
        topics=tuple(topics),
        topic_keywords=topic_keywords or {},
        foci=foci_clean,
        wiki_page=(wiki_page or "").strip(),
    )
    EVENTS[slug] = ev
    _save_custom_events()
    return ev


def _set_archived(slug: str, archived: bool) -> None:
    if slug in _BUILTIN_SLUGS:
        raise ValueError("cannot archive a built-in event")
    if slug not in EVENTS:
        raise ValueError(f"event {slug!r} not registered")
    import dataclasses
    EVENTS[slug] = dataclasses.replace(EVENTS[slug], archived=archived)
    _save_custom_events()


def archive_custom_event(slug: str) -> None:
    """Hide a user-added event from the landing page. The web app never
    deletes the event's directory/PDFs/state file on disk — this only flips
    a flag in events_custom.json, fully reversible via unarchive_custom_event.
    Built-ins cannot be archived."""
    _set_archived(slug, True)


def unarchive_custom_event(slug: str) -> None:
    """Reverse archive_custom_event."""
    _set_archived(slug, False)


# Load any events that were registered in a previous run, then fill in the
# default events if they're not already present (e.g. first run, or a fresh
# clone with no events_custom.json yet). Load-then-seed in this order so a
# previously-saved edit or archive always wins over re-seeding the pristine
# default.
_load_custom_events()
_seed_default_events()
