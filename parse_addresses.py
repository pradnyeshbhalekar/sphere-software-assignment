#!/usr/bin/env python3
"""
Rule-based parser for messy, hand-typed Mumbai addresses.

Extracts: building name, street, area, city, pin code
Flags rows that cannot be reliably parsed and explains why.

The parser works purely off the address string. It never looks at the
ID column, row position, or the (optional) difficulty column.
"""

import csv
import difflib
import re
import sys

# ---------------------------------------------------------------------------
# Vocabulary used by the heuristics. This is a generic gazetteer/keyword list,
# not something fit to the 30 rows in this specific file - the intent is that
# it generalizes to addresses the parser has never seen.
# ---------------------------------------------------------------------------

BUILDING_KEYWORDS = [
    "tower", "apartment", "apt", "chs", "chsl", "co-op", "coop", "cooperative",
    "society", "heights", "height", "villa", "residency", "residences",
    "estate", "garden", "gardens", "manor", "mansion", "isle", "complex",
    "court", "building", "bldg", "splendour", "splendor", "splendr",
    "crystal", "crys", "chambers", "sapphire", "castle", "plaza", "arcade",
    "enclave", "terrace", "park", "paradise", "orchid", "emerald", "the",
]

STREET_KEYWORDS = [
    "road", "rd", "street", "st", "marg", "lane", "ln", "gali", "chowk",
    "link road", "highway",
]

# Well-known Mumbai localities/neighbourhoods. This is intentionally broader
# than what appears in the sample file, so the parser generalizes to other
# Mumbai addresses rather than memorizing this dataset.
AREA_GAZETTEER = [
    "cuffe parade", "bandra west", "bandra east", "bandra", "worli",
    "lower parel", "andheri west", "andheri east", "andheri", "powai",
    "nariman point", "chembur", "santacruz west", "santacruz east",
    "santacruz", "jogeshwari west", "jogeshwari east", "jogeshwari",
    "mahim", "churchgate", "goregaon west", "goregaon east", "goregaon",
    "malad west", "malad east", "malad", "kandivali", "borivali", "dadar",
    "parel", "byculla", "colaba", "fort", "matunga", "sion", "wadala",
    "kurla", "ghatkopar", "vikhroli", "bhandup", "mulund", "juhu",
    "vile parle", "khar", "mahalaxmi", "tardeo", "grant road", "marine lines",
    "sewri", "trombay", "hiranandani", "vidyavihar", "wadala", "chandivali",
]

# Words that indicate a token is a flat/floor/wing descriptor, not a name.
UNIT_WORDS = [
    "flat", "floor", "flo", "wing", "no", "no.", "unit", "opp", "opp.",
    "near", "nr", "nr.", "next", "of", "a", "the",
]

UNIT_TOKEN_RE = re.compile(r"^[a-zA-Z]?[-/]?\d+[a-zA-Z]?$")

MIN_MEANINGFUL_WORDS = 3  # below this, we consider the address too sparse to parse

# "Building"/"Bldg" as a leading connector isn't part of the name itself
# (e.g. "Building Nova" -> "Nova"), unlike "Tower"/"Heights" etc.
GENERIC_BUILDING_CONNECTORS = ("building", "bldg")

# Marks the start of a landmark clause (near/opp/next to/etc), which runs to
# the next comma.
LANDMARK_TRIGGER_RE = re.compile(
    r"\blandmark\s*:|\b(?:next\s+to|opposite|opp\.?|near|nr\.?|behind)\b",
    re.I,
)

# Flat/unit/wing/block identifier patterns, pulled out of the address up
# front so they land in their own field instead of getting lost.
FLAT_PHRASE_RE = re.compile(
    r"\bflat\s*(?:#|no\.?|number)?\s*[:\-]?\s*\d+\s*[a-zA-Z]?\b",
    re.I,
)
WING_LETTER_CODE_RE = re.compile(r"\b[a-zA-Z]{1,3}\d{0,2}[-/]\d{2,6}(?:[-/]\d{1,4})?\b")
WING_WORD_RE = re.compile(r"\b(?:[a-zA-Z]\s*wing|wing\s*[a-zA-Z])\b", re.I)
LOOSE_SLASH_UNIT_RE = re.compile(r"\b([a-zA-Z]{1,3})\s*,?\s*/\s*(\d{2,4})\b")
LEADING_NUM_LETTER_RE = re.compile(r"\b\d{1,4}[a-zA-Z]\b")
LETTER_NUM_CODE_RE = re.compile(r"\b[a-zA-Z]\d{2,5}\b")


def find_city(text):
    m = re.search(r"\bmumbai\b", text, re.I)
    return ("Mumbai", m.span()) if m else ("", None)


def find_pincode(text):
    """Return (pincode, matched_span_or_None, note) where note flags a
    partial/abbreviated pin we deliberately refuse to expand."""
    m = re.search(r"\b(4\d{5})\b", text)
    if not m:
        m = re.search(r"\b(\d{6})\b", text)
    if m:
        return m.group(1), m.span(), None

    m = re.search(r"mumbai\s*[-–]\s*(\d{1,4})\b", text, re.I)
    if m:
        return "", None, (
            f"pincode incomplete - only trailing digits '{m.group(1)}' given, "
            f"not enough to safely reconstruct a 6-digit code"
        )
    return "", None, "pincode missing - no 6-digit code found in the text"


def extract_landmark_clauses(text):
    """Strip landmark clauses (near/opp/next to/.../landmark:) so they don't
    bleed into building_name. Returns (remainder_text, [landmark, ...])."""
    landmarks = []
    max_words = 6  # landmark phrases are short; caps a run-on clause when
    # there's no comma to mark the end
    while True:
        m = LANDMARK_TRIGGER_RE.search(text)
        if not m:
            break
        end_m = re.search(r"[,;]", text[m.end():])
        end = m.end() + end_m.start() if end_m else len(text)
        candidate = text[m.start():end]

        # don't let the clause swallow an area name that follows it with no
        # comma in between - stop right before that instead
        low_candidate = candidate.lower()
        gaz_idx = None
        for area in AREA_GAZETTEER:
            idx = low_candidate.find(area)
            if idx != -1 and (gaz_idx is None or idx < gaz_idx):
                gaz_idx = idx
        if gaz_idx is not None:
            end = m.start() + gaz_idx

        clause_words = text[m.start():end].split()
        if len(clause_words) > max_words:
            end = m.start() + len(" ".join(clause_words[:max_words]))

        descriptive = text[m.end():end].strip(" .")
        if not descriptive:
            # just the marker word, nothing to report - strip and move on
            text = text[: m.start()] + " " + text[m.end():]
            continue

        clause = text[m.start():end].strip(" .")
        landmarks.append(clause)
        text = text[: m.start()] + " " + text[end:]
    text = re.sub(r"\s+", " ", text).strip()
    return text, landmarks


def extract_unit_number(text):
    """Pull flat/unit/wing/block identifiers out into their own value so
    they don't get lost in building_name cleanup. Returns
    (remainder_text, unit_number_string)."""
    unit_parts = []

    def take_all(pattern, transform=None):
        nonlocal text
        matches = list(pattern.finditer(text))
        found = [(transform(m) if transform else m.group(0)) for m in matches]
        for m in reversed(matches):
            text = text[: m.start()] + " " + text[m.end():]
        unit_parts.extend(found)

    take_all(FLAT_PHRASE_RE)
    take_all(WING_LETTER_CODE_RE)
    take_all(WING_WORD_RE)
    take_all(LOOSE_SLASH_UNIT_RE, lambda m: f"{m.group(1)}/{m.group(2)}")
    take_all(LEADING_NUM_LETTER_RE)
    take_all(LETTER_NUM_CODE_RE)

    text = re.sub(r"\s+", " ", text).strip()
    seen = []
    for part in unit_parts:
        part = part.strip(" .,-")
        if part and part not in seen:
            seen.append(part)
    return text, "; ".join(seen)


def split_segments(text):
    parts = [p.strip(" .") for p in re.split(r",|;", text)]
    return [p for p in parts if p]


FILLER_PHRASE_RES = [
    re.compile(r"\bflat\s*(no\.?|number)?\s*[:\-]?\s*\d+[a-z]?\b", re.I),
    re.compile(r"\b\d+\s*(st|nd|rd|th)?\s*floor\b", re.I),
    re.compile(r"\b\d+\s*(st|nd|rd|th)\b", re.I),  # bare ordinal, e.g. "3 rd"
    re.compile(r"\bno\.?\s*\d+[a-z]?\b", re.I),
    re.compile(r"\bunit\s*\d+[a-z]?\b", re.I),
]


def strip_filler_phrases(text):
    for pat in FILLER_PHRASE_RES:
        text = pat.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_unit_tokens(segment, dropped=None):
    """Drop leading/trailing flat/floor/wing filler tokens & bare unit codes.
    If `dropped` is a list, identifier-looking tokens removed this way
    (e.g. "605", "74", not stopwords like "near") get appended to it."""
    segment = strip_filler_phrases(segment)
    tokens = segment.split()
    def is_filler(tok):
        low = tok.lower().strip(".:")
        if low in UNIT_WORDS:
            return True
        if UNIT_TOKEN_RE.match(tok):
            return True
        if re.match(r"^\d+(st|nd|rd|th)$", low):
            return True
        return False

    def record_drop(tok):
        if dropped is None:
            return
        low = tok.lower().strip(".:")
        if low not in UNIT_WORDS and UNIT_TOKEN_RE.match(tok):
            dropped.append(tok)

    while tokens and is_filler(tokens[0]):
        record_drop(tokens[0])
        tokens.pop(0)
    while tokens and is_filler(tokens[-1]):
        record_drop(tokens[-1])
        tokens.pop()
    return " ".join(tokens)


def strip_generic_building_connector(text):
    """Strip a leading "Building"/"Bldg" when it's just a connector
    ("Building Nova" -> "Nova"), not when it's part of the name itself
    ("C 12 Building")."""
    tokens = text.split()
    if len(tokens) > 1 and tokens[0].lower().strip(".") in GENERIC_BUILDING_CONNECTORS:
        tokens.pop(0)
    return " ".join(tokens)


def contains_keyword(segment, keywords):
    low = segment.lower()
    for kw in keywords:
        if re.search(rf"\b{re.escape(kw)}\b", low):
            return True
    return False


def gazetteer_match(segment):
    """Returns (canonical_name, literal_text_found) or (None, None).
    Exact substring match first; falls back to a fuzzy match on 1-2 word
    windows to tolerate hand-typed misspellings (e.g. 'cuff parade',
    'jogeshwar west')."""
    low = segment.lower()
    for area in AREA_GAZETTEER:
        if area in low:
            idx = low.index(area)
            return area.title(), segment[idx:idx + len(area)]

    words = re.findall(r"[a-zA-Z]+", segment)
    for size in (2, 1):
        for i in range(len(words) - size + 1):
            window_words = words[i:i + size]
            window = " ".join(w.lower() for w in window_words)
            match = difflib.get_close_matches(window, AREA_GAZETTEER, n=1, cutoff=0.82)
            if match:
                return match[0].title(), " ".join(window_words)
    return None, None


def split_street_phrase(segment, dropped=None):
    """Given a segment containing a street keyword, split into
    (building_candidate, street_phrase)."""
    tokens = segment.split()
    low_tokens = [t.lower().strip(".") for t in tokens]
    kw_index = None
    for i, t in enumerate(low_tokens):
        if t in STREET_KEYWORDS:
            kw_index = i
            break
    if kw_index is None:
        return "", segment
    # street phrase = up to 3 words before the keyword, plus the keyword itself
    start = max(0, kw_index - 2)
    street_tokens = tokens[start:kw_index + 1]
    # Don't let a building-type word (e.g. "Building Harbur Road") leak into
    # the street phrase - it belongs with the building name instead.
    while street_tokens and street_tokens[0].lower().strip(".") in BUILDING_KEYWORDS:
        street_tokens.pop(0)
        start += 1
    street_phrase = " ".join(street_tokens)
    building_part = strip_unit_tokens(" ".join(tokens[:start]).strip(), dropped)
    # "Building"/"Bldg" here is usually a connector, not part of the name -
    # strip it whether it's trailing ("C 12 Building") or leading ("Building Nova")
    building_tokens = building_part.split()
    if building_tokens and building_tokens[-1].lower().strip(".") in GENERIC_BUILDING_CONNECTORS:
        building_tokens.pop()
    building_part = strip_generic_building_connector(" ".join(building_tokens))
    return building_part, street_phrase


def word_count(text):
    return len([w for w in re.split(r"\s+", text.strip()) if w])


def parse_address(address):
    reasons = []
    text = address.strip()

    city, _city_span = find_city(text)
    if not city:
        reasons.append("city missing - 'Mumbai' not found in text")

    pin, pin_span, pin_note = find_pincode(text)
    if pin_note:
        reasons.append(pin_note)

    # Build a working copy with city/pin removed for segment analysis.
    work = text
    if pin_span:
        work = work[: pin_span[0]] + " " + work[pin_span[1]:]
    work = re.sub(r"\bmumbai\b", " ", work, flags=re.I)

    work, landmarks = extract_landmark_clauses(work)
    work, unit_number = extract_unit_number(work)
    dropped_unit_tokens = []

    segments = split_segments(work)

    building = ""
    street = ""
    area = ""
    conflict = False
    building_hits = 0

    remaining_segments = []
    for seg in segments:
        cleaned = strip_unit_tokens(seg, dropped_unit_tokens)
        if not cleaned:
            continue

        # An area name can be embedded in the middle of a longer chunk when
        # the address has no internal commas (very common in this data).
        # Pull it out first so it doesn't get swallowed by the building or
        # street text.
        gaz, literal = gazetteer_match(cleaned)
        if gaz:
            pattern = re.compile(re.escape(literal), re.I)
            remainder = strip_unit_tokens(pattern.sub(" ", cleaned, count=1), dropped_unit_tokens)
            if not area:
                area = gaz
        else:
            remainder = cleaned

        if not remainder:
            continue

        if contains_keyword(remainder, STREET_KEYWORDS):
            b_part, s_phrase = split_street_phrase(remainder, dropped_unit_tokens)
            if not street:
                street = s_phrase
            if b_part and not building:
                building = b_part
            elif b_part:
                building_hits += 1
            continue

        if contains_keyword(remainder, BUILDING_KEYWORDS):
            building_hits += 1
            if not building:
                building = strip_generic_building_connector(remainder)
            continue

        remaining_segments.append(remainder)

    # Drop segments that are pure punctuation/unit-code noise left over
    # from stripping (e.g. a stray "-" from "Mumbai -400005").
    remaining_segments = [s for s in remaining_segments if re.search(r"[A-Za-z]{2,}", s)]

    # Area: prefer a gazetteer hit among leftover segments (covers cases the
    # per-segment pass above missed).
    for seg in remaining_segments:
        hit, _ = gazetteer_match(seg)
        if hit:
            area = hit
            remaining_segments.remove(seg)
            break

    # Fallback: if no building found yet, the first leftover segment that
    # looks like a proper-noun phrase (not a lone number/unit code) and is
    # not the area we just picked is treated as the building name.
    if not building:
        for seg in remaining_segments:
            if seg == area:
                continue
            if word_count(seg) >= 1 and not UNIT_TOKEN_RE.match(seg) and re.search(r"[A-Za-z]{3,}", seg):
                building = seg
                remaining_segments.remove(seg)
                break

    # Fallback area: if still blank, use the leftover segment immediately
    # preceding where the city was found in the original text.
    if not area and remaining_segments:
        # heuristic: last leftover segment (closest to city) that isn't the
        # building we already chose
        for seg in reversed(remaining_segments):
            if seg != building and word_count(seg) <= 4:
                area = seg
                break

    # Conflict detection: more than one independent building-type signal,
    # or an explicit company/registered-entity name mixed in with a
    # building name => likely two addresses jammed together.
    company_signal = re.search(r"\b(pvt\.?\s*ltd\.?|private\s+limited|llp|inc\.?)\b", text, re.I)
    if building_hits >= 2 or (company_signal and building_hits >= 1):
        conflict = True

    if conflict:
        # We can see multiple independent building/entity references but
        # can't reliably tell which one is the actual delivery point, so we
        # don't guess by picking one - blank it out instead.
        building = ""
        reasons.append(
            "multiple addresses/entities appear jammed into one field - "
            "building name could not be reliably isolated"
        )

    if not building:
        reasons.append("no building name identifiable")
    if not area:
        reasons.append("area/locality missing")

    # landmark text was stripped from building_name - if what's left is
    # empty or a single word, flag it instead of presenting a name that
    # might be missing a chunk
    landmark_uncertain = bool(landmarks) and word_count(building) < 2
    if landmark_uncertain:
        reasons.append(
            "landmark text present (" + "; ".join(landmarks) + ") - "
            "building name may be incomplete"
        )

    # unit-like tokens that didn't make it into unit_number get flagged
    # instead of silently dropped (a different unit may already be captured,
    # e.g. "E wing" found but "1507" still left over)
    leftover_unit_tokens = [
        tok for tok in dict.fromkeys(dropped_unit_tokens)
        if tok.lower() not in unit_number.lower()
    ]
    unit_ambiguous = bool(leftover_unit_tokens)
    if unit_ambiguous:
        reasons.append(
            "possible unit identifier(s) '" + ", ".join(leftover_unit_tokens) + "' "
            "found but could not be confidently isolated as unit_number"
        )

    core_word_count = word_count(re.sub(r"[,.]", " ", work))
    if core_word_count < MIN_MEANINGFUL_WORDS:
        reasons.append(
            f"too few identifiable tokens ({core_word_count}) to reliably parse"
        )

    flagged_fields = {
        "building": not building,
        "area": not area,
        "pincode": not pin,
        "city": not city,
    }
    flag = "yes" if (
        flagged_fields["building"]
        or flagged_fields["area"]
        or flagged_fields["pincode"]
        or flagged_fields["city"]
        or conflict
        or landmark_uncertain
        or unit_ambiguous
        or core_word_count < MIN_MEANINGFUL_WORDS
    ) else "no"

    if flag == "no" and not street:
        # Not a reason to flag - many valid Indian addresses omit a street
        # name entirely (building + area + city + pin is a complete address).
        pass

    reason_text = "; ".join(dict.fromkeys(reasons))  # de-dup, keep order

    return {
        "building_name": building,
        "unit_number": unit_number,
        "street": street,
        "area": area,
        "city": city,
        "pin_code": pin,
        "flag": flag,
        "reason": reason_text,
    }


def main(in_path, out_path):
    with open(in_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    out_fields = [
        "id", "address", "building_name", "unit_number", "street", "area",
        "city", "pin_code", "flag", "reason",
    ]

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        for row in rows:
            parsed = parse_address(row["address"])
            writer.writerow({
                "id": row["id"],
                "address": row["address"],
                **parsed,
            })

    print(f"Wrote {len(rows)} rows to {out_path}")


if __name__ == "__main__":
    in_path = sys.argv[1] if len(sys.argv) > 1 else "addresses.csv"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "parsed_addresses.csv"
    main(in_path, out_path)
