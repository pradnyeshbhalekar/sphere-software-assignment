# Address Parser — Notes

`parse_addresses.py` reads `addresses.csv` and writes `parsed_addresses.csv`
with `building_name, street, area, city, pin_code, flag, reason` (plus the
original `id`/`address` for traceability). It's a rule-based parser: regex +
keyword lists + a small locality gazetteer, no ML, no lookups against the ID
or row position. Run it with:

```
python3 parse_addresses.py addresses.csv parsed_addresses.csv
```

## 1. What "correct" should mean for ambiguous cases

For this data, I think the right output is the **most conservative reading
that is still useful**, not the most complete one. Concretely:

- If a field has one plausible reading and no competing reading, fill it in
  even if the source has a typo (e.g. "cuff parade" → `Cuffe Parade`,
  "Jogeshwar West" → `Jogeshwari West`). Correcting an obvious hand-typing
  slip is not the same as inventing a fact.
- If a field has *no* textual signal at all (e.g. no locality name anywhere
  in the string), leave it blank rather than defaulting to something
  plausible-sounding. `C01` ("47, mumbai") has no way to know the building,
  street, area, or pin — filling any of them would be a guess dressed up as
  an answer.
- If a field has *two competing* candidates and no way to tell which is
  correct (`C07` mixes "Sapphire Isle" with a company name "Brightpath
  Technologies Pvt Ltd" and a second building "Silverline Garden Estate"),
  the correct output is blank + flagged, not "pick the first one and hope."
  A wrong-but-confident value is worse for downstream use (geocoding,
  delivery) than an honest blank.
- Partial/abbreviated pins (`Mumbai-71` for what is presumably `400071`) are
  treated as **missing**, not silently expanded. Assuming the `400` prefix
  is a reasonable guess for Mumbai specifically, but it's still a guess, and
  the brief for this exercise is explicit that fields should never be
  invented — so I flag it with the partial digits explained in the reason
  rather than reconstructing it.
- Street is treated differently from the other four fields: a **missing
  street is not, by itself, a parsing failure**. Most Indian residential
  addresses are fully specified by building + area + city + pin, with no
  street name at all (`A01`, `A03`, `A05`, `A07`, `A08` in this data are
  genuine, deliverable addresses with no street mentioned). Flagging every
  street-less row would flag the majority of *good* rows.

## 2. Additional fields worth extracting

Given more time/budget, I'd add:

- **Landmark** — "Opp Harbour View", "Near Radio Club", "Landmark: Frosty
  Cones Ice Cream Lane" appear constantly in this data and are genuinely
  useful for last-mile delivery in India, where landmarks often locate a
  place better than the formal address does. The parser already detects and
  strips an explicit `Landmark:` marker; it's one line of code to keep it as
  its own column instead of discarding it.
- **Floor/unit/flat number** — separated from the building name (`Flat
  no.57`, `A-1203`, `k wing`). Right now this gets stripped out and thrown
  away as noise so it doesn't pollute the building field; for a real
  delivery use case you'd want to keep it, not delete it.
- **Sub-locality vs. city-zone** — "Bandra **West**" vs. "Bandra" — worth
  splitting the directional suffix into its own flag/column, since it's
  operationally significant (west/east are different postal zones) and
  currently gets folded into the area string.
- **State** — every address here is implicitly Maharashtra/Mumbai, but a
  general-purpose version of this parser (beyond this one city) would need
  a state field, since pin codes alone don't disambiguate across states as
  cleanly as one might assume with hand-typed data.
- **Confidence score per field**, not just a row-level flag — e.g. area
  found via exact gazetteer match vs. fuzzy match vs. positional fallback
  are not equally trustworthy, and a downstream consumer might want to
  treat them differently (e.g. auto-accept exact matches, queue fuzzy ones
  for human review).

## 3. The flag vs. parse rule

A row is flagged (`flag = yes`) when **any** of the following holds:

1. **Building name is blank** — no keyword-based, gazetteer-based, or
   positional signal for a building/society name could be found anywhere
   in the string.
2. **Area is blank** — no known locality (exact or fuzzy gazetteer match)
   and no safe positional fallback either.
3. **Pin code is missing or incomplete** — no 6-digit code found, or only a
   truncated/abbreviated form was present (we refuse to expand it).
4. **City is missing** — "Mumbai" (or a recognizable variant) doesn't
   appear in the string at all.
5. **Conflicting-address signal** — two or more independent
   building/entity references are detected (e.g. two separate
   building-type keywords plus a company name), which usually means two
   different addresses/records got concatenated into one field.
6. **Too few identifiable tokens** — after stripping the city and pin, the
   remaining text has fewer than 3 meaningful words (`C01`: "47" is the
   only content word left).

Everything else (most importantly, a **missing street**) does not by
itself trigger a flag — see the reasoning in section 1. When a row *is*
flagged for one of the above reasons, the `reason` column lists every
missing/unreliable field that applies, not just the one that tripped the
flag, so a human reviewing the CSV can see the full picture at once.

This gives, on the 30-row sample: 18 rows parsed cleanly (`flag = no`) and
12 flagged, roughly matching the "easy / medium / hard" split already
implied by the difficulty column — without the parser ever looking at that
column or the ID prefix to get there.

## 4. What changes at 20,000 rows instead of 30

- **The gazetteer becomes the bottleneck, not the regex.** With 30 rows I
  could eyeball every locality name and hand-verify the list. At 20k rows
  you'd want a real, maintained gazetteer (e.g. India Post's official
  Mumbai pin-code/locality dataset) rather than ~40 hardcoded strings, and
  you'd want to log every row where the *only* area signal was a fuzzy
  match below some similarity threshold, so those can be spot-checked or
  fed back into the gazetteer.
- **Fuzzy matching needs to be constrained harder.** `difflib` at n=20k
  addresses will start producing false positives — short, common words
  fuzzy-matching into locality names by coincidence. I'd tighten the cutoff,
  require a minimum token length, and/or restrict fuzzy matching to
  candidates already narrowed by pin code (a pin code narrows "which
  localities are even possible" to a handful, which makes fuzzy area
  matching much safer and lets you cross-validate area against pin instead
  of trusting either alone).
- **Performance**: the current script is O(rows × gazetteer size ×
  window sizes) for the fuzzy pass — fine for 30 rows, fine for 20k too
  actually (gazetteer is small), but if the gazetteer grows to thousands of
  entries (national scale) you'd want a proper fuzzy-lookup index (e.g.
  a trigram index or `rapidfuzz` with pre-built candidate blocks) instead of
  `difflib.get_close_matches` over the whole list per window.
- **Flag rate becomes a metric to watch, not just a per-row output.** At
  30 rows, "12 flagged" is a number you can read in one sitting. At 20k,
  you'd want to bucket the flagged rows by `reason` and track the
  distribution over time — a spike in "multiple addresses jammed together"
  might mean an upstream form/CSV-merge bug, which is a different problem
  than the parser being weak on that pattern.
- **Manual review changes shape.** With 30 rows a human can review every
  flagged row individually. At 20k you'd sample-review by reason bucket,
  and probably build a small feedback loop where corrections to flagged
  rows get used to expand the gazetteer/keyword lists rather than being
  thrown away after one-off review.
- **The "too few tokens" threshold and building/street keyword lists should
  be tuned on a held-out sample**, not eyeballed against the training set,
  once there's enough data to split one out — at 30 rows there's no
  meaningful train/test split possible, so the current thresholds are
  necessarily judgment calls rather than validated ones.
