"""Normalisation helpers so 'Maple Grove Rd., Ste 2' and 'maple grove road' compare equal."""
import re
from difflib import SequenceMatcher

STREET_WORDS = {
    "street": "st", "str": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr",
    "boulevard": "blvd", "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy",
    "highway": "hwy", "circle": "cir", "terrace": "ter", "trail": "trl", "square": "sq",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}
UNIT_RE = re.compile(r"(\b(suite|ste|unit|apt|bldg|building)\b|#)\s*[\w-]+$")

# Words that say what kind of place it is, not which place it is.
GENERIC_NAME_WORDS = {
    "the", "of", "at", "and", "a", "an", "bellhaven", "senior", "living", "community", "communities",
    "assisted", "memory", "care", "independent", "skilled", "nursing", "rehab", "rehabilitation",
    "center", "centre", "residence", "residences", "home", "homes", "retirement", "village",
    "llc", "inc", "corp", "co", "facility", "health", "healthcare",
}

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}


def clean(text):
    text = (text or "").lower().replace("&", " and ")
    text = re.sub(r"[^\w\s#-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def street(address):
    """'123 N. Maple Grove Road, Suite 4' -> '123 n maple grove rd'."""
    text = clean((address or "").split(",")[0])
    text = UNIT_RE.sub("", text).strip()
    return " ".join(STREET_WORDS.get(w, w) for w in text.replace("-", " ").split())


def street_number(address):
    m = re.match(r"\s*(\d+)", address or "")
    return m.group(1) if m else ""


def zip5(z):
    m = re.search(r"\d{5}", str(z or ""))
    return m.group(0) if m else ""


def state(s):
    s = (s or "").strip()
    return STATES.get(s.lower(), s.upper()[:2]) if s else ""


def city(c):
    return clean(c).replace("saint ", "st ").replace("fort ", "ft ")


def name_core(name):
    """Distinctive part of a facility name: 'Bellhaven at Maple Grove Assisted Living' -> 'maple grove'."""
    words = [w for w in clean(name).replace("-", " ").split() if w not in GENERIC_NAME_WORDS]
    return " ".join(words)


def similarity(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def phone(p):
    digits = re.sub(r"\D", "", p or "")
    return digits[-10:] if len(digits) >= 10 else ""
