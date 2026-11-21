"""The keyword rules: a plain-English question -> a search plan, without any network.

This is what answers a question when the ARC LLM is switched off, has no key, or cannot be
reached (it only answers on the VT network or VPN). It is deterministic: the same question, the
same dictionary and the same `now` always give the same plan.

`plan(question, dictionary, now)` returns the plan language of `pcldb.ask` (see
`ask.compile_plan`):

    {"keywords": "...", "filters": [...], "sort": {...} or None, "limit": 10,
     "count_by": None or a field, "explanation": "..."}

What the rules recognise, in this order (each rule uses up the words it understood):

1. counting: "how many", "count", "number of"; "per / by / for each <field>" -> `count_by`;
2. dates -> a range on `started` (or on `uploaded` / `verified` when that word stands before
   the date): today, yesterday, this / last week, month, year, last N days (weeks, months,
   years, hours), N days ago, since / after / before / until a date, between two dates,
   2026-11-01, 11/01/2026, November, Nov 13, 13 November 2026, a weekday name;
3. "sample 130", "samples VS-001 and VS-002";
4. how many and in which order: "top 5", "last 3 sessions", "the latest session", newest,
   oldest, largest, smallest;
5. review words ("need review", "unreviewed", "reviewed") and other words with a fixed
   meaning: simulated / real, failed / partial / complete / incomplete, corrected, with or
   without problems, classified / unclassified, withdrawn, copies;
6. values the dictionary knows (instruments, labels, methods, tags, models, sample ids,
   locations), also
   typed with spaces for `_` or `-` and in any letter case; "not", "without", "except",
   "other than" or "everything but" before one turns it into an exclusion ("haven't" is read
   as "have not"); several labels joined by "and" must all be there;
7. a loose instrument name: one part of an instrument slug ("raman" for `raman-portable`);
8. what is left, minus stop words, becomes the keywords (every one of them must match); a
   word after a negation is left out instead (a keyword cannot exclude), with a notice.

Dates in a plan are local calendar days (`YYYY-MM-DD`); `ask.compile_plan` turns them into UTC.
"The last N hours" is exact instead: a UTC time (`YYYY-MM-DDTHH:MM:SSZ`), N hours before now.
"""

from __future__ import annotations

import calendar
import datetime
import re

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_FILTERS = 8
MAX_KEYWORD_CHARS = 200
MAX_VALUES = 20
# Days outside these years are not taken for dates (a lab's sessions are not that old, and the
# first days of 1970 cannot be turned into UTC on every system).
_MIN_DAY = datetime.date(1971, 1, 1)
_MAX_DAY = datetime.date(2200, 12, 31)
MAX_ID_CHARS = 64         # a longer word after "sample" is not taken for an id

EXPLANATION = "Planned by the keyword rules on this computer, without the assistant."

# Fields whose known values are looked for in the question, most specific first: when one word
# is a known value of two fields, the first field here gets it.
VALUE_FIELDS = ("instrument", "labels", "sample_ids", "method", "tags", "models", "completion",
                "location")
# Fields that hold a list: several values joined by "and" mean "all of them".
_ALL_FIELDS = ("labels", "tags", "models")

_TOKEN = re.compile(r"[^\W_]+(?:[._\-/][^\W_]+)*")
_SPLIT = re.compile(r"[._\-/\s]+")

_STOP_WORDS = frozenset("""
a about again all also am an and any are as at be been being both but by can could did do does
done during each either every for from had has have having her here his how i if in into is it
its just me my no nor not of on only or other our out over own please same she should so some
such than that the their them then there these they this those through to too under up us very
was we were what when where which while who whom whose why will with within without would you
your s t d m re ve ll may might must shall most least more less

show shows list lists find finds get give display tell see want wanted need needs looking look
search fetch pull bring return print know like got view
session sessions scan scans run runs experiment experiments data dataset datasets measurement
measurements record records result results file files entry entries thing things one ones item
items sample samples specimen label labels labelled labeled instrument instruments tag tags
tagged method methods mode modes model models id ids name named called state status kind type
made make taken took recorded measured collected acquired captured performed started ran
uploaded upload uploads using used use contain contains containing include includes including
found detected detect detects identified identify identifies showed shown showing appear appears
appeared appearing present seen saw observed observe spotted matched matching mention mentions
mentioned involve involves involved involving related regarding concerning gave giving hold
holds held imaged
whats grouped group split broken down breakdown everything anything something
many much count counts number total tally per
latest newest recent recently last first oldest earliest new old past previous ago since after
before until till between today yesterday week weeks month months year years day days hour hours
time date dates currently now still yet ever far
""".split())

_NEGATIONS = frozenset({"not", "without", "except", "excluding", "exclude", "no", "non", "minus"})
# "but" excludes only after these words: "everything but raman", "all sessions but sem".
_BUT_LEADS = frozenset({"everything", "anything", "all", "every"})
# "haven't" -> "have not": contractions are spelled out before the question is cut into words.
_IRREGULAR_NOT = re.compile(r"\b(can|won|shan)['\u2019]t\b|\b(can)(not)\b", re.IGNORECASE)
_NOT_CONTRACTION = re.compile(r"\b([^\W_]+)n['\u2019]t\b", re.IGNORECASE)
_IRREGULAR_STEMS = {"can": "can", "won": "will", "shan": "shall"}
_FILLERS = frozenset({"the", "a", "an", "any", "all", "that", "are", "is", "were", "was", "been",
                      "be", "being", "of", "which", "who", "have", "has", "yet", "still", "as",
                      "do", "does", "did"})
# Words that may stand between "not" and the thing: "do not contain adenine".
_LINKS = frozenset({"contain", "contains", "containing", "include", "includes", "including",
                    "having", "with", "labelled", "labeled", "tagged", "from", "on", "using",
                    "showing", "show", "shows", "showed", "shown", "found", "detected",
                    "identified", "seen", "observed", "measured", "find", "detect", "identify"})

_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
                 "twenty": 20, "thirty": 30, "fifty": 50, "couple": 2}

_MONTHS = {}
for _number, _names in enumerate((
        "january jan", "february feb", "march mar", "april apr", "may", "june jun", "july jul",
        "august aug", "september sep sept", "october oct", "november nov", "december dec"), 1):
    for _name in _names.split():
        _MONTHS[_name] = _number
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6}

_ISO_DATE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_US_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_ISO_MONTH = re.compile(r"(\d{4})-(\d{2})")
_DAY = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?")
_YEAR = re.compile(r"(?:19|20)\d{2}")

_UNITS = {"day": "day", "days": "day", "week": "week", "weeks": "week", "month": "month",
          "months": "month", "year": "year", "years": "year", "hour": "hour", "hours": "hour"}

# A word before a date and what it does to the period [start, end] that follows.
# ("from" alone is exact: "sessions from yesterday" are yesterday's sessions.)
_LEAD_ONE = {"since": "from", "from": "exact", "starting": "from", "beginning": "from",
             "after": "after", "before": "before", "until": "until", "till": "until",
             "through": "until", "thru": "until", "between": "between",
             "in": "exact", "on": "exact", "during": "exact", "for": "exact", "of": "exact",
             "within": "exact", "over": "exact", "at": "exact"}
_LEAD_TWO = {("starting", "from"): "from", ("as", "of"): "from", ("later", "than"): "after",
             ("newer", "than"): "after", ("earlier", "than"): "before",
             ("older", "than"): "before", ("prior", "to"): "before", ("up", "to"): "until"}
_RANGE_JOINERS = frozenset({"to", "and", "until", "till", "through", "thru"})
_DATE_FIELD_WORDS = {"uploaded": "uploaded", "upload": "uploaded", "uploads": "uploaded",
                     "verified": "verified", "checked": "verified"}
_YEAR_LEADS = frozenset({"in", "during", "since", "from", "before", "after", "of", "for",
                         "until", "through", "between", "and", "to"})
_MAY_LEADS = _YEAR_LEADS | {"last", "this"}

# Words for a field, used after "per", "by", "for each" and after "how many".
_FIELD_WORDS = {
    "instrument": "instrument", "instruments": "instrument", "machine": "instrument",
    "machines": "instrument", "device": "instrument", "devices": "instrument",
    "label": "labels", "labels": "labels", "class": "labels", "classes": "labels",
    "substance": "labels", "substances": "labels", "compound": "labels", "compounds": "labels",
    "tag": "tags", "tags": "tags",
    "method": "method", "methods": "method", "mode": "method", "modes": "method",
    "sample": "sample_ids", "samples": "sample_ids",
    "model": "models", "models": "models",
    "completion": "completion", "status": "completion", "outcome": "completion",
    "outcomes": "completion",
    "format": "format", "formats": "format",
    "location": "location", "locations": "location", "place": "location", "room": "location",
    "operator": "operator", "operators": "operator", "person": "operator",
    "problem": "problems", "problems": "problems",
    "review": "review_state",
}
_FIELD_WORD_TAILS = frozenset({"id", "ids", "name", "names", "state", "states", "type", "types"})
_GROUP_ONE = frozenset({"per", "by", "across"})
_GROUP_TWO = frozenset({("for", "each"), ("for", "every"), ("grouped", "by"), ("group", "by"),
                        ("split", "by"), ("broken", "by"), ("down", "by")})

_SESSION_NOUNS_ONE = frozenset({"session", "scan", "run", "measurement", "experiment", "one",
                                "upload", "dataset", "record", "entry"})
_SESSION_NOUNS_MANY = frozenset({"sessions", "scans", "runs", "measurements", "experiments",
                                 "ones", "uploads", "datasets", "records", "entries", "results"})
_SORTS = {"newest": ("started", "desc"), "latest": ("started", "desc"),
          "recent": ("started", "desc"), "recently": ("started", "desc"),
          "oldest": ("started", "asc"), "earliest": ("started", "asc"),
          "largest": ("bytes", "desc"), "biggest": ("bytes", "desc"),
          "smallest": ("bytes", "asc")}
# These two are ordinary words too: they sort only when a number or a session word follows.
_SORTS_WEAK = {"last": ("started", "desc"), "first": ("started", "asc")}

_REVIEW_PENDING = (
    ("need", "to", "be", "reviewed"), ("needs", "to", "be", "reviewed"),
    ("waiting", "for", "review"), ("waiting", "for", "a", "review"),
    ("not", "yet", "reviewed"), ("not", "been", "reviewed"), ("to", "be", "reviewed"),
    ("need", "a", "review"), ("needs", "a", "review"),
    ("need", "review"), ("needs", "review"), ("needing", "review"), ("need", "reviewing"),
    ("needs", "reviewing"), ("awaiting", "review"), ("pending", "review"),
    ("review", "pending"), ("to", "review"), ("not", "reviewed"), ("unreviewed",),
)
_REVIEW_DONE = (("review", "done"), ("been", "reviewed"), ("reviewed",))

_SAMPLE_WORDS = frozenset({"sample", "samples", "specimen", "specimens"})
_SAMPLE_TAILS = frozenset({"id", "ids", "number", "numbers", "no", "nr", "named", "called"})

# word -> (field, op, values). A negation word before it flips any <-> none, exists <-> missing.
_SPECIAL = {
    "simulated": ("simulated", "any", [True]), "simulation": ("simulated", "any", [True]),
    "simulations": ("simulated", "any", [True]), "sim": ("simulated", "any", [True]),
    "sims": ("simulated", "any", [True]), "synthetic": ("simulated", "any", [True]),
    "non-simulated": ("simulated", "any", [False]), "nonsimulated": ("simulated", "any", [False]),
    "real": ("simulated", "any", [False]), "physical": ("simulated", "any", [False]),
    "failed": ("completion", "any", ["failed"]), "failure": ("completion", "any", ["failed"]),
    "fail": ("completion", "any", ["failed"]), "fails": ("completion", "any", ["failed"]),
    "finish": ("completion", "any", ["complete"]),
    "failures": ("completion", "any", ["failed"]), "crashed": ("completion", "any", ["failed"]),
    "partial": ("completion", "any", ["partial"]), "partially": ("completion", "any", ["partial"]),
    "stopped": ("completion", "any", ["partial"]), "aborted": ("completion", "any", ["partial"]),
    "interrupted": ("completion", "any", ["partial"]),
    "complete": ("completion", "any", ["complete"]),
    "completed": ("completion", "any", ["complete"]),
    "finished": ("completion", "any", ["complete"]),
    "successful": ("completion", "any", ["complete"]),
    "incomplete": ("completion", "none", ["complete"]),
    "unfinished": ("completion", "none", ["complete"]),
    "corrected": ("corrected", "any", [True]), "uncorrected": ("corrected", "any", [False]),
    "problem": ("problems", "exists", None), "problems": ("problems", "exists", None),
    "issue": ("problems", "exists", None), "issues": ("problems", "exists", None),
    "classified": ("labels", "exists", None), "unclassified": ("labels", "missing", None),
    "unlabelled": ("labels", "missing", None), "unlabeled": ("labels", "missing", None),
    "withdrawn": ("withdrawn", "any", [True]),
    "copy": ("is_copy", "any", [True]), "copies": ("is_copy", "any", [True]),
    "duplicate": ("is_copy", "any", [True]), "duplicates": ("is_copy", "any", [True]),
}
# A negation that no rule used up is reported (the results ignore it).
_UNUSED_NEGATIONS = frozenset({"not", "never", "without", "except", "excluding", "exclude"})
_FLIP = {"any": "none", "none": "any", "exists": "missing", "missing": "exists"}

_RESERVED = (_STOP_WORDS | _NEGATIONS | frozenset(_SPECIAL) | frozenset(_SORTS)
             | frozenset(_FIELD_WORDS) | frozenset(_MONTHS) | frozenset(_WEEKDAYS)
             | frozenset(_NUMBER_WORDS) | frozenset(_UNITS) | frozenset(_LEAD_ONE)
             | frozenset({"top", "limit", "reviewed", "unreviewed", "review", "most", "least"}))


# ---------------------------------------------------------------------- small helpers


class _Words:
    """The question as words; `used[i]` is set once a rule has understood word i."""

    def __init__(self, question):
        question = _spell_out_not(question)
        self.text = [match.group(0) for match in _TOKEN.finditer(question)]
        self.low = [word.casefold() for word in self.text]
        self.used = [False] * len(self.text)

    def __len__(self):
        return len(self.text)

    def free(self, index):
        return 0 <= index < len(self.low) and not self.used[index]

    def word(self, index):
        """The lower-case word at `index`, or "" when it is outside or already used."""
        return self.low[index] if self.free(index) else ""

    def use(self, *indexes):
        for index in indexes:
            if 0 <= index < len(self.used):
                self.used[index] = True

    def find(self, phrase):
        """Every start index where the free words equal `phrase` (a tuple of lower-case words)."""
        size = len(phrase)
        found = []
        index = 0
        while index + size <= len(self.low):
            if (tuple(self.low[index:index + size]) == phrase
                    and not any(self.used[index:index + size])):
                found.append(index)
                index += size
            else:
                index += 1
        return found


def _spell_out_not(question):
    """ "haven't" -> "have not", "can't" -> "can not": otherwise "haven't been reviewed" would
    read as "haven" + "t" + "been reviewed" and mean the opposite."""
    def irregular(match):
        if match.group(1):
            return _IRREGULAR_STEMS[match.group(1).lower()] + " not"
        return match.group(2) + " " + match.group(3)
    return _NOT_CONTRACTION.sub(r"\1 not", _IRREGULAR_NOT.sub(irregular, question))


def _atoms(text):
    return [part for part in _SPLIT.split(text.casefold()) if part]


def _number(word):
    if word.isdigit() and len(word) <= 4:
        return int(word)
    return _NUMBER_WORDS.get(word)


def _date(year, month, day):
    """The day, or None when there is no such day or it is outside the years the rules use."""
    try:
        found = datetime.date(year, month, day)
    except ValueError:
        return None
    return found if _MIN_DAY <= found <= _MAX_DAY else None


def _clamp(day):
    if isinstance(day, datetime.datetime):
        return day          # an exact moment ("the last 3 hours"): made from now, never extreme
    return min(max(day, _MIN_DAY), _MAX_DAY)


def _order(value):
    """Days and exact moments in one order: a day counts as its local midnight."""
    if isinstance(value, datetime.datetime):
        return value.replace(tzinfo=None)       # moments are made in local time
    return datetime.datetime.combine(value, datetime.time())


def _plan_text(value):
    """A day as YYYY-MM-DD (a local day); an exact moment as a UTC time."""
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value.isoformat()


def _month_period(year, month):
    if not 1 <= month <= 12 or not _MIN_DAY.year <= year <= _MAX_DAY.year:
        return None
    return (datetime.date(year, month, 1),
            datetime.date(year, month, calendar.monthrange(year, month)[1]))


def _year_period(year):
    if not _MIN_DAY.year <= year <= _MAX_DAY.year:
        return None
    return datetime.date(year, 1, 1), datetime.date(year, 12, 31)


def _months_back(day, count):
    year, month = divmod(day.year * 12 + (day.month - 1) - count, 12)
    month += 1
    if year < _MIN_DAY.year:
        return _MIN_DAY
    return datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _days_back(day, count):
    try:
        return _clamp(day - datetime.timedelta(days=count))
    except OverflowError:
        return _MIN_DAY


def _used(period, words_used, rolling=False):
    """A period with the number of words it took, or None when there is no such period."""
    return None if period is None else (period[0], period[1], words_used, rolling)


def _field_names(dictionary):
    """The names of the fields the dictionary lists, or None when it lists none."""
    fields = (dictionary or {}).get("fields")
    if not fields:
        return None
    if isinstance(fields, dict):
        return set(fields)
    return {item["name"] for item in fields if isinstance(item, dict) and "name" in item}


def _known_values(dictionary, field):
    values = ((dictionary or {}).get("values") or {}).get(field) or []
    return [value for value in values if isinstance(value, str) and value.strip()]


# ---------------------------------------------------------------------- dates


def _period_at(words, index, now, lead=""):
    """The calendar period that starts at word `index`: (first day, last day, words used,
    rolling), or None. `rolling` is True for "the last 7 days": a period that runs up to now.
    `lead` is the word before it ("in", "since", ...)."""
    today = now.date()
    word = words.word(index)
    if not word:
        return None
    second = words.word(index + 1)
    third = words.word(index + 2)

    match = _ISO_DATE.fullmatch(word)
    if match:
        day = _date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return _used((day, day), 1) if day else None
    match = _US_DATE.fullmatch(word)
    if match:
        day = _date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
        return _used((day, day), 1) if day else None
    match = _ISO_MONTH.fullmatch(word)
    if match:
        return _used(_month_period(int(match.group(1)), int(match.group(2))), 1)

    if word == "today":
        return _used((today, today), 1)
    if word == "yesterday":
        return _used((_days_back(today, 1), _days_back(today, 1)), 1)

    if word in ("this", "current"):
        if second == "week":
            start = _days_back(today, today.weekday())
            return _used((start, start + datetime.timedelta(days=6)), 2)
        if second == "month":
            return _used(_month_period(today.year, today.month), 2)
        if second == "year":
            return _used(_year_period(today.year), 2)
        if second in _MONTHS:
            return _used(_month_period(today.year, _MONTHS[second]), 2)

    if word in ("last", "past", "previous"):
        count = _number(second) if second else None
        unit = _UNITS.get(third)
        if count is not None and unit:
            return _used(_rolling(now, count, unit), 3, True)
        if second in ("week", "month", "year"):
            if word == "past":
                return _used(_rolling(now, 1, second), 2, True)
            if second == "week":
                start = _days_back(today, today.weekday() + 7)
                return _used((start, start + datetime.timedelta(days=6)), 2)
            if second == "month":
                earlier = _months_back(today.replace(day=1), 1)
                return _used(_month_period(earlier.year, earlier.month), 2)
            return _used(_year_period(today.year - 1), 2)
        if second in _WEEKDAYS and word != "past":
            day = _days_back(today, (today.weekday() - _WEEKDAYS[second]) % 7 or 7)
            return _used((day, day), 2)
        if second in _MONTHS and word == "last":
            month = _MONTHS[second]
            year = today.year if month < today.month else today.year - 1
            return _used(_month_period(year, month), 2)

    count = _number(word)
    if count is not None and _UNITS.get(second) in ("day", "week") and third == "ago":
        day = _days_back(today, count * (7 if _UNITS[second] == "week" else 1))
        return _used((day, day), 3)

    if word in _MONTHS:
        month = _MONTHS[word]
        day_match = _DAY.fullmatch(second) if second else None
        year_follows = bool(second and _YEAR.fullmatch(second))
        if word == "may" and lead not in _MAY_LEADS and not day_match and not year_follows:
            return None             # "may I see ...": the verb, not the month
        if day_match and not year_follows:
            if third and _YEAR.fullmatch(third):
                day = _date(int(third), month, int(day_match.group(1)))
                return _used((day, day), 3) if day else None
            day = _recent_day(today, month, int(day_match.group(1)))
            return _used((day, day), 2) if day else None
        if year_follows:
            return _used(_month_period(int(second), month), 2)
        return _used(_month_period(today.year if month <= today.month else today.year - 1,
                                   month), 1)

    day_match = _DAY.fullmatch(word)
    if day_match and not _YEAR.fullmatch(word):
        month_at = index + 1
        if words.word(month_at) == "of":
            month_at += 1
        name = words.word(month_at)
        if name in _MONTHS:
            month = _MONTHS[name]
            after = words.word(month_at + 1)
            used = month_at - index + 1
            if after and _YEAR.fullmatch(after):
                day = _date(int(after), month, int(day_match.group(1)))
                return _used((day, day), used + 1) if day else None
            day = _recent_day(today, month, int(day_match.group(1)))
            return _used((day, day), used) if day else None

    if _YEAR.fullmatch(word) and lead in _YEAR_LEADS:
        return _used(_year_period(int(word)), 1)

    if word in _WEEKDAYS:
        day = _days_back(today, (today.weekday() - _WEEKDAYS[word]) % 7)
        return _used((day, day), 1)
    return None


def _recent_day(today, month, day):
    """"November 13" without a year: the most recent one that is not in the future."""
    found = _date(today.year, month, day)
    if found is None or found > today:
        found = _date(today.year - 1, month, day)
    return found


def _rolling(now, count, unit):
    """The period from `count` units before today until today."""
    today = now.date()
    count = max(0, min(count, 9999))
    if unit == "day":
        return _days_back(today, count), today
    if unit == "week":
        return _days_back(today, count * 7), today
    if unit == "month":
        return _months_back(today, count), today
    if unit == "year":
        return _months_back(today, count * 12), today
    # Hours are exact: "the last 24 hours" starts 24 hours before now, not at a midnight.
    try:
        return now - datetime.timedelta(hours=count), today
    except OverflowError:
        return _MIN_DAY, today


def _date_rules(words, now, found):
    """Find every date phrase; add {"field", "from", "to", "at"} to `found`."""
    index = 0
    while index < len(words):
        if not words.free(index):
            index += 1
            continue
        lead_at = index - 1
        if words.word(lead_at) == "the":
            lead_at -= 1
        lead_word = words.word(lead_at)
        period = _period_at(words, index, now, lead_word)
        if period is None:
            index += 1
            continue
        start, end, used, rolling = period
        # "between A and B", "from A to B", "A to B"
        joined = False
        joiner = words.word(index + used)
        if joiner in _RANGE_JOINERS:
            other_at = index + used + 1
            if words.word(other_at) == "the":
                other_at += 1
            other = _period_at(words, other_at, now, joiner)
            if other is not None:
                joined = True
                start = min(start, other[0], key=_order)
                end = max(end, other[1], key=_order)
                used = other_at + other[2] - index

        lead_from = lead_at
        effect = _LEAD_TWO.get((words.word(lead_at - 1), lead_word))
        if effect:
            lead_from = lead_at - 1
        else:
            effect = _LEAD_ONE.get(lead_word)
        if effect is None:
            effect, lead_from = "exact", index
        if not joined and words.word(index + used) in ("onwards", "onward", "forward"):
            effect = "from"                 # "from 2026-11-01 onwards"
            used += 1
        one_day = datetime.timedelta(days=1)
        if joined:
            low, high = start, end
        elif effect == "from":
            low, high = start, None
        elif effect == "after":
            low, high = end + one_day, None
        elif effect == "before":
            low, high = None, (start if isinstance(start, datetime.datetime)
                               else start - one_day)
        elif effect == "until":
            low, high = None, end
        else:
            low, high = start, (None if rolling else end)
        low = None if low is None else _clamp(low)
        high = None if high is None else _clamp(high)

        field = "started"
        for back in range(lead_from - 1, max(lead_from - 4, -1), -1):
            name = words.word(back)
            if name in _DATE_FIELD_WORDS:
                field = _DATE_FIELD_WORDS[name]
                words.use(back)
                break
            if name not in _FILLERS and name not in ("on", "in"):
                break
        words.use(*range(min(lead_from, index), index + used))
        found.append({"field": field, "from": low, "to": high, "at": index})
        index += used


def _merge_ranges(found):
    """One range per field: the latest start and the earliest end that do not contradict."""
    merged = {}
    for item in found:
        current = merged.get(item["field"])
        if current is None:
            merged[item["field"]] = dict(item)
            continue
        low, high = current["from"], current["to"]
        if item["from"] is not None and (low is None or _order(item["from"]) > _order(low)):
            low = item["from"]
        if item["to"] is not None and (high is None or _order(item["to"]) < _order(high)):
            high = item["to"]
        if low is not None and high is not None and _order(low) > _order(high):
            continue            # the later phrase contradicts the first: keep the first
        current["from"], current["to"] = low, high
    return list(merged.values())


# ---------------------------------------------------------------------- the planner


class _Plan:
    """What the rules have found so far."""

    def __init__(self, names):
        self.names = names
        self.entries = []       # (position in the question, field, op, values or None)
        self.count_by = None
        self.sort = None
        self.limit = None

    def knows(self, field):
        return self.names is None or field in self.names


def _negation_before(words, at):
    """The index of the first word of a negation ("not", "without", "other than", "everything
    but") that stands before word `at`, with only small words such as "are", "yet", "with"
    between; None when there is none."""
    before = at - 1
    while words.word(before) in _FILLERS or words.word(before) in _LINKS:
        before -= 1
    word = words.word(before)
    if word in _NEGATIONS:
        return before
    if word == "than" and words.word(before - 1) == "other":
        return before - 1
    if word == "but" and before > 0 and (words.low[before - 1] in _BUT_LEADS
                                         or words.low[before - 1] in _SESSION_NOUNS_MANY):
        return before
    return None


# "adenine was not found": a negation AFTER a value, followed by one of these words.
_FOUND_VERBS = frozenset({"found", "detected", "identified", "seen", "observed", "present",
                          "shown", "measured", "labelled", "labeled", "there", "included",
                          "contained", "used"})


def _negated_after(words, last):
    """True when "not" (or "never") and a word such as "found" follow word `last`, with only
    small words such as "was", "been" between: "r6g was not detected". Those words are used."""
    at = last + 1
    while words.word(at) in _FILLERS:
        at += 1
    if words.word(at) not in ("not", "never"):
        return False
    verb = at + 1
    while words.word(verb) in _FILLERS:
        verb += 1
    if words.word(verb) not in _FOUND_VERBS:
        return False
    words.use(*range(last + 1, verb + 1))
    return True


def _negated(words, at):
    """True when a negation stands before word `at` (see `_negation_before`); the words that
    say so are used up."""
    before = _negation_before(words, at)
    if before is None:
        return False
    words.use(*range(before, at))
    return True


def _count_rules(words, plan):
    for phrase in (("how", "many"), ("how", "much"), ("number", "of"), ("total", "number"),
                   ("count",), ("counts",), ("tally",)):
        for at in words.find(phrase):
            words.use(*range(at, at + len(phrase)))
            follower = at + len(phrase)
            if words.word(follower) in ("of", "the"):
                follower += 1
            name = _FIELD_WORDS.get(words.word(follower))
            if (plan.count_by is None and name and plan.knows(name)
                    and words.word(follower) not in _SAMPLE_WORDS):
                plan.count_by = name
                words.use(follower)
                if words.word(follower + 1) in _FIELD_WORD_TAILS:
                    words.use(follower + 1)
    for at in range(len(words)):
        size = 0
        if (words.word(at), words.word(at + 1)) in _GROUP_TWO:
            size = 2
        elif words.word(at) in _GROUP_ONE:
            size = 1
        if not size:
            continue
        target = at + size
        while words.word(target) in ("each", "every", "the"):
            target += 1
        name = _FIELD_WORDS.get(words.word(target))
        if not name or not plan.knows(name):
            continue
        if plan.count_by is None:
            plan.count_by = name
        words.use(*range(at, target + 1))
        if words.word(target + 1) in _FIELD_WORD_TAILS:
            words.use(target + 1)


def _sample_rules(words, plan, dictionary):
    """ "sample 130", "samples VS-001 and VS-002": the id is what follows the word."""
    if not plan.knows("sample_ids"):
        return
    known = {value.casefold(): value
             for value in reversed(_known_values(dictionary, "sample_ids"))}

    def is_id(word):
        return bool(word) and len(word) <= MAX_ID_CHARS and (
            word in known or any(ch.isdigit() for ch in word))

    for at in range(len(words)):
        if words.word(at) not in _SAMPLE_WORDS:
            continue
        target = at + 1
        while words.word(target) in _SAMPLE_TAILS:
            target += 1
        values = []
        while is_id(words.word(target)):
            value = known.get(words.low[target], words.text[target])
            if value not in values:
                values.append(value)
            target += 1
            if words.word(target) in ("and", "or") and is_id(words.word(target + 1)):
                target += 1
        if values:
            negated = _negated(words, at)
            words.use(*range(at, target))
            plan.entries.append((at, "sample_ids", "none" if negated else "any",
                                 values[:MAX_VALUES]))


def _one_is_meant(words, at):
    """True for "the latest session", "oldest raman session": a session word in the singular
    within the three words after word `at`."""
    for word in words.low[at + 1:at + 4]:
        if word in _SESSION_NOUNS_MANY:
            return False
        if word in _SESSION_NOUNS_ONE:
            return True
    return False


def _limit_rules(words, plan):
    for at in range(len(words)):
        word = words.word(at)
        if not word:
            continue
        following = words.word(at + 1)
        number_after = _number(following) if following else None
        order = _SORTS.get(word) or _SORTS_WEAK.get(word)
        if word in ("top", "limit") and number_after is not None:
            plan.limit = number_after
            words.use(at, at + 1)
        elif order and number_after is not None:
            plan.limit, plan.sort = number_after, order
            if word == "recent" and words.word(at - 1) == "most":
                words.use(at - 1)
            words.use(at, at + 1)
        else:
            number = _number(word)
            if number is None:
                continue
            next_at = at + 1
            if words.word(next_at) == "most" and words.word(next_at + 1) == "recent":
                next_at += 1
            following = words.word(next_at)
            order = _SORTS.get(following) or _SORTS_WEAK.get(following)
            if order:
                plan.limit, plan.sort = number, order
                words.use(*range(at, next_at + 1))
            elif following in _SESSION_NOUNS_MANY and plan.limit is None:
                plan.limit = number
                words.use(at)
    for at in range(len(words)):
        word = words.word(at)
        following = words.word(at + 1)
        order = _SORTS.get(word)
        if order is None and word in _SORTS_WEAK and (
                following in _SESSION_NOUNS_MANY or _one_is_meant(words, at)):
            order = _SORTS_WEAK[word]
        if order is None:
            continue
        if plan.sort is None:
            plan.sort = order
        if word == "recent" and words.word(at - 1) == "most":
            words.use(at - 1)
        words.use(at)
        if plan.limit is None and _one_is_meant(words, at):
            plan.limit = 1


def _fixed_word_rules(words, plan):
    if plan.knows("review_state"):
        for phrases, value in ((_REVIEW_PENDING, "pending"), (_REVIEW_DONE, "done")):
            for phrase in phrases:
                for at in words.find(phrase):
                    words.use(*range(at, at + len(phrase)))
                    plan.entries.append((at, "review_state", "any", [value]))
    for at in range(len(words)):
        special = _SPECIAL.get(words.word(at))
        if special is None:
            continue
        field, op, values = special
        if not plan.knows(field):
            continue
        words.use(at)
        if _negated(words, at):
            op = _FLIP[op]
        if op == "none" and values and isinstance(values[0], bool):
            op, values = "any", [not values[0]]      # "not simulated" is "simulated: no"
        plan.entries.append((at, field, op, list(values) if values else None))


def _value_rules(words, plan, dictionary):
    """Known values of the dictionary, longest first; then loose instrument names."""
    stream = []     # (word index, part, is the first part of its word, is the last part)
    for at, low in enumerate(words.low):
        parts = _atoms(low)
        for position, part in enumerate(parts):
            stream.append((at, part, position == 0, position == len(parts) - 1))
    candidates = []
    for priority, field in enumerate(VALUE_FIELDS):
        if not plan.knows(field):
            continue
        for value in _known_values(dictionary, field):
            atoms = _atoms(value)
            if not atoms or all(atom.isdigit() for atom in atoms):
                continue        # a bare number is a sample id only after the word "sample"
            if len(atoms) == 1 and atoms[0] in _RESERVED:
                continue        # an everyday word is never taken for a value
            candidates.append((-len(atoms), -len(value), priority, value, field, atoms))
    candidates.sort()
    hits = []                   # (first word, last word, field, value, negated)
    for _, _, _, value, field, atoms in candidates:
        size = len(atoms)
        for start in range(len(stream) - size + 1):
            window = stream[start:start + size]
            if not window[0][2] or not window[-1][3]:
                continue        # the value must cover whole words, not a part of one
            if [item[1] for item in window] != atoms:
                continue
            covered = sorted({item[0] for item in window})
            if any(words.used[index] for index in covered):
                continue
            words.use(*covered)
            hits.append((covered[0], covered[-1], field, value,
                         _negated(words, covered[0]) or _negated_after(words, covered[-1])))

    loose = {}
    if plan.knows("instrument"):
        for value in _known_values(dictionary, "instrument"):
            for atom in _atoms(value):
                if len(atom) >= 3 and not atom.isdigit() and atom not in _RESERVED:
                    if value not in loose.setdefault(atom, []):
                        loose[atom].append(value)
    for at in range(len(words)):
        word = words.word(at)
        if word in loose:
            words.use(at)
            negated = _negated(words, at) or _negated_after(words, at)
            hits.extend((at, at, "instrument", value, negated) for value in loose[word])

    hits.sort(key=lambda hit: (hit[0], hit[3]))
    groups = {}
    for first, last, field, value, negated in hits:
        group = groups.setdefault((field, negated), {"at": first, "values": [], "spans": []})
        if value not in group["values"]:
            group["values"].append(value)
            group["spans"].append((first, last))
    for (field, negated), group in groups.items():
        op = "none" if negated else "any"
        if not negated and field in _ALL_FIELDS and len(group["values"]) > 1:
            gaps = [words.low[one[1] + 1:two[0]]
                    for one, two in zip(group["spans"], group["spans"][1:])]
            if all(gap == ["and"] for gap in gaps):
                op = "all"
        plan.entries.append((group["at"], field, op, group["values"][:MAX_VALUES]))


def _filters_from(entries):
    """One filter per field and operation, in the order of the question; at most 8."""
    filters = []
    index_of = {}
    for _, field, op, values in sorted(entries, key=lambda entry: entry[0]):
        if op == "range":
            item = {"field": field, "op": "range", "from": values[0], "to": values[1]}
        elif op in ("exists", "missing"):
            item = {"field": field, "op": op}
        else:
            item = {"field": field, "op": op, "values": list(values)}
        key = (field, op)
        if key in index_of:
            if "values" in item:
                target = filters[index_of[key]]["values"]
                target.extend(value for value in item["values"] if value not in target)
                del target[MAX_VALUES:]
            continue
        if len(filters) >= MAX_FILTERS:
            break
        index_of[key] = len(filters)
        filters.append(item)
    return filters


def plan(question, dictionary, now):
    """The plan for `question`. `dictionary` is what `ask.build_dictionary` returns (its
    `values` are the known instruments, labels, ...; it may be empty or None). `now` is the
    local time as an aware datetime; one without a time zone is read as this computer's time."""
    return plan_with_notices(question, dictionary, now)[0]


def plan_with_notices(question, dictionary, now):
    """(plan, notices): `plan` as above; `notices` are sentences for the person, for example
    that words after "not" were left out because the rules can only exclude known values."""
    if not isinstance(now, datetime.datetime):
        raise TypeError("now must be a datetime")
    if now.tzinfo is None:
        now = now.astimezone()
    words = _Words(question if isinstance(question, str) else "")
    found = _Plan(_field_names(dictionary))

    _count_rules(words, found)
    ranges = []
    _date_rules(words, now, ranges)
    for item in _merge_ranges(ranges):
        if found.knows(item["field"]):
            found.entries.append((item["at"], item["field"], "range",
                                  (_plan_text(item["from"]) if item["from"] else None,
                                   _plan_text(item["to"]) if item["to"] else None)))
    _sample_rules(words, found, dictionary)
    _limit_rules(words, found)
    _fixed_word_rules(words, found)
    _value_rules(words, found, dictionary)

    keywords = ""
    dropped = []
    for at in range(len(words)):
        if words.used[at] or words.low[at] in _STOP_WORDS:
            continue
        # A word after "not" that is no known value cannot be excluded by a keyword search:
        # searching for it would return exactly what was to be left out. It is dropped (and
        # so is a word that follows a dropped one directly: "not from the SEM lab").
        if _negation_before(words, at) is not None or (dropped and dropped[-1][0] == at - 1):
            dropped.append((at, words.text[at]))
            continue
        longer = (keywords + " " + words.text[at]).strip()
        if len(longer) > MAX_KEYWORD_CHARS:
            break
        keywords = longer

    sort = found.sort
    if sort is None and not keywords:
        sort = ("started", "desc")      # a listing shows the newest first; a word search
    if sort is not None and not found.knows(sort[0]):       # keeps the best match first
        sort = None
    limit = found.limit if found.limit is not None else DEFAULT_LIMIT
    notices = []
    if dropped:
        shown = " ".join(text for _, text in dropped)[:MAX_KEYWORD_CHARS]
        notices.append('The words "%s" after a "not" were left out of the search: without the '
                       "assistant, only known values (instruments, labels, methods, tags, "
                       "samples, models) can be excluded. The results are not filtered by "
                       "them." % shown)
    else:
        left = [words.text[at] for at in range(len(words))
                if not words.used[at] and words.low[at] in _UNUSED_NEGATIONS]
        if left:
            notices.append('The word "%s" was not understood by the keyword rules, so nothing '
                           "was left out because of it. Check that the results are what you "
                           "meant." % left[0])
    return {
        "keywords": keywords,
        "filters": _filters_from(found.entries),
        "sort": {"field": sort[0], "order": sort[1]} if sort else None,
        "limit": max(1, min(limit, MAX_LIMIT)),
        "count_by": found.count_by,
        "explanation": EXPLANATION,
    }, notices
