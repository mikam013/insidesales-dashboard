import copy
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
ENV_PATH = os.path.join(HERE, ".env")
POWER_DIALER_HISTORY_PATH = os.path.join(HERE, "power_dialer_history.json")
INBOUND_CALL_AUDIT_PATH = os.path.join(HERE, "inbound_call_audit.json")
# FieldRoutes revenue/ACV numbers aren't reachable from here (they live in
# Revhawk's BigQuery), so this is a snapshot that gets refreshed by hand.
REVENUE_SNAPSHOT_PATH = os.path.join(HERE, "revenue_snapshot.json")
if not os.path.exists(REVENUE_SNAPSHOT_PATH) and os.path.exists("/etc/secrets/revenue_snapshot.json"):
    # On Render the snapshot is uploaded as a Secret File, since the repo is public.
    REVENUE_SNAPSHOT_PATH = "/etc/secrets/revenue_snapshot.json"
# Preferred source: the private data repo, refreshed by a scheduled job.
REVENUE_SNAPSHOT_TTL_SECONDS = 1800
_revenue_snapshot_cache = {"fetchedAt": 0, "data": None}


def load_revenue_snapshot():
    # Private data repo first (when a token is configured), cached for 30 min;
    # falls back to a local/secret file so local dev keeps working.
    token = os.environ.get("DATA_REPO_TOKEN", "")
    repo = os.environ.get("DATA_REPO", "mikam013/insidesales-data")
    if token:
        now = time.monotonic()
        if _revenue_snapshot_cache["data"] is None or now - _revenue_snapshot_cache["fetchedAt"] > REVENUE_SNAPSHOT_TTL_SECONDS:
            req = urllib.request.Request(f"https://api.github.com/repos/{repo}/contents/revenue_snapshot.json")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Accept", "application/vnd.github.raw")
            req.add_header("User-Agent", "evo-dashboard/1.0")
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    _revenue_snapshot_cache["data"] = json.loads(resp.read().decode("utf-8"))
                    _revenue_snapshot_cache["fetchedAt"] = now
            except Exception:
                if _revenue_snapshot_cache["data"] is None and not os.path.exists(REVENUE_SNAPSHOT_PATH):
                    raise
        if _revenue_snapshot_cache["data"] is not None:
            return _revenue_snapshot_cache["data"]
    if not os.path.exists(REVENUE_SNAPSHOT_PATH):
        return None
    with open(REVENUE_SNAPSHOT_PATH) as f:
        return json.load(f)

def _hash_key(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def contact_match_keys(contact):
    # Same normalisation as query.sql: last 10 digits of each phone, lowercased email.
    keys = set()
    phones = [contact.get("phone")] + [p.get("phone") if isinstance(p, dict) else p for p in contact.get("additionalPhones") or []]
    for phone in phones:
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())[-10:]
        if len(digits) == 10:
            keys.add(_hash_key(digits))
    emails = [contact.get("email")] + [e.get("email") if isinstance(e, dict) else e for e in contact.get("additionalEmails") or []]
    for email in emails:
        email = str(email or "").strip().lower()
        if "@" in email[1:]:
            keys.add(_hash_key(email))
    return keys


REVENUE_ROW_FIELDS = ("sold", "acv", "arv", "initial", "recurring", "multiyear", "m18", "m24", "cancelled",
                      "cancelledAcv", "serviced", "servicedAcv", "servicedActive", "servicedActiveAcv")


def revenue_rows(sales):
    # Same per rep-group/month totals build_snapshot.py writes for the whole team.
    agg = {}
    for sale in sales:
        a = agg.setdefault((sale["group"], sale["sold"][:7]), dict.fromkeys(REVENUE_ROW_FIELDS, 0))
        acv = sale.get("contractValue") or 0
        a["sold"] += 1
        a["acv"] += acv
        a["arv"] += sale.get("arv") or 0
        a["initial"] += sale.get("initial") or 0
        if sale.get("frequency") != "-1" and (sale.get("months") or 0) > 1:
            a["recurring"] += 1
        if (sale.get("months") or 0) >= 18:
            a["multiyear"] += 1
        if sale.get("months") == 18:
            a["m18"] += 1
        if sale.get("months") == 24:
            a["m24"] += 1
        if sale.get("cancelled"):
            a["cancelled"] += 1
            a["cancelledAcv"] += acv
        if sale.get("serviced"):
            a["serviced"] += 1
            a["servicedAcv"] += acv
            if not sale.get("cancelled"):
                a["servicedActive"] += 1
                a["servicedActiveAcv"] += acv
    return [dict(rep=k[0], month=k[1], **{f: round(v) for f, v in totals.items()})
            for k, totals in sorted(agg.items(), key=lambda kv: (kv[0][1], kv[0][0]))]


def _channel_lead_sources():
    # (channel, contacts) for every lead list a FieldRoutes sale can be matched
    # to. The account a lead lives in decides its channel (Mika, 2026-10-26):
    # every main-account contact is Inbound whatever its source; Cold and Warm
    # Outbound are every contact in those sub-accounts.
    sources = [("Inbound", [c for c in get_all_contacts_cached() if not is_excluded_contact(c)]),
               ("Commercial", get_commercial_contacts())]
    if COLD_OUTBOUND_API_KEY and COLD_OUTBOUND_LOCATION_ID:
        sources.append(("Cold Outbound", cold_outbound_contacts.contacts()))
    if WARM_OUTBOUND_API_KEY and WARM_OUTBOUND_LOCATION_ID:
        sources.append(("Warm Outbound", warm_outbound_contacts.contacts()))
    return sources


# Same-day tie-break, highest last (a website form lead can land in the main
# account and Warm Outbound on the same day).
CHANNEL_TIE_ORDER = ["Cold Outbound", "Warm Outbound", "Inbound", "Commercial"]


def _name_key(name):
    # Order-free full name ("Pritchard Ethan" == "Ethan Pritchard"); needs two words.
    words = sorted(w for w in "".join(ch if ch.isalpha() or ch == " " else " " for ch in (name or "").lower()).split())
    return " ".join(words) if len(words) >= 2 else None


# Main-account lead sources whose sales belong to Warm Outbound (Mika, 2026-10-29).
WARM_LEAD_SOURCES = re.compile(r"^(website form|meta ads|paid social|snoball referral|contact us form)", re.I)


def with_rep_names(snapshot):
    # FieldRoutes sales by the rep's own name, never "Others"; James Graham Jr.
    # only counts from his start date (Mika, 2026-10-30). Rows and rep lists are
    # rebuilt so a rep only appears in months where they sold something.
    out = copy.deepcopy(snapshot)
    sales = []
    for sale in out.get("sales") or []:
        name = sale.get("rep") or sale.get("group")
        if name in REP_START and sale["sold"] < REP_START[name]:
            continue
        sale["group"] = name
        sales.append(sale)
    out["sales"] = sales
    out["rows"] = revenue_rows(sales)
    sold_by = {r["rep"] for r in out["rows"]}
    order = CORE_REPS + FORMER_REPS
    out["reps"] = ["Jay Reyes", "Daniel Barba"] + sorted(sold_by - {"Jay Reyes", "Daniel Barba"}, key=lambda n: (order.index(n) if n in order else len(order), n))
    out["othersNote"] = None
    if isinstance(out.get("inbound"), dict):
        out["inbound"]["othersNote"] = None
    return out


def sale_source_label(channel, lead_source):
    # Sales List "Source" column: just the source, e.g. "Inbound", "Meta Ads",
    # "Website Form"; no channel name (Mika, 2026-10-30).
    if channel == "Not matched":
        return "Not matched to a lead"
    raw = (lead_source or "").strip()
    # GMB, Truck Number, FieldRoutes... are all calls to the Inbound lines: just "Inbound".
    if raw.lower().startswith("inbound") or channel in ("Inbound", "Commercial"):
        return "Inbound"
    low = raw.lower()
    return ("Meta Ads" if "meta" in low or "facebook" in low or "paid social" in low else
            "Website Form" if "form" in low else
            "Referral" if "referral" in low else
            "Missed Inbound Call" if "missed inbound" in low else
            raw or "No source set")


def with_live_channels(snapshot):
    # Gives each FieldRoutes sale a channel by matching its phone/email keys to
    # the lead lists above: the most recent lead that came in on or before the
    # sold date wins (same-day ties: Inbound, then Warm, then Cold). Sales matching no lead
    # before the sale are "Not matched". Rebuilds the Inbound Leads section from the Inbound sales.
    # Falls back to the stored Inbound tags if the snapshot predates match keys
    # or HighLevel is down.
    snapshot = with_rep_names(snapshot)
    sales = snapshot.get("sales") or []
    if not any(sale.get("keys") for sale in sales):
        return snapshot
    try:
        leads = {}
        by_name = {}
        for channel, contacts in _channel_lead_sources():
            for c in contacts:
                added = local_date(parse_date(c.get("dateAdded")))
                if added is None:
                    continue
                for key in contact_match_keys(c):
                    leads.setdefault(key, []).append((added.isoformat(), channel, c.get("id"), c.get("source")))
                name = _name_key(f"{c.get('firstName') or ''} {c.get('lastName') or ''}")
                if name:
                    by_name.setdefault(name, []).append((added.isoformat(), channel, c.get("id"), c.get("source")))
    except Exception:
        return snapshot
    live = copy.deepcopy(snapshot)
    for sale in live["sales"]:
        candidates = [lead for key in sale.get("keys") or [] for lead in leads.get(key, []) if lead[0] <= sale["sold"]]
        if not candidates:
            # Phone/email differ between FieldRoutes and HighLevel for some
            # customers; fall back to the same full name (either order).
            candidates = by_name.get(_name_key(sale.get("customer")), [])
        best = max(candidates, key=lambda lead: (lead[0] <= sale["sold"], lead[0], CHANNEL_TIE_ORDER.index(lead[1])), default=None)
        sale["channel"] = best[1] if best else "Not matched"
        if sale["channel"] == "Inbound" and WARM_LEAD_SOURCES.match((best[3] or "").strip()):
            sale["channel"] = "Warm Outbound"
        sale["leadContactId"] = best[2] if best else None
        sale["source"] = sale_source_label(sale["channel"], best[3] if best else None)
        sale["inbound"] = sale["channel"] == "Inbound"
        sale.pop("keys", None)
    inbound = dict(live.get("inbound") or {})
    inbound["rows"] = revenue_rows([sale for sale in live["sales"] if sale["inbound"]])
    inbound["asOf"] = datetime.now(BUSINESS_TZ).date().isoformat()
    inbound["label"] = "Inbound leads (any lead in the main HighLevel account) matched to FieldRoutes by phone/email, where that lead is the most recent lead before the sale (matched live)"
    inbound["reps"] = live.get("reps")
    live["inbound"] = inbound
    live["channels"] = [ch for ch, _ in _channel_lead_sources()] + ["Not matched"]
    live.pop("inboundSubscriptionIds", None)
    return live


API_BASE = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"

BUSINESS_TZ = ZoneInfo("America/Chicago")

REPS = [
    {"name": "Jay Reyes", "pipelineId": "T1XpjVLy3lDfLVWY7f9D", "closedWonStageId": "47bfd32a-1e99-46af-9a47-4723a9af3e36"},
    {"name": "Daniel Barba", "pipelineId": "I7YdjQ5jlYF21SEl5B5g", "closedWonStageId": "342ebbe5-29be-46d7-aa19-4d486e841233"},
    {"name": "James Graham Jr.", "pipelineId": "LnqE1gadHU7IyzfvIBMv", "closedWonStageId": "ee212943-abe0-4110-a437-a3646aa293a8"},
    {"name": "Sergio Anaya", "pipelineId": "4QyVIDo2jPdiPQUflkLd", "closedWonStageId": "e05e0bd7-c78f-4906-a16b-edcaa56fc8e8", "onlyIfClosed": True},
]

# Reps shown on the dashboard (Mika, 2026-10-30): Jay and Daniel always; James
# Graham Jr. from Nov 1, 2026; former reps (Sergio Anaya, Jesus Felix) by name,
# only in periods where they closed a deal. No "Others" bucket.
CORE_REPS = ["Jay Reyes", "Daniel Barba", "James Graham Jr."]
FORMER_REPS = ["Sergio Anaya", "Jesus Felix"]
REP_START = {"James Graham Jr.": "2026-11-01"}


def rep_active(name, end):
    return end.isoformat() >= REP_START.get(name, "0000")


def active_reps(end):
    # Pipeline reps worth querying for a period ending on `end`.
    return [r for r in REPS if rep_active(r["name"], end)]

CLOSED_DATE_FIELD_ID = "y2AEQ5nyUzxhLLLnG6bx"
QUALIFICATION_STATUS_FIELD_ID = "SYrPZw5WGIbpPYuiDaBx"
FIRST_TOUCH_QUALIFIER_FIELD_ID = "bvDD6N47FKnlJEEwNAkb"
POWER_DIALER_ATTEMPT_FIELD_ID = "vNjCVqQ3NiMGBQ4mHehJ"
LEAD_STAGE_FIELD_ID = "dt44fTl1UU9rY77EyAvb"
SALES_REP_FIELD_ID = "LWLa9vQ7KLP3UuV5MMzc"
PROPERTY_TYPE_FIELD_ID = "8d7ke2jHKefOOPYwswEJ"  # Residential / Commercial (main account only)
COMMENTS_FIELD_ID = "ApQ29DPJs00MuMPSu0dq"  # Quality folder; holds the "Qualification reason (...): ..." line


REASON_LABELS = {"Qualified": "Qualification reason", "Unqualified": "Unqualification reason", "Unknown": "Unknown reason"}


def with_qualification_reason(comments, reason, source, status):
    # One reason line labelled by status (Mika, 2026-10-30): "Qualification
    # reason" / "Unqualification reason" / "Unknown reason". Replaces any earlier
    # reason line and keeps the rest of the comments.
    labels = tuple(l.lower() for l in REASON_LABELS.values())
    kept = [l for l in str(comments or "").splitlines() if not l.lower().startswith(labels)]
    line = f"{REASON_LABELS.get(status, 'Qualification reason')} ({source} {datetime.now(BUSINESS_TZ).date().isoformat()}): {reason}"
    return "\n".join([line] + [l for l in kept if l.strip()])

EXCLUDED_TAGS = {"termite setter program"}

DEFAULT_LOCATION_ID = "TqGBaQdHphlOYfdWZo9s"

# Cold Outbound is a separate GHL subaccount (own API key + location).
COLD_OUTBOUND_LAST_CALLER_FIELD_ID = "RKEqPv4XbmkuZrqb0ZJr"
COLD_OUTBOUND_LAST_CALL_DATE_FIELD_ID = "d8N0IGeaZJsqhdq04x8A"
COLD_OUTBOUND_DISPOSITION_FIELD_ID = "tfYslC34CMnaSULFwYU1"
COLD_OUTBOUND_DISPOSITION_DATE_FIELD_ID = "kFn9EOOlCzR4q8cLRfGk"

# "Last Outbound Caller" picklist options in the Cold Outbound subaccount —
# shown on the chart even when a rep has zero calls in the selected range.
COLD_OUTBOUND_CALLERS = ["Jay Reyes", "Daniel Barba"]

# Fixed call-outcome categories, always shown even at zero count. "Last Call
# Dispositon" is a free-text field in HighLevel (no picklist), so this list is
# maintained here rather than read from the field definition.
COLD_OUTBOUND_DISPOSITIONS = [
    "Closed/Won",
    "Answered",
    "No Answer/VM",
    "Not Interested",
    "Incorrect Number",
    "Do Not Call",
]


def is_tag_excluded(contact):
    return bool(set(contact.get("tags") or []) & EXCLUDED_TAGS)


def is_commercial_contact(contact):
    return custom_field_value(contact.get("customFields"), PROPERTY_TYPE_FIELD_ID) == "Commercial"


# ~7,500 contacts were bulk-imported into the main account on 2026-05-04/05 with
# no source (old customers + leads, not new enquiries). Counting them as Inbound
# leads made This Year show 11,368 leads at 11% qualified. No FieldRoutes sale
# is matched to any of them, so leaving them out only fixes lead counts
# (Mika: "make sure everything is accurate", 2026-10-27). Set BULK_IMPORT_DATES=""
# to count them again.
BULK_IMPORT_DATES = {d.strip() for d in os.environ.get("BULK_IMPORT_DATES", "2026-05-04,2026-05-05").split(",") if d.strip()}


def is_bulk_import_contact(contact):
    if contact.get("source") or not BULK_IMPORT_DATES:
        return False
    added = local_date(parse_date(contact.get("dateAdded")))
    return bool(added) and added.isoformat() in BULK_IMPORT_DATES


def is_excluded_contact(contact):
    # Commercial leads are kept off the main dashboards and shown on the
    # Commercial tab instead (Mika, 2026-10-26). Bulk-imported contacts aren't leads.
    return is_tag_excluded(contact) or is_commercial_contact(contact) or is_bulk_import_contact(contact)


def load_dotenv_into_environ():
    # Local dev convenience only. On a host like Render, real env vars are set
    # via the dashboard and take precedence over anything in a .env file.
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


load_dotenv_into_environ()
# Inbound = the main account. The _INBOUND names match the Warm/Cold ones;
# the old unsuffixed names still work.
API_KEY = os.environ.get("HIGHLEVEL_API_KEY_INBOUND") or os.environ.get("HIGHLEVEL_API_KEY", "")
LOCATION_ID = os.environ.get("HIGHLEVEL_LOCATION_ID_INBOUND") or os.environ.get("HIGHLEVEL_LOCATION_ID", DEFAULT_LOCATION_ID)
COLD_OUTBOUND_API_KEY = os.environ.get("HIGHLEVEL_API_KEY_COLD_OUTBOUND", "")
COLD_OUTBOUND_LOCATION_ID = os.environ.get("HIGHLEVEL_LOCATION_ID_COLD_OUTBOUND", "")
WARM_OUTBOUND_API_KEY = os.environ.get("HIGHLEVEL_API_KEY_WARM_OUTBOUND", "")
WARM_OUTBOUND_LOCATION_ID = os.environ.get("HIGHLEVEL_LOCATION_ID_WARM_OUTBOUND", "")


class ConfigError(Exception):
    pass


def hl_request(path, params=None, method="GET", api_key=None, body=None):
    api_key = api_key or API_KEY
    if not api_key:
        raise ConfigError("HighLevel API key is not set. Add it to .env and restart the server.")
    params = dict(params or {})
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Version", API_VERSION)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "evo-dashboard/1.0")
    last_error = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(2 * (attempt + 1))  # backoff: 2s, 4s, 6s, 8s
                last_error = e
                continue
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HighLevel API error {e.code} on {path}: {body}")
        except (TimeoutError, OSError) as e:
            last_error = e
    raise RuntimeError(f"HighLevel API request to {path} timed out after 3 attempts: {last_error}")


def parse_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    try:
        value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def custom_field_value(custom_fields, field_id):
    for cf in custom_fields or []:
        if cf.get("id") == field_id:
            for key in ("value", "fieldValue", "fieldValueDate", "fieldValueString", "fieldValueArray"):
                if cf.get(key) is not None:
                    return cf.get(key)
    return None


def in_range(d, start, end):
    return d is not None and start <= d <= end


def local_date(dt):
    # For real timestamps (dateAdded, createdAt) — convert to the business's
    # timezone to get the calendar date it actually happened on locally.
    if dt is None:
        return None
    return dt.astimezone(BUSINESS_TZ).date() if dt.tzinfo else dt.date()


def utc_date_only(dt):
    # For date-only fields (like the opportunity "Closed Date" custom field),
    # HighLevel encodes the chosen calendar date as midnight UTC — that's a
    # pure date value with no real time-of-day, so it must NOT be converted
    # to another timezone (doing so would shift it back a day).
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).date() if dt.tzinfo else dt.date()


def fetch_all_opportunities(pipeline_id, stage_id, location_id=None, api_key=None):
    results = []
    start_after = None
    start_after_id = None
    for _ in range(20):  # safety cap: 20 pages x 100 = 2000 opportunities per rep
        params = {
            "location_id": location_id or LOCATION_ID,
            "pipeline_id": pipeline_id,
            "pipeline_stage_id": stage_id,
            "limit": 100,
        }
        if start_after:
            params["startAfter"] = start_after
        if start_after_id:
            params["startAfterId"] = start_after_id
        data = hl_request("/opportunities/search", params, api_key=api_key)
        page = data.get("opportunities", data.get("data", []))
        results.extend(page)
        meta = data.get("meta", {})
        next_after = meta.get("startAfter")
        next_after_id = meta.get("startAfterId")
        if not page or len(page) < 100 or not next_after_id:
            break
        start_after, start_after_id = next_after, next_after_id
    return results


SEARCH_PAGE_LIMIT = 500
SEARCH_RESULT_WINDOW = 10000  # HighLevel rejects page * pageLimit beyond this
DELTA_PAGE_LIMIT = 100
# Full rescans exist only to drop contacts deleted in HighLevel — a delta
# sync can see edits and new contacts, but not deletions.
FULL_RESYNC_SECONDS = 1800
# Re-pull a few minutes before the last-seen update so clock skew or
# near-simultaneous edits can't slip between two delta syncs.
DELTA_OVERLAP_MS = 5 * 60 * 1000


def _updated_ms(contact):
    d = parse_date(contact.get("dateUpdated") or contact.get("dateAdded"))
    return int(d.timestamp() * 1000) if d else 0


class ContactStore:
    # Holds every contact in a HighLevel location in memory. The full scan
    # (~23 pages of 500 via /contacts/search, mostly in parallel) only runs at startup and every
    # FULL_RESYNC_SECONDS; every other sync — including the Refresh Data
    # button — pulls just the contacts updated since the last sync, which is
    # usually a single request.
    def __init__(self, location_id, api_key):
        self.location_id = location_id
        self.api_key = api_key
        self._by_id = {}
        self._high_water_ms = 0
        self._full_synced_at = None
        self._synced_at = None
        self._lock = threading.Lock()  # one sync at a time; concurrent callers wait and reuse it

    def _search(self, sort_field, page_limit, should_stop=None, start_after=None):
        results = []
        search_after = start_after
        for _ in range(200):  # safety cap: 200 pages x 500 = 100000
            body = {
                "locationId": self.location_id,
                "pageLimit": page_limit,
                "sort": [{"field": sort_field, "direction": "desc"}],
            }
            if search_after:
                body["searchAfter"] = search_after
            data = hl_request("/contacts/search", method="POST", body=body, api_key=self.api_key)
            page = data.get("contacts", [])
            results.extend(page)
            if len(page) < page_limit or (should_stop and should_stop(page)):
                break
            search_after = page[-1].get("searchAfter")
            if not search_after:
                break
        return results

    def _search_page(self, page_number):
        body = {
            "locationId": self.location_id,
            "page": page_number,
            "pageLimit": SEARCH_PAGE_LIMIT,
            "sort": [{"field": "dateAdded", "direction": "desc"}],
        }
        return hl_request("/contacts/search", method="POST", body=body, api_key=self.api_key)

    def _full_sync(self):
        # Numbered pages can be fetched in parallel, but HighLevel caps them at
        # 10000 results — anything past that continues via the searchAfter cursor.
        first = self._search_page(1)
        total = first.get("total") or 0
        numbered_pages = min(-(-total // SEARCH_PAGE_LIMIT), SEARCH_RESULT_WINDOW // SEARCH_PAGE_LIMIT)
        with ThreadPoolExecutor(max_workers=10) as pool:
            rest = list(pool.map(self._search_page, range(2, numbered_pages + 1)))
        pages = [first.get("contacts", [])] + [d.get("contacts", []) for d in rest]
        contacts = [c for page in pages for c in page]
        last_page = pages[-1]
        if total > SEARCH_RESULT_WINDOW and len(last_page) == SEARCH_PAGE_LIMIT and last_page[-1].get("searchAfter"):
            contacts += self._search("dateAdded", SEARCH_PAGE_LIMIT, start_after=last_page[-1]["searchAfter"])
        # Contacts added mid-scan can shift page boundaries and duplicate a
        # row (deduped here) or skip one (picked up by the next delta sync).
        self._by_id = {c["id"]: c for c in contacts}
        self._high_water_ms = max((_updated_ms(c) for c in contacts), default=0)
        self._full_synced_at = time.monotonic()

    def _delta_sync(self):
        cutoff = self._high_water_ms - DELTA_OVERLAP_MS
        changed = self._search("dateUpdated", DELTA_PAGE_LIMIT, should_stop=lambda page: _updated_ms(page[-1]) < cutoff)
        merged = dict(self._by_id)  # swap in a new dict so readers never see one mid-update
        for c in changed:
            merged[c["id"]] = c
        self._by_id = merged
        self._high_water_ms = max([self._high_water_ms] + [_updated_ms(c) for c in changed])

    def sync(self, max_age_seconds=0):
        with self._lock:
            now = time.monotonic()
            if self._synced_at is not None and now - self._synced_at < max_age_seconds:
                return
            if self._full_synced_at is None or now - self._full_synced_at >= FULL_RESYNC_SECONDS:
                self._full_sync()
            else:
                self._delta_sync()
            self._synced_at = time.monotonic()
        _cache.clear()  # computed responses were built from the old contact set

    def contacts(self):
        self.sync(max_age_seconds=DATA_CACHE_TTL_SECONDS)
        return sorted(self._by_id.values(), key=lambda c: c.get("dateAdded") or "", reverse=True)


DATA_CACHE_TTL_SECONDS = 600
_opps_cache = {}
main_contacts = ContactStore(LOCATION_ID, API_KEY)
cold_outbound_contacts = ContactStore(COLD_OUTBOUND_LOCATION_ID, COLD_OUTBOUND_API_KEY)
warm_outbound_contacts = ContactStore(WARM_OUTBOUND_LOCATION_ID, WARM_OUTBOUND_API_KEY)


def get_contacts_for(range_start):
    # Every caller filters by dateAdded itself, so the full contact set is a
    # safe superset of the old "fetch back to range_start" behavior.
    return get_all_contacts_cached()


def _get_all_contacts_raw():
    return main_contacts.contacts()


def get_all_contacts_cached():
    return [c for c in _get_all_contacts_raw() if not is_excluded_contact(c)]


def get_excluded_contact_ids():
    return {c.get("id") for c in _get_all_contacts_raw() if is_excluded_contact(c)}


def get_commercial_contacts():
    return [c for c in _get_all_contacts_raw() if is_commercial_contact(c) and not is_tag_excluded(c)]


def get_audit_contacts():
    # The transcript checker and call log still cover commercial leads.
    return [c for c in _get_all_contacts_raw() if not is_tag_excluded(c)]


def get_cold_outbound_contacts_cached():
    if not COLD_OUTBOUND_API_KEY or not COLD_OUTBOUND_LOCATION_ID:
        raise ConfigError(
            "HIGHLEVEL_API_KEY_COLD_OUTBOUND / HIGHLEVEL_LOCATION_ID_COLD_OUTBOUND are not set. Add them to .env and restart the server."
        )
    return cold_outbound_contacts.contacts()


def get_opportunities_for(pipeline_id, stage_id):
    now = time.monotonic()
    key = (pipeline_id, stage_id)
    hit = _opps_cache.get(key)
    if hit and now - hit["fetchedAt"] < DATA_CACHE_TTL_SECONDS:
        return hit["opps"]
    opps = fetch_all_opportunities(pipeline_id, stage_id)
    _opps_cache[key] = {"fetchedAt": now, "opps": opps}
    return opps


def is_test_opportunity(opp):
    return "test" in (opp.get("name") or "").lower()


def rep_sales(rep, start, end, allowed_contact_ids=None):
    opps = get_opportunities_for(rep["pipelineId"], rep["closedWonStageId"])
    excluded_ids = get_excluded_contact_ids()
    count = 0
    value = 0.0
    missing_closed_date = 0
    value_by_contact = {}
    for opp in opps:
        if is_test_opportunity(opp) or opp.get("contactId") in excluded_ids:
            continue
        if allowed_contact_ids is not None and opp.get("contactId") not in allowed_contact_ids:
            continue
        closed_dt = parse_date(custom_field_value(opp.get("customFields"), CLOSED_DATE_FIELD_ID))
        missing = closed_dt is None
        if missing:
            closed = local_date(parse_date(opp.get("createdAt") or opp.get("dateAdded")))
        else:
            closed = utc_date_only(closed_dt)
        if in_range(closed, start, end):
            count += 1
            opp_value = float(opp.get("monetaryValue") or 0)
            value += opp_value
            contact_id = opp.get("contactId")
            value_by_contact[contact_id] = max(value_by_contact.get(contact_id, 0), opp_value)
            if missing:
                missing_closed_date += 1
    return {
        "name": rep["name"],
        "count": count,
        "value": round(value, 2),
        "missingClosedDate": missing_closed_date,
        "valueByContact": value_by_contact,
    }


def rep_deals(rep, start, end, allowed_contact_ids=None, with_contact=False):
    opps = get_opportunities_for(rep["pipelineId"], rep["closedWonStageId"])
    excluded_ids = get_excluded_contact_ids()
    deals = []
    for opp in opps:
        if is_test_opportunity(opp) or opp.get("contactId") in excluded_ids:
            continue
        if allowed_contact_ids is not None and opp.get("contactId") not in allowed_contact_ids:
            continue
        closed_dt = parse_date(custom_field_value(opp.get("customFields"), CLOSED_DATE_FIELD_ID))
        missing = closed_dt is None
        if missing:
            closed = local_date(parse_date(opp.get("createdAt") or opp.get("dateAdded")))
        else:
            closed = utc_date_only(closed_dt)
        if in_range(closed, start, end):
            deal = {
                "name": opp.get("name") or "Unnamed",
                "value": round(float(opp.get("monetaryValue") or 0), 2),
                "closedDate": closed.isoformat(),
            }
            if with_contact:
                deal["contactId"] = opp.get("contactId")
            deals.append(deal)
    deals.sort(key=lambda d: d["closedDate"], reverse=True)
    return deals


def handle_rep_deals(rep_name, start, end):
    rep = next((r for r in REPS if r["name"] == rep_name), None)
    if not rep:
        raise ValueError(f"Unknown rep: {rep_name}")
    return {"rep": rep_name, "deals": rep_deals(rep, start, end)}


def handle_inbound_rep_deals(rep_name, start, end):
    rep = next((r for r in REPS if r["name"] == rep_name), None)
    if not rep:
        raise ValueError(f"Unknown rep: {rep_name}")
    return {"rep": rep_name, "deals": rep_deals(rep, start, end, allowed_contact_ids=all_inbound_contact_ids())}


def rep_qualified_lead_list(contacts, rep_name, start, end):
    leads = []
    for c in contacts:
        added = local_date(parse_date(c.get("dateAdded")))
        if not in_range(added, start, end):
            continue
        status = custom_field_value(c.get("customFields"), QUALIFICATION_STATUS_FIELD_ID)
        if status != "Qualified":
            continue
        reps = custom_field_value(c.get("customFields"), FIRST_TOUCH_QUALIFIER_FIELD_ID)
        reps = reps if isinstance(reps, list) else ([reps] if reps else [])
        if rep_name not in reps:
            continue
        leads.append({
            "name": c.get("contactName") or c.get("firstName") or "Unnamed",
            "dateAdded": added.isoformat(),
        })
    leads.sort(key=lambda d: d["dateAdded"], reverse=True)
    return leads


def handle_inbound_qualified_leads(rep_name, start, end):
    if not any(r["name"] == rep_name for r in REPS):
        raise ValueError(f"Unknown rep: {rep_name}")
    contacts = get_contacts_for(start)
    inbound_contacts = [c for c in contacts if is_inbound_source(c.get("source"))]
    return {"rep": rep_name, "leads": rep_qualified_lead_list(inbound_contacts, rep_name, start, end)}


def handle_sales(start, end):
    reps = active_reps(end)
    with ThreadPoolExecutor(max_workers=len(reps)) as pool:
        reps_out = list(pool.map(lambda rep: rep_sales(rep, start, end), reps))
    reps_out = [r for r, rep in zip(reps_out, reps) if not rep.get("onlyIfClosed") or r["count"]]
    for r in reps_out:
        r.pop("valueByContact", None)
    return {
        "reps": reps_out,
        "totals": {
            "count": sum(r["count"] for r in reps_out),
            "value": round(sum(r["value"] for r in reps_out), 2),
        },
    }


def rep_leads_count(contacts, rep_name, start, end):
    count = 0
    for c in contacts:
        added = local_date(parse_date(c.get("dateAdded")))
        if not in_range(added, start, end):
            continue
        reps = custom_field_value(c.get("customFields"), FIRST_TOUCH_QUALIFIER_FIELD_ID)
        reps = reps if isinstance(reps, list) else ([reps] if reps else [])
        if rep_name in reps:
            count += 1
    return count


def rep_qualified_leads_count(contacts, rep_name, start, end):
    count = 0
    for c in contacts:
        added = local_date(parse_date(c.get("dateAdded")))
        if not in_range(added, start, end):
            continue
        status = custom_field_value(c.get("customFields"), QUALIFICATION_STATUS_FIELD_ID)
        if status != "Qualified":
            continue
        reps = custom_field_value(c.get("customFields"), FIRST_TOUCH_QUALIFIER_FIELD_ID)
        reps = reps if isinstance(reps, list) else ([reps] if reps else [])
        if rep_name in reps:
            count += 1
    return count


def rep_ftq_pool_count(all_contacts, rep_name):
    # Not scoped to a date range — this is the rep's entire First-Touch-Qualifier
    # lead pool to date. Denominator for the leaderboard close rate, since a
    # same-day cohort is nearly always empty (the field lags a day or more
    # behind a lead's dateAdded).
    count = 0
    for c in all_contacts:
        reps = custom_field_value(c.get("customFields"), FIRST_TOUCH_QUALIFIER_FIELD_ID)
        reps = reps if isinstance(reps, list) else ([reps] if reps else [])
        if rep_name in reps:
            count += 1
    return count


FIRST_TOUCH_QUALIFIER_STALE_DAYS = 7


def first_touch_qualifier_is_stale(all_contacts):
    # The rolling pool above assumes First-Touch Qualifier tagging is
    # ongoing and just lags a day or so behind dateAdded. If tagging has
    # stopped entirely (as happened starting 2026-07-24), the pool freezes
    # while closedCount keeps climbing, so the rolling rate drifts further
    # past reality every day it's used. Treat the whole mechanism as
    # unreliable once nothing has been tagged in a while.
    latest = None
    for c in all_contacts:
        if not custom_field_value(c.get("customFields"), FIRST_TOUCH_QUALIFIER_FIELD_ID):
            continue
        added = parse_date(c.get("dateAdded"))
        if added and (latest is None or added > latest):
            latest = added
    if latest is None:
        return True
    cutoff = datetime.now(BUSINESS_TZ).date() - timedelta(days=FIRST_TOUCH_QUALIFIER_STALE_DAYS)
    return local_date(latest) < cutoff


def rep_closed_leads_count(contacts, rep_name, start, end):
    # Attributed via the Sales Rep field (not First-Touch Qualifier, unlike
    # rep_leads_count/rep_qualified_leads_count) — closing credit goes to
    # whoever's marked as Sales Rep, which can differ from who first touched
    # the lead. Still anchored on the contact's own dateAdded, not opportunity
    # closed date (see count_closed_leads for the same fix at the totals level).
    count = 0
    for c in contacts:
        added = local_date(parse_date(c.get("dateAdded")))
        if not in_range(added, start, end):
            continue
        stage = custom_field_value(c.get("customFields"), LEAD_STAGE_FIELD_ID)
        if stage != "Closed/Won":
            continue
        reps = custom_field_value(c.get("customFields"), SALES_REP_FIELD_ID)
        reps = reps if isinstance(reps, list) else ([reps] if reps else [])
        if rep_name in reps:
            count += 1
    return count


def count_leads(contacts, start, end):
    return sum(1 for c in contacts if in_range(local_date(parse_date(c.get("dateAdded"))), start, end))


def count_qualified_leads(contacts, start, end):
    count = 0
    for c in contacts:
        added = local_date(parse_date(c.get("dateAdded")))
        if not in_range(added, start, end):
            continue
        status = custom_field_value(c.get("customFields"), QUALIFICATION_STATUS_FIELD_ID)
        if status == "Qualified":
            count += 1
    return count


def count_closed_leads(contacts, start, end):
    # Cohort-consistent version of "closed": anchored to the lead's own
    # creation date (Lead Stage right now), not the opportunity's Closed Date —
    # avoids mixing leads added today with deals that closed today but were
    # created earlier (the mismatch behind the Sergio 150%/333% close rates).
    count = 0
    for c in contacts:
        added = local_date(parse_date(c.get("dateAdded")))
        if not in_range(added, start, end):
            continue
        stage = custom_field_value(c.get("customFields"), LEAD_STAGE_FIELD_ID)
        if stage == "Closed/Won":
            count += 1
    return count


def is_inbound_source(source):
    return "inbound" in (source or "").lower()


def count_sources(contacts, start, end):
    counts = {"Inbound": 0}
    for c in contacts:
        added = local_date(parse_date(c.get("dateAdded")))
        if not in_range(added, start, end):
            continue
        if is_inbound_source(c.get("source")):
            counts["Inbound"] += 1
    return counts


def count_closed_sources(contacts, start, end):
    counts = {"Inbound": 0}
    for c in contacts:
        added = local_date(parse_date(c.get("dateAdded")))
        if not in_range(added, start, end):
            continue
        stage = custom_field_value(c.get("customFields"), LEAD_STAGE_FIELD_ID)
        if stage != "Closed/Won":
            continue
        if is_inbound_source(c.get("source")):
            counts["Inbound"] += 1
    return counts


def compute_period_metrics(start, end, contacts, allowed_contact_ids=None):
    reps = active_reps(end)
    with ThreadPoolExecutor(max_workers=len(reps)) as pool:
        sales_list = list(pool.map(lambda rep: rep_sales(rep, start, end, allowed_contact_ids), reps))

    all_contacts = get_all_contacts_cached()
    rolling_pool_stale = first_touch_qualifier_is_stale(all_contacts)

    total_value = sum(r["value"] for r in sales_list)
    reps_out = []
    for rep, sales in zip(reps, sales_list):
        if rep.get("onlyIfClosed") and not sales["count"]:
            continue
        leads = rep_leads_count(contacts, rep["name"], start, end)
        qualified = rep_qualified_leads_count(contacts, rep["name"], start, end)
        closed_leads = rep_closed_leads_count(contacts, rep["name"], start, end)
        close_rate = round(closed_leads / leads * 100, 1) if leads else None
        qualified_close_rate = round(sales["count"] / qualified * 100, 1) if qualified else None
        ftq_pool = rep_ftq_pool_count(all_contacts, rep["name"])
        # Leaderboard close rate = closed deals this period ÷ the rep's entire
        # all-time First-Touch-Qualifier lead pool (not scoped to this period —
        # a same-day cohort is nearly always empty since the field lags a day
        # or more behind a lead's dateAdded). If tagging has stopped outright
        # (pool frozen) or the rate is over 100%, don't show a number that
        # looks precise but is actually wrong.
        leaderboard_close_rate = round(sales["count"] / ftq_pool * 100, 1) if ftq_pool else None
        if rolling_pool_stale or (leaderboard_close_rate is not None and leaderboard_close_rate > 100):
            leaderboard_close_rate = None
        pct_of_total = round(sales["value"] / total_value * 100, 1) if total_value else 0.0
        reps_out.append({
            "name": rep["name"],
            "closedValue": sales["value"],
            "closedCount": sales["count"],
            "missingClosedDate": sales["missingClosedDate"],
            "leads": leads,
            "qualifiedLeads": qualified,
            "closedLeads": closed_leads,
            "closeRate": close_rate,
            "qualifiedCloseRate": qualified_close_rate,
            "leaderboardCloseRate": leaderboard_close_rate,
            "pctOfTotal": pct_of_total,
        })

    # A contact with Closed Won deals in two reps' pipelines is one sale, not
    # two — totals count unique contacts (keeping the larger deal value).
    unique_sales = {}
    for sales in sales_list:
        for contact_id, v in sales["valueByContact"].items():
            unique_sales[contact_id] = max(unique_sales.get(contact_id, 0), v)
    total_closed_count = len(unique_sales)
    total_leads = count_leads(contacts, start, end)
    total_qualified = count_qualified_leads(contacts, start, end)
    overall_close_rate = round(total_closed_count / total_qualified * 100, 1) if total_qualified else None
    total_closed_leads = count_closed_leads(contacts, start, end)
    total_closed_rate = round(total_closed_leads / total_qualified * 100, 1) if total_qualified else None

    return {
        "reps": reps_out,
        "totals": {
            "closedValue": round(sum(unique_sales.values()), 2),
            "closedCount": total_closed_count,
            "leads": total_leads,
            "qualifiedLeads": total_qualified,
            "closeRate": overall_close_rate,
            "closedLeads": total_closed_leads,
            "totalClosedRate": total_closed_rate,
            "sources": count_sources(contacts, start, end),
            "closedSources": count_closed_sources(contacts, start, end),
        },
    }


def handle_performance(start, end):
    contacts = get_contacts_for(start)
    return compute_period_metrics(start, end, contacts)


def all_inbound_contact_ids():
    return {c.get("id") for c in get_all_contacts_cached() if is_inbound_source(c.get("source"))}


def handle_inbound_calls(start, end):
    contacts = get_contacts_for(start)
    inbound_contacts = [c for c in contacts if is_inbound_source(c.get("source"))]
    # Leads/qualified count by when the contact was created; sales count by
    # the deal's sold (Closed) date, for any inbound lead whenever it came in.
    return compute_period_metrics(start, end, inbound_contacts, allowed_contact_ids=all_inbound_contact_ids())


def _fetch_call_messages_once(start, end, location_id, api_key):
    # One paginated pull. Returns (messages, total) where total is the count
    # HighLevel reports for the range, so callers can tell a partial pull.
    start_utc = datetime.combine(start, datetime.min.time(), BUSINESS_TZ).astimezone(timezone.utc)
    end_utc = datetime.combine(end + timedelta(days=1), datetime.min.time(), BUSINESS_TZ).astimezone(timezone.utc)
    params = {
        "locationId": location_id,
        "channel": "Call",
        "startDate": start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endDate": end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "limit": 200,
    }
    messages = []
    seen = set()
    total = None
    while True:
        data = hl_request("/conversations/messages/export", params, api_key=api_key)
        if total is None:
            total = data.get("total")
        page = [m for m in data.get("messages", []) if m.get("id") not in seen]
        for m in page:
            seen.add(m["id"])
        messages.extend(page)
        cursor = data.get("nextCursor")
        if not page or not cursor:
            return messages, total
        params["cursor"] = cursor


CALL_PULL_GAP_SECONDS = 3
CALL_EMPTY_CONFIRM_GAP_SECONDS = 15
CALL_MAX_PULLS = 10


def fetch_call_messages(start, end, location_id=None, api_key=None):
    # Every call message in the range, newest first. Inbound calls mostly come
    # in as TYPE_IVR_CALL (phone menu), so callers filter on direction, not type.
    # The export endpoint is flaky: it can return nothing for a while, stop
    # short, or even report a wrong total. So a range is pulled repeatedly,
    # keeping every call any pull returns, until two pulls in a row each
    # return the whole set collected so far and that set is at least the
    # largest total HighLevel reported. An empty range is only believed after
    # four empty pulls spread over ~45 seconds.
    location_id = location_id or LOCATION_ID
    by_id = {}
    empty_streak = 0
    complete_streak = 0
    best_total = 0
    for _ in range(CALL_MAX_PULLS):
        pulled, total = _fetch_call_messages_once(start, end, location_id, api_key)
        for m in pulled:
            by_id[m["id"]] = m
        if not pulled:
            complete_streak = 0
            empty_streak += 1
            if not by_id and empty_streak >= 4:
                break
            time.sleep(CALL_EMPTY_CONFIRM_GAP_SECONDS)
            continue
        empty_streak = 0
        best_total = max(best_total, total or 0)
        complete_streak = complete_streak + 1 if len(pulled) == len(by_id) else 0
        if complete_streak >= 2 and len(by_id) >= best_total:
            break
        time.sleep(CALL_PULL_GAP_SECONDS)
    return sorted(by_id.values(), key=lambda m: m.get("dateAdded") or "", reverse=True)


def load_inbound_call_audit():
    # Per-call audit (who handled it, call type, qualified, outcome) read from
    # the transcripts, keyed by message id. Calls not in here show as unaudited.
    # Hand audits win over the transcript checker's automatic ones.
    audit = load_auto_call_audit()
    if os.path.exists(INBOUND_CALL_AUDIT_PATH):
        with open(INBOUND_CALL_AUDIT_PATH) as f:
            audit.update(json.load(f))
    return audit


def handle_inbound_call_log(start, end):
    audit = load_inbound_call_audit()
    contacts_by_id = {c.get("id"): c for c in get_audit_contacts()}
    calls = []
    for m in fetch_call_messages(start, end):
        if m.get("direction") != "inbound":
            continue
        contact = contacts_by_id.get(m.get("contactId")) or {}
        if not is_inbound_source(contact.get("source")):
            continue
        # Only leads created in the selected range — calls from older
        # contacts belong to the period those leads came in.
        if not in_range(local_date(parse_date(contact.get("dateAdded"))), start, end):
            continue
        call_meta = (m.get("meta") or {}).get("call") or {}
        added = parse_date(m.get("dateAdded"))
        name = " ".join(filter(None, [contact.get("firstName"), contact.get("lastName")])) or m.get("from") or "Unknown caller"
        a = audit.get(m["id"]) or {}
        calls.append({
            "id": m["id"],
            "contactId": m.get("contactId"),
            "name": name,
            "source": contact.get("source"),
            "date": local_date(added).isoformat() if added else None,
            "time": added.astimezone(BUSINESS_TZ).strftime("%-I:%M %p") if added else None,
            "duration": call_meta.get("duration") or 0,
            "status": call_meta.get("status"),
            "audited": bool(a),
            "handledBy": a.get("handledBy"),
            "callType": a.get("callType"),
            "qualified": a.get("qualified"),
            "outcome": a.get("outcome"),
            "pest": a.get("pest"),
            "price": a.get("price"),
            "summary": a.get("summary"),
            "notes": a.get("notes"),
        })

    def tally(key):
        counts = {}
        for c in calls:
            if c["audited"]:
                counts[c[key]] = counts.get(c[key], 0) + 1
        return counts

    sales_leads = [c for c in calls if c["callType"] == "Sales lead"]
    return {
        "totalCalls": len(calls),
        "connected": sum(1 for c in calls if c["status"] == "completed"),
        "talkMinutes": round(sum(c["duration"] for c in calls) / 60),
        "uniqueCallers": len({c["contactId"] for c in calls if c["contactId"]}),
        "audited": sum(1 for c in calls if c["audited"]),
        "salesLeads": len(sales_leads),
        "qualified": sum(1 for c in sales_leads if c["qualified"] == "Qualified"),
        "sold": sum(1 for c in calls if c["outcome"] == "Sold"),
        "byHandler": tally("handledBy"),
        "byCallType": tally("callType"),
        "byOutcome": {k: v for k, v in tally("outcome").items() if k not in (None, "N/A")},
        "locationId": LOCATION_ID,
        "calls": calls,
    }


# --- Transcript checker -------------------------------------------------------
# Fills in Qualification Status + First-touch Qualifiers on new Inbound leads
# from their call transcripts, using Mika's audit rules. The dashboard does the
# HighLevel side; the judging is done by a scheduled Claude routine (on the
# Claude plan, so no API bill) that pulls /api/transcript-checker/pending and
# posts its verdicts back to /api/transcript-checker/verdicts.
# Only contacts where BOTH fields are blank are touched — a rep's values are
# never overwritten.
TRANSCRIPT_CHECKER_TOKEN = os.environ.get("TRANSCRIPT_CHECKER_TOKEN", "")
TRANSCRIPT_LOOKBACK_DAYS = 14
# Leads created before this date are left alone (they predate the checker and
# get audited by hand); override with TRANSCRIPT_CHECKER_SINCE=YYYY-MM-DD.
# Mika 2026-10-27: cover ALL Inbound leads, whatever date they came in.
TRANSCRIPT_CHECKER_SINCE = datetime.fromisoformat(os.environ.get("TRANSCRIPT_CHECKER_SINCE", "2000-01-01")).date()
# Leads handed to the routine per run (newest first) so one run stays a sensible size.
TRANSCRIPT_CHECKER_BATCH = int(os.environ.get("TRANSCRIPT_CHECKER_BATCH", "40"))
# contactId -> message ids already judged for Property Type, so a lead left blank
# (unclear) isn't sent again until a new call comes in.
TRANSCRIPT_CHECKER_SEEN_PATH = os.path.join(HERE, "transcript_checker_seen.json")
# HighLevel usually transcribes within minutes; a call with no transcript after
# this long is treated as one that will never get one (missed, too short, etc.).
TRANSCRIPT_WAIT_HOURS = 2
# An "Unknown" verdict is only written once the lead has had this long for a
# real conversation to happen — until then a later call may settle it.
UNKNOWN_WAIT_HOURS = 48
QUALIFICATION_STATUS_OPTIONS = ("Qualified", "Unqualified", "Unknown")
PROPERTY_TYPE_OPTIONS = ("Residential", "Commercial")
FIRST_TOUCH_QUALIFIER_OPTIONS = ("Jay Reyes", "Daniel Barba", "Sergio Anaya", "James Graham Jr.")
CALL_AUDIT_FIELDS = ("handledBy", "callType", "qualified", "pitched", "outcome", "pest", "price", "summary", "notes")
INBOUND_CALL_AUDIT_AUTO_PATH = os.path.join(HERE, "inbound_call_audit_auto.json")

TRANSCRIPT_CHECKER_RULES = """You audit inbound sales calls for Evo Pest Control, a pest control company. \
Each lead comes with every call HighLevel recorded for it, oldest first, with transcripts ("Evo" = our agent, "Caller" = the lead). \
Decide the lead's Qualification Status and First-touch Qualifier, and audit each call.

Qualification Status rules:
- Qualified = the lead is in our service area AND wants a pest service we offer. One-time treatments are offered ONLY for bed bugs and general pest control: a one-time request for those counts as Qualified; a one-time request for anything else (rodents, termites, mosquitoes, wildlife, etc.) is Unqualified, even if the rep pitched a recurring plan or price (Mika, 2026-10-26).
- If the rep pitched a plan or quoted a price, the lead is Qualified — even voles/moles, existing customers adding a service, or callers who then declined.
- If the rep pitched or said we service their area, treat them as in area.
- Unqualified if the caller is calling for someone else (a friend, relative, client, tenant) or is not the one who will pay, e.g. a landlord who will have the renter pay (Mika, 2026-10-30).
- Unqualified = out of service area, a pest we don't service, not a sales lead (pure account/billing/service-visit calls), WDI or real-estate inspection report requests, or the caller declined/hung up before anything could be judged as a sales lead.
- Unknown = it genuinely can't be judged from the transcripts (no real conversation, voicemail only, cut off before the need or location came up).
- Read ALL calls, not just the first: a cut-off or transferred first call is often followed by the real conversation.

First-touch Qualifier = the Evo sales rep who had the first real conversation with the lead. Options: Jay Reyes, Daniel Barba, Sergio Anaya, James Graham Jr. \
On inbound calls the rep's name comes from the greeting ("This is Jay"). Support staff who transfer the call don't count — use the rep they transfer to. \
ALWAYS fill it for Qualified and Unqualified leads (Mika, 2026-10-30): if no real conversation happened, use the rep who answered or made the first call (greeting or callback voicemail "This is Jay with EVO"). For Unknown leads with no real conversation, use null. Use null too when none of the reps ever handled a call. \
Give it to the rep on the RECORDED calls, even if the caller mentions an earlier unrecorded talk with another rep. \
A price relayed only to customer service, with the lead not on the line, does not make the lead Qualified (status Unknown), but that rep is still the First-touch Qualifier.

Property Type = "Residential" (a home, apartment, duplex, rental house, Airbnb, mobile home) or "Commercial" (a business, store, restaurant, office, church, daycare, clinic, treatment facility, warehouse, shop, or a company/property manager calling about a building they run). \
Each lead lists "needs": the fields still blank. If "qualification" is not in needs, send "qualificationStatus": null and "firstTouchQualifier": null (those fields are already filled and won't change). \
Only set Property Type when the transcripts make it clear. Use null when unsure, and ALWAYS null when the lead has no transcript ("hasTranscript": false) — never guess from the name or address (Mika, 2026-10-27).

Per-call audit fields:
- handledBy: rep full name if one of the three reps, "Voicemail/IVR only" if no person answered, else "Unknown".
- callType: "Sales lead", "Existing customer service", "WDI/real estate", "Voicemail lead", "Other/not a lead", or "No conversation".
- qualified: "Qualified", "Unqualified", "Unknown", or "N/A" (not a sales conversation).
- pitched: true/false.
- outcome: "Sold", "Pitched - declined/thinking", "Not pitched", "Callback/follow-up", or "N/A".
- pest: the pest(s) discussed, short, or null. price: the plan/price quoted, short, or null.
- summary: one plain sentence. notes: anything a manager should know (location, objections), or null.
- reason (lead level): one or two sentences explaining the Qualification Status. If it wasn't a sales call, say exactly what the call was about (e.g. "Not a sales call: existing customer calling to cancel because the price went up", "looking for an electrician"), never just "not a sales lead" (Mika, 2026-10-30).

Verdict format (POST a JSON list of these):
{"contactId": "...", "qualificationStatus": "Qualified|Unqualified|Unknown", "firstTouchQualifier": "Jay Reyes|Daniel Barba|Sergio Anaya|James Graham Jr.|null", "propertyType": "Residential|Commercial|null",
 "reason": "...", "calls": [{"messageId": "...", "handledBy": "...", "callType": "...", "qualified": "...", "pitched": false,
 "outcome": "...", "pest": null, "price": null, "summary": "...", "notes": null}]}"""

_transcripts = {}  # message id -> transcript lines (only stored once HighLevel has one)
_checker_status = {"lastPending": None, "lastVerdicts": None, "log": []}
_auto_audit_lock = threading.Lock()


def _checker_log(entry):
    entry["at"] = datetime.now(BUSINESS_TZ).isoformat(timespec="seconds")
    _checker_status["log"] = ([entry] + _checker_status["log"])[:200]
    print(f"transcript-checker: {entry}")


def fetch_transcript_lines(message_id):
    if message_id in _transcripts:
        return _transcripts[message_id]
    try:
        sentences = hl_request(f"/conversations/locations/{LOCATION_ID}/messages/{message_id}/transcription") or []
    except RuntimeError as e:
        if "RECORDING_NOT_FOUND" not in str(e):  # no recording yet, or never will be
            raise
        sentences = []
    lines = [
        ("Evo" if s.get("mediaChannel") == 1 else "Caller") + ": " + s["transcript"]
        for s in sentences
        if isinstance(s, dict) and s.get("transcript")
    ]
    if lines:
        _transcripts[message_id] = lines
    return lines


def load_auto_call_audit():
    if not os.path.exists(INBOUND_CALL_AUDIT_AUTO_PATH):
        return {}
    with open(INBOUND_CALL_AUDIT_AUTO_PATH) as f:
        return json.load(f)


def save_auto_call_audit(entries):
    with _auto_audit_lock:
        audit = load_auto_call_audit()
        audit.update(entries)
        with open(INBOUND_CALL_AUDIT_AUTO_PATH, "w") as f:
            json.dump(audit, f, indent=1)


def _checker_needs(c):
    cfs = c.get("customFields")
    needs = []
    if not custom_field_value(cfs, QUALIFICATION_STATUS_FIELD_ID) and not custom_field_value(cfs, FIRST_TOUCH_QUALIFIER_FIELD_ID):
        needs.append("qualification")
    if not custom_field_value(cfs, PROPERTY_TYPE_FIELD_ID):
        needs.append("propertyType")
    return needs


def _blank_inbound_candidates(since, today):
    return {
        c["id"]: c
        for c in get_audit_contacts()
        if is_inbound_source(c.get("source"))
        and in_range(local_date(parse_date(c.get("dateAdded"))), since, today)
        and _checker_needs(c)
    }


def _contact_call_messages(contact_id):
    # Full call messages (time, direction, duration) for one contact, oldest first.
    out = []
    convs = hl_request("/conversations/search", params={"locationId": LOCATION_ID, "contactId": contact_id}).get("conversations") or []
    for conv in convs:
        last = None
        while True:
            params = {"limit": 100}
            if last:
                params["lastMessageId"] = last
            page = hl_request(f"/conversations/{conv['id']}/messages", params=params).get("messages") or {}
            out += [m for m in page.get("messages") or [] if m.get("messageType") in ("TYPE_CALL", "TYPE_IVR_CALL")]
            if not page.get("nextPage"):
                break
            last = page.get("lastMessageId")
    return sorted(out, key=lambda m: m.get("dateAdded") or "")


def _load_checker_seen():
    try:
        with open(TRANSCRIPT_CHECKER_SEEN_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _mark_checker_seen(contact_id, message_ids):
    with _auto_audit_lock:
        seen = _load_checker_seen()
        seen[contact_id] = sorted(set(seen.get(contact_id, [])) | set(message_ids))
        try:
            with open(TRANSCRIPT_CHECKER_SEEN_PATH, "w") as f:
                json.dump(seen, f)
        except OSError:
            pass


def handle_transcript_checker_pending():
    # Leads whose transcripts are all in (or never coming), ready to be judged.
    main_contacts.sync()  # pick up fields reps filled since the last refresh
    now = datetime.now(timezone.utc)
    today = local_date(now)
    candidates = _blank_inbound_candidates(TRANSCRIPT_CHECKER_SINCE, today)
    added_dates = [local_date(parse_date(c.get("dateAdded"))) for c in candidates.values()]
    since = max(min([d for d in added_dates if d] or [today]), TRANSCRIPT_CHECKER_SINCE)
    calls_by_contact = {}
    # Cached call store (past days are kept on disk), so all-time ranges stay cheap.
    for m in call_store(LOCATION_ID, API_KEY).calls(since, today):
        if m.get("contactId") in candidates:
            calls_by_contact.setdefault(m["contactId"], []).append(m)
    seen = _load_checker_seen()
    # Property-Type-only leads already judged on these same calls are skipped.
    calls_by_contact = {
        cid: ms for cid, ms in calls_by_contact.items()
        if "qualification" in _checker_needs(candidates[cid])
        or not {m["id"] for m in ms} <= set(seen.get(cid, []))
    }
    # Newest leads first, a batch per run.
    order = sorted(calls_by_contact, key=lambda cid: candidates[cid].get("dateAdded") or "", reverse=True)

    leads = []
    for contact_id in order:
        if len(leads) >= TRANSCRIPT_CHECKER_BATCH:
            break
        contact = candidates[contact_id]
        needs = _checker_needs(contact)
        try:
            messages = _contact_call_messages(contact_id)
        except Exception as e:
            _checker_log({"contactId": contact_id, "result": f"error loading calls: {e}"})
            continue
        if not messages:
            continue
        messages.sort(key=lambda m: m.get("dateAdded") or "")
        calls = []
        waiting = False
        for m in messages:
            added = parse_date(m.get("dateAdded"))
            lines = fetch_transcript_lines(m["id"])
            if not lines and (now - added).total_seconds() / 3600 < TRANSCRIPT_WAIT_HOURS:
                waiting = True
                break
            calls.append({
                "messageId": m["id"],
                "direction": m.get("direction"),
                "time": added.astimezone(BUSINESS_TZ).strftime("%Y-%m-%d %-I:%M %p CT"),
                "durationSeconds": ((m.get("meta") or {}).get("call") or {}).get("duration") or 0,
                "transcript": "\n".join(lines) if lines else "(no transcript — missed, voicemail or too short)",
            })
        if waiting:
            continue
        has_transcript = any(not c["transcript"].startswith("(no transcript") for c in calls)
        if needs == ["propertyType"] and not has_transcript:
            continue  # nothing to judge Property Type from (Mika: no transcript = leave blank)
        first_call_age_hours = (now - parse_date(messages[0].get("dateAdded"))).total_seconds() / 3600
        leads.append({
            "contactId": contact_id,
            "name": " ".join(filter(None, [contact.get("firstName"), contact.get("lastName")])) or None,
            "address": ", ".join(filter(None, [contact.get("city"), contact.get("state"), contact.get("postalCode")])) or None,
            "source": contact.get("source"),
            # Unknown verdicts on leads younger than this are held back, not written.
            "unknownAllowed": first_call_age_hours >= UNKNOWN_WAIT_HOURS,
            "added": local_date(parse_date(contact.get("dateAdded"))).isoformat() if contact.get("dateAdded") else None,
            # Fields still blank on this lead; only these are written.
            "needs": needs,
            "hasTranscript": has_transcript,
            "calls": calls,
        })
    _checker_status["lastPending"] = datetime.now(BUSINESS_TZ).isoformat(timespec="seconds")
    return {"rules": TRANSCRIPT_CHECKER_RULES, "leads": leads}


def handle_transcript_checker_unaudited_calls():
    # Inbound calls on Inbound leads from the last couple of weeks that have no
    # per-call audit yet (the call log's "handled by / call type / outcome").
    # The routine audits these and commits them to inbound_call_audit.json.
    now = datetime.now(timezone.utc)
    today = local_date(now)
    audit = load_inbound_call_audit()
    contacts_by_id = {c.get("id"): c for c in get_audit_contacts()}
    calls = []
    for m in fetch_call_messages(today - timedelta(days=TRANSCRIPT_LOOKBACK_DAYS), today):
        if m.get("direction") != "inbound" or m["id"] in audit:
            continue
        contact = contacts_by_id.get(m.get("contactId")) or {}
        if not is_inbound_source(contact.get("source")):
            continue
        added = parse_date(m.get("dateAdded"))
        lines = fetch_transcript_lines(m["id"])
        if not lines and (now - added).total_seconds() / 3600 < TRANSCRIPT_WAIT_HOURS:
            continue
        calls.append({
            "messageId": m["id"],
            "contactId": m.get("contactId"),
            "name": " ".join(filter(None, [contact.get("firstName"), contact.get("lastName")])) or None,
            "source": contact.get("source"),
            "time": added.astimezone(BUSINESS_TZ).strftime("%Y-%m-%d %-I:%M %p CT"),
            "durationSeconds": ((m.get("meta") or {}).get("call") or {}).get("duration") or 0,
            "transcript": "\n".join(lines) if lines else "(no transcript — missed, voicemail or too short)",
        })
    return {"rules": TRANSCRIPT_CHECKER_RULES, "fields": CALL_AUDIT_FIELDS, "calls": calls}


def handle_transcript_checker_verdicts(verdicts):
    results = []
    for v in verdicts if isinstance(verdicts, list) else []:
        contact_id = v.get("contactId")
        status = v.get("qualificationStatus")
        first_touch = v.get("firstTouchQualifier")
        fresh = {}
        if not contact_id or (status is not None and status not in QUALIFICATION_STATUS_OPTIONS):
            results.append({"contactId": contact_id, "result": "rejected — bad contactId or qualificationStatus"})
            continue
        try:
            fresh = hl_request(f"/contacts/{contact_id}").get("contact") or {}
            # Only Inbound leads, and re-check the fields: a rep may have filled them since.
            cfs = fresh.get("customFields")
            if not is_inbound_source(fresh.get("source")):
                result = "rejected — not an Inbound lead"
            elif status is None:
                result = "qualification not changed"
            elif custom_field_value(cfs, QUALIFICATION_STATUS_FIELD_ID) or custom_field_value(cfs, FIRST_TOUCH_QUALIFIER_FIELD_ID):
                result = "skipped — already filled"
            elif status == "Unknown" and (datetime.now(timezone.utc) - parse_date(fresh.get("dateAdded"))).total_seconds() / 3600 < UNKNOWN_WAIT_HOURS:
                result = "held — Unknown on a lead under 48h old"
            else:
                fields = [{"id": QUALIFICATION_STATUS_FIELD_ID, "field_value": status}]
                if first_touch in FIRST_TOUCH_QUALIFIER_OPTIONS:
                    fields.append({"id": FIRST_TOUCH_QUALIFIER_FIELD_ID, "field_value": first_touch})
                # Every status gets its reason in Comments (Mika, 2026-10-30).
                if v.get("reason"):
                    fields.append({"id": COMMENTS_FIELD_ID, "field_value": with_qualification_reason(
                        custom_field_value(cfs, COMMENTS_FIELD_ID), v["reason"], "transcript checker", status
                    )})
                hl_request(f"/contacts/{contact_id}", method="PUT", body={"customFields": fields})
                result = "written"
            # Property Type: only into a blank field, and only when the lead has a real transcript.
            property_type = v.get("propertyType")
            if (
                not result.startswith("rejected")
                and property_type in PROPERTY_TYPE_OPTIONS
                and not custom_field_value(cfs, PROPERTY_TYPE_FIELD_ID)
            ):
                has_transcript = any(
                    fetch_transcript_lines(c["messageId"])
                    for c in v.get("calls") or []
                    if isinstance(c, dict) and c.get("messageId")
                )
                if has_transcript:
                    hl_request(f"/contacts/{contact_id}", method="PUT", body={"customFields": [{"id": PROPERTY_TYPE_FIELD_ID, "field_value": property_type}]})
                    result += f"; property type {property_type}"
                else:
                    result += "; property type skipped (no transcript)"
            if not result.startswith("rejected"):
                _mark_checker_seen(contact_id, [c["messageId"] for c in v.get("calls") or [] if isinstance(c, dict) and c.get("messageId")])
            save_auto_call_audit({
                c["messageId"]: {k: c.get(k) for k in CALL_AUDIT_FIELDS}
                for c in v.get("calls") or []
                if isinstance(c, dict) and c.get("messageId")
            })
        except Exception as e:
            result = f"error: {e}"
        entry = {
            "contactId": contact_id,
            "name": " ".join(filter(None, [fresh.get("firstName"), fresh.get("lastName")])) or None,
            "status": status,
            "firstTouch": first_touch,
            "propertyType": v.get("propertyType"),
            "reason": v.get("reason"),
            "result": result,
        }
        _checker_log(entry)
        results.append(entry)
    _cache.clear()  # the call log and qualification counts read these
    _checker_status["lastVerdicts"] = datetime.now(BUSINESS_TZ).isoformat(timespec="seconds")
    return {"results": results}


# --- Sales Overview -----------------------------------------------------------
# One funnel per channel (leads -> qualified -> sales) with sales from both
# HighLevel (Closed/Won deals) and FieldRoutes (sold subscriptions matched to
# the channel's leads, see with_live_channels). Reps are grouped the way the
# Former reps show by name, only in periods where they closed a deal.

SUB_ACCOUNT_CHANNELS = {
    # Qualified-by-rep uses the rep field each sub-account actually fills in.
    "Warm Outbound": {
        "store": lambda: warm_outbound_contacts,
        "location": lambda: WARM_OUTBOUND_LOCATION_ID,
        "key": lambda: WARM_OUTBOUND_API_KEY,
        "qualification_field": "JluPjqym7pF5CByC0Fj7",  # Lead Qualification Status
        "rep_field": "PnwEnLs4RyBiiKz3d00J",  # Sales Rep
        "closed_date_field": "gmGD1OEr5ritXRSebiEn",
    },
    "Cold Outbound": {
        "store": lambda: cold_outbound_contacts,
        "location": lambda: COLD_OUTBOUND_LOCATION_ID,
        "key": lambda: COLD_OUTBOUND_API_KEY,
        "qualification_field": "fkGG5Ck4AwIV7z9KM4Vk",  # Qualification Status
        "rep_field": COLD_OUTBOUND_LAST_CALLER_FIELD_ID,  # Last Outbound Caller
        "closed_date_field": "IerreF7oDIA3f0rVSJ9C",
    },
}


# Warm Outbound lead types, by the tag each lead gets on arrival.
WARM_LEAD_TYPES = [("Meta", "meta leads"), ("Website", "website leads"), ("Snoball", "snoball")]


# Leads without one of those tags get a specific type, never "Other" (Mika, 2026-10-30).
WARM_EXTRA_TYPES = ["Missed Inbound", "Referral", "Phone call-in", "Added manually"]


def warm_lead_type(contact):
    tags = {t.lower() for t in contact.get("tags") or []}
    tagged = next((name for name, tag in WARM_LEAD_TYPES if tag in tags), None)
    if tagged:
        return tagged
    source = (contact.get("source") or "").lower()
    if "missed inbound" in tags or "missed inbound" in source:
        return "Missed Inbound"
    if "referral" in source or "referral" in tags:
        return "Referral"
    if tags & {"name via lookup", "couldn't find caller name"}:
        return "Phone call-in"
    return "Added manually"


def _warm_breakdown(contacts, leads, hl_deals, fr_sales):
    # Leads received, sales and contract value per Warm lead type.
    type_of = {c.get("id"): warm_lead_type(c) for c in contacts}
    hl_by_contact = {}
    for _, contact_id, value in hl_deals:
        hl_by_contact[contact_id] = max(value, hl_by_contact.get(contact_id, 0))
    names = [name for name, _ in WARM_LEAD_TYPES] + WARM_EXTRA_TYPES
    out = []
    for name in names:
        fr = [sale for sale in fr_sales if type_of.get(sale.get("leadContactId")) == name]
        hl = [v for cid, v in hl_by_contact.items() if type_of.get(cid) == name]
        row = {
            "name": name,
            "leads": sum(1 for c in leads if type_of.get(c.get("id")) == name),
            "frCount": len(fr),
            "frAcv": round(sum(sale["contractValue"] for sale in fr), 2),
            "hlCount": len(hl),
            "hlValue": round(sum(hl), 2),
        }
        if name not in WARM_EXTRA_TYPES or any(row[k] for k in ("leads", "frCount", "hlCount")):
            out.append(row)
    return out


def overview_rep(name):
    return name


def overview_reps(end, channels=()):
    # Core reps active in the period, plus any former rep who closed a deal in it.
    reps = [r for r in CORE_REPS if rep_active(r, end)]
    closers = {r["name"] for ch in channels for r in ch["reps"] if r["hlCount"] or r["frCount"]}
    return reps + [r for r in FORMER_REPS if r in closers] + sorted(closers - set(reps) - set(FORMER_REPS))


def _is_closed_won_stage(name):
    # "Closed/Won" in the sub-accounts, "🤝 Closed Won" in the main account.
    name = (name or "").lower()
    return "closed" in name and "won" in name


def closed_won_deals(cache_name, location, api_key):
    # Every Closed/Won deal in an account's per-rep pipelines (each pipeline
    # is named after its rep), so new reps' pipelines count automatically.
    key = ("closed-won", cache_name)
    hit = _opps_cache.get(key)
    if hit and time.monotonic() - hit["fetchedAt"] < DATA_CACHE_TTL_SECONDS:
        return hit["opps"]
    deals = []
    for pipeline in hl_request("/opportunities/pipelines", {"locationId": location}, api_key=api_key).get("pipelines", []):
        for stage in pipeline.get("stages", []):
            if not _is_closed_won_stage(stage.get("name")):
                continue
            for opp in fetch_all_opportunities(pipeline["id"], stage["id"], location, api_key):
                deals.append((pipeline.get("name"), opp))
    _opps_cache[key] = {"fetchedAt": time.monotonic(), "opps": deals}
    return deals


def sub_account_closed_won(channel):
    cfg = SUB_ACCOUNT_CHANNELS[channel]
    return closed_won_deals(channel, cfg["location"](), cfg["key"]())


def _rep_list(value):
    return value if isinstance(value, list) else ([value] if value else [])


def _channel_summary(name, leads, qualified, qualified_by_rep, hl_deals, fr_sales, end):
    # hl_deals: (rep, contact_id, value); one sale per contact, larger value kept.
    by_contact = {}
    for rep, contact_id, value in hl_deals:
        if contact_id not in by_contact or value > by_contact[contact_id][1]:
            by_contact[contact_id] = (rep, value)
    reps = []
    names = [r for r in CORE_REPS if rep_active(r, end)]
    names += sorted({r for r, _ in by_contact.values()} | {sale["group"] for sale in fr_sales} | set(qualified_by_rep) - set(names))
    for rep in dict.fromkeys(names):
        hl = [v for r, v in by_contact.values() if r == rep]
        fr = [sale for sale in fr_sales if sale["group"] == rep]
        q = qualified_by_rep.get(rep, 0)
        reps.append({
            "name": rep,
            "qualified": q,
            "hlCount": len(hl),
            "hlValue": round(sum(hl), 2),
            "frCount": len(fr),
            "frAcv": round(sum(sale["contractValue"] for sale in fr), 2),
            "hlCloseRate": round(len(hl) / q * 100, 1) if q else None,
            "frCloseRate": round(len(fr) / q * 100, 1) if q else None,
        })
    # Anyone but the active core reps shows only when they closed a deal in
    # the period (Mika, 2026-10-30); "Customer nurture" isn't a rep.
    core = [r for r in CORE_REPS if rep_active(r, end)]
    reps = [r for r in reps if r["name"] in core or ((r["hlCount"] or r["frCount"]) and r["name"] != "Customer nurture")]
    order = core + FORMER_REPS
    reps.sort(key=lambda r: (order.index(r["name"]) if r["name"] in order else len(order), r["name"]))
    hl_count = len(by_contact)
    return {
        "name": name,
        "leads": leads,
        "qualified": qualified,
        "hlCount": hl_count,
        "hlValue": round(sum(v for _, v in by_contact.values()), 2),
        "frCount": len(fr_sales),
        "frAcv": round(sum(sale["contractValue"] for sale in fr_sales), 2),
        "hlCloseRate": round(hl_count / qualified * 100, 1) if qualified else None,
        "frCloseRate": round(len(fr_sales) / qualified * 100, 1) if qualified else None,
        "reps": reps,
    }


def inbound_source_leads(start=None):
    # Inbound leads = every main-account contact whose source says "Inbound",
    # May 5 imports included (Mika, 2026-10-29). Commercial leads are left out
    # and shown on the Commercial tab only (Mika, 2026-10-30); test/junk tags dropped.
    return [c for c in _get_all_contacts_raw()
            if is_inbound_source(c.get("source")) and not is_tag_excluded(c) and not is_commercial_contact(c)]


def handle_sales_overview(start, end):
    snapshot = load_revenue_snapshot()
    sales = with_live_channels(snapshot)["sales"] if snapshot else []
    in_range = [sale for sale in sales if start.isoformat() <= sale["sold"] <= end.isoformat()]
    fr_by_channel = lambda ch: [sale for sale in in_range if sale.get("channel") == ch]
    channels = []

    inbound = inbound_source_leads(start)
    leads = [c for c in inbound if start <= (local_date(parse_date(c.get("dateAdded"))) or date_min) <= end]
    qualified = [c for c in leads if custom_field_value(c.get("customFields"), QUALIFICATION_STATUS_FIELD_ID) == "Qualified"]
    q_by_rep = {}
    for c in qualified:
        for rep in _rep_list(custom_field_value(c.get("customFields"), FIRST_TOUCH_QUALIFIER_FIELD_ID)):
            q_by_rep[overview_rep(rep)] = q_by_rep.get(overview_rep(rep), 0) + 1
    excluded_ids = get_excluded_contact_ids()
    hl_deals = [
        (overview_rep(rep_name), opp.get("contactId"), float(opp.get("monetaryValue") or 0))
        for rep_name, opp in closed_won_deals("Inbound", LOCATION_ID, API_KEY)
        if not is_test_opportunity(opp) and opp.get("contactId") not in excluded_ids
        and _deal_closed_on(opp, CLOSED_DATE_FIELD_ID, start, end)
    ]
    # Close rate only compares like with like: Inbound sales whose lead has an
    # "Inbound" source. Sales from older-named sources (Yelp, GMB, Field Routes,
    # no source...) are reported separately (Mika, 2026-10-29).
    inbound_ids = {c.get("id") for c in inbound}
    fr_inbound = [sale for sale in fr_by_channel("Inbound") if sale.get("leadContactId") in inbound_ids]
    fr_older = [sale for sale in fr_by_channel("Inbound") if sale.get("leadContactId") not in inbound_ids]
    summary = _channel_summary("Inbound", len(leads), len(qualified), q_by_rep, hl_deals, fr_inbound, end)
    summary["olderSources"] = {"count": len(fr_older), "acv": round(sum(sale["contractValue"] for sale in fr_older), 2)}
    channels.append(summary)

    for name, cfg in SUB_ACCOUNT_CHANNELS.items():
        if not cfg["key"]() or not cfg["location"]():
            continue
        contacts = cfg["store"]().contacts()
        leads = [c for c in contacts if start <= (local_date(parse_date(c.get("dateAdded"))) or date_min) <= end]
        qualified = [c for c in leads if custom_field_value(c.get("customFields"), cfg["qualification_field"]) == "Qualified"]
        q_by_rep = {}
        for c in qualified:
            for rep in _rep_list(custom_field_value(c.get("customFields"), cfg["rep_field"])):
                q_by_rep[overview_rep(rep)] = q_by_rep.get(overview_rep(rep), 0) + 1
        hl_deals = [
            (overview_rep(rep_name), opp.get("contactId"), float(opp.get("monetaryValue") or 0))
            for rep_name, opp in sub_account_closed_won(name)
            if not is_test_opportunity(opp) and _deal_closed_on(opp, cfg["closed_date_field"], start, end)
        ]
        summary = _channel_summary(name, len(leads), len(qualified), q_by_rep, hl_deals, fr_by_channel(name), end)
        if name == "Warm Outbound":
            summary["leadTypes"] = _warm_breakdown(contacts, leads, hl_deals, fr_by_channel(name))
        if name == "Cold Outbound":
            users = location_user_names(cfg["location"](), cfg["key"]())
            calls = [m for m in call_store(cfg["location"](), cfg["key"]()).calls(start, end)
                     if m.get("direction") == "outbound" and is_rep_call(m, users)]
            by_rep = {}
            for m in calls:
                rep = call_rep_label(m, users)
                by_rep[rep] = by_rep.get(rep, 0) + 1
            summary["callsMade"] = len(calls)
            # Same reps, same order in "Calls made" and "Sold contracts" (Mika,
            # 2026-10-30): everyone who called, most calls first, then anyone
            # who sold without calling.
            order = [r for r, _ in sorted(by_rep.items(), key=lambda kv: -kv[1])]
            have = {r["name"] for r in summary["reps"]}
            summary["reps"] += [{"name": r, "qualified": 0, "hlCount": 0, "hlValue": 0, "frCount": 0, "frAcv": 0,
                                 "hlCloseRate": None, "frCloseRate": None} for r in order if r not in have]
            order += [r["name"] for r in summary["reps"] if r["name"] not in order]
            summary["reps"].sort(key=lambda r: order.index(r["name"]))
            summary["callsByRep"] = [{"name": r, "calls": by_rep.get(r, 0)} for r in order]
        channels.append(summary)

    other = {ch: {"count": len(fr_by_channel(ch)), "acv": round(sum(sale["contractValue"] for sale in fr_by_channel(ch)), 2)}
             for ch in ("Not matched",)}
    return {"reps": overview_reps(end, channels), "channels": channels, "otherFieldRoutes": other,
            "dataThrough": snapshot.get("dataThrough") if snapshot else None}


date_min = datetime.min.date()


TEAM_CHANNELS = ["Inbound", "Warm Outbound", "Cold Outbound"]
LEAD_CHANNELS = ["Inbound", "Warm Outbound"]

# Lead Sources chart: Inbound, then each Warm lead type as the platform the
# lead came from (Mika, 2026-10-30).
PLATFORM_NAMES = {"gmb": "Google Business Profile", "google lsa": "Google LSA", "facebook ads": "Meta Ads",
                  "truck number": "Truck Number", "fieldroutes": "FieldRoutes", "arco": "Arco",
                  "main pest ai": "Main Pest AI"}
WARM_PLATFORM_NAMES = {"Meta": "Meta Ads", "Website": "Website Form", "Phone call-in": "Warm Phone Call-in",
                       "Missed Inbound": "Missed Inbound Call", "Referral": "Referral", "Added manually": "Added Manually"}


def inbound_platform(contact):
    parts = [p.strip() for p in (contact.get("source") or "").split("|")]
    platform = parts[-1] if len(parts) > 1 else ""
    if not platform:
        return "Inbound Call (no platform set)"
    return PLATFORM_NAMES.get(platform.lower(), platform)


def team_lead_contacts(start, end):
    # (channel, location id, contact, platform) for every Inbound + Warm lead added in the range.
    in_range = lambda c: start <= (local_date(parse_date(c.get("dateAdded"))) or date_min) <= end
    # GMB, Truck Number, FieldRoutes... are all phone calls to the Inbound lines,
    # so they count as one "Inbound" source (Mika, 2026-10-30).
    out = [("Inbound", LOCATION_ID, c, "Inbound") for c in inbound_source_leads(start) if in_range(c)]
    cfg = SUB_ACCOUNT_CHANNELS["Warm Outbound"]
    if cfg["key"]() and cfg["location"]():
        out += [("Warm Outbound", cfg["location"](), c, WARM_PLATFORM_NAMES.get(warm_lead_type(c), warm_lead_type(c)))
                for c in cfg["store"]().contacts() if in_range(c)]
    return out


def handle_team_combined(start, end):
    # Sales Team tab (Mika, 2026-10-30): leads received, contracts closed and
    # contract value combined across Inbound + Warm Outbound + Cold Outbound
    # (every FieldRoutes sale in the period except Commercial, same as the
    # "FieldRoutes Sales by Channel" table); close rate Inbound only.
    overview = cached(("sales-overview", start, end), lambda: handle_sales_overview(start, end))
    channels = [c for c in overview["channels"] if c["name"] in TEAM_CHANNELS]
    snapshot = load_revenue_snapshot()
    sales = [s for s in (with_live_channels(snapshot)["sales"] if snapshot else [])
             if start.isoformat() <= s["sold"] <= end.isoformat() and s.get("channel") != "Commercial"]
    inbound = next((c for c in channels if c["name"] == "Inbound"), None)
    inbound_reps = {r["name"]: r for r in (inbound["reps"] if inbound else [])}
    qualified_by = {}
    for ch in channels:
        for r in ch["reps"]:
            qualified_by[r["name"]] = qualified_by.get(r["name"], 0) + r["qualified"]
    total_value = sum(s["contractValue"] for s in sales)
    names = [r for r in CORE_REPS if rep_active(r, end)]
    names += [n for n in dict.fromkeys(s["group"] for s in sales) if n not in names]
    out = []
    for name in names:
        mine = [s for s in sales if s["group"] == name]
        q = qualified_by.get(name, 0)
        # Jay and Daniel always; anyone else only with sales or qualified leads.
        if name not in ("Jay Reyes", "Daniel Barba") and not mine and not q:
            continue
        value = round(sum(s["contractValue"] for s in mine), 2)
        ib = inbound_reps.get(name) or {}
        out.append({
            "name": name,
            "closedCount": len(mine),
            "closedValue": value,
            "qualifiedLeads": q,
            "inboundQualified": ib.get("qualified", 0),
            "closeRate": ib.get("frCloseRate"),
            "pctOfTotal": round(value / total_value * 100, 1) if total_value else 0,
        })
    by_channel = {c: sum(1 for s in sales if s.get("channel") == c) for c in TEAM_CHANNELS + ["Not matched"]}
    platforms = {}
    for _, _, _, platform in team_lead_contacts(start, end):
        platforms[platform] = platforms.get(platform, 0) + 1
    return {
        "reps": out,
        "totals": {
            # Leads received = Inbound + Warm Outbound only (Mika, 2026-10-30);
            # Cold Outbound shows as rep call counts instead.
            "leads": sum(c["leads"] for c in channels if c["name"] in LEAD_CHANNELS),
            "qualifiedLeads": sum(c["qualified"] for c in channels if c["name"] in LEAD_CHANNELS),
            "closedCount": len(sales),
            "closedValue": round(total_value, 2),
            "closeRate": inbound["frCloseRate"] if inbound else None,
            "closeRateNote": f"Inbound only: {inbound['frCount']} sales / {inbound['qualified']} qualified" if inbound else "Inbound only",
            "sources": dict(sorted(platforms.items(), key=lambda kv: -kv[1])),
            "closedSources": by_channel,
        },
        "warmCalls": _warm_rep_calls(start, end),
        "coldCalls": next((c.get("callsMade") for c in channels if c["name"] == "Cold Outbound"), None),
        "dataThrough": overview.get("dataThrough"),
    }


def _warm_rep_calls(start, end):
    # Outgoing calls sales reps made from the Warm Outbound account (same rep-only rule as Cold).
    if not (WARM_OUTBOUND_API_KEY and WARM_OUTBOUND_LOCATION_ID):
        return None
    users = location_user_names(WARM_OUTBOUND_LOCATION_ID, WARM_OUTBOUND_API_KEY)
    calls = [m for m in call_store(WARM_OUTBOUND_LOCATION_ID, WARM_OUTBOUND_API_KEY).calls(start, end)
             if m.get("direction") == "outbound" and is_rep_call(m, users)]
    by_rep = {}
    for m in calls:
        by_rep[call_rep_label(m, users)] = by_rep.get(call_rep_label(m, users), 0) + 1
    return {"total": len(calls), "byRep": [{"name": r, "calls": n} for r, n in sorted(by_rep.items(), key=lambda kv: -kv[1])]}


def handle_commercial(start, end):
    # Main-account leads with Property Type = Commercial, laid out like one
    # Sales Overview channel. They're left out of every other dashboard.
    snapshot = load_revenue_snapshot()
    sales = with_live_channels(snapshot)["sales"] if snapshot else []
    fr = [sale for sale in sales if sale.get("channel") == "Commercial" and start.isoformat() <= sale["sold"] <= end.isoformat()]
    commercial = get_commercial_contacts()
    commercial_ids = {c.get("id") for c in commercial}
    leads = [c for c in commercial if start <= (local_date(parse_date(c.get("dateAdded"))) or date_min) <= end]
    qualified = [c for c in leads if custom_field_value(c.get("customFields"), QUALIFICATION_STATUS_FIELD_ID) == "Qualified"]
    q_by_rep = {}
    for c in qualified:
        for rep in _rep_list(custom_field_value(c.get("customFields"), FIRST_TOUCH_QUALIFIER_FIELD_ID)):
            q_by_rep[overview_rep(rep)] = q_by_rep.get(overview_rep(rep), 0) + 1
    hl_deals = [
        (overview_rep(rep_name), opp.get("contactId"), float(opp.get("monetaryValue") or 0))
        for rep_name, opp in closed_won_deals("Inbound", LOCATION_ID, API_KEY)
        if opp.get("contactId") in commercial_ids and not is_test_opportunity(opp)
        and _deal_closed_on(opp, CLOSED_DATE_FIELD_ID, start, end)
    ]
    channels = [_channel_summary("Commercial", len(leads), len(qualified), q_by_rep, hl_deals, fr, end)]
    return {"reps": overview_reps(end, channels), "channels": channels,
            "taggedCommercial": len(commercial), "dataThrough": snapshot.get("dataThrough") if snapshot else None}


def _deal_closed_on(opp, closed_date_field, start, end):
    # Closed Date field when set, else the day the deal was created (same
    # fallback the Sales tab uses for main-account deals).
    closed_dt = parse_date(custom_field_value(opp.get("customFields"), closed_date_field))
    closed = utc_date_only(closed_dt) if closed_dt else local_date(parse_date(opp.get("createdAt") or opp.get("dateAdded")))
    return closed is not None and start <= closed <= end


# --- Call Count ---------------------------------------------------------------
# Every call logged in each HighLevel account (main = Inbound, plus the Warm
# and Cold Outbound sub-accounts) for the range, by the user who made/took it.
# Inbound phone-menu calls carry no user, so they're grouped as "No rep".
_users_cache = {}


KNOWN_OLD_USERS = {
    "JUGrZ3c7aw0XZJipG2hv": "Jay Reyes",     # 404 on /users; "This is Jay with Evo" on its calls
    "vnTmQZ5Hj0G62jbuLBcv": "Daniel Barba",  # not a main-account user; "This is Daniel" on its calls
    "oJQH2cnA8rODttA2fsd7": "James",         # 404 on /users; "This is James" (termite inspection scheduling)
    "TlGxGbhydmIu5vebr9fu": "Shaquille Munoz",  # 404 on /users; "Shaquille from Evo" on its Cold Outbound calls
}


def is_rep_call(m, users):
    # Calls a sales rep actually made (Mika, 2026-10-30): no workflow ringless
    # voicemail drops (they show under office-line users), no calls without a
    # user, no office phone lines or test calls.
    if m.get("messageType") == "TYPE_CAMPAIGN_VOICEMAIL":
        return False
    label = call_rep_label(m, users)
    return not (label.startswith("No rep") or label == "Unknown user" or label == "Mika Monte"
                or "office" in label.lower() or any(ch.isdigit() for ch in label))


def location_user_names(location_id, api_key):
    hit = _users_cache.get(location_id)
    if hit and time.monotonic() - hit[0] < DATA_CACHE_TTL_SECONDS:
        return hit[1]
    users = hl_request("/users/", {"locationId": location_id}, api_key=api_key).get("users", [])
    names = {u["id"]: u.get("name") or " ".join(filter(None, [u.get("firstName"), u.get("lastName")])) for u in users}
    # Old/removed user accounts HighLevel can no longer name, identified from the
    # reps' greetings on their calls (Mika, 2026-10-27). Live users win.
    names = {**KNOWN_OLD_USERS, **names}
    _users_cache[location_id] = (time.monotonic(), names)
    return names


def _call_status(m):
    return ((m.get("meta") or {}).get("call") or {}).get("status") or m.get("status")


def _call_duration(m):
    return ((m.get("meta") or {}).get("call") or {}).get("duration") or m.get("duration") or 0


def _call_tally(messages):
    # Same buckets as HighLevel's call dashboard: completed = Answered,
    # no-answer = Missed/No answer; busy/failed are counted separately.
    statuses = [_call_status(m) for m in messages]
    return {
        "total": len(messages),
        "outbound": sum(1 for m in messages if m.get("direction") == "outbound"),
        "inbound": sum(1 for m in messages if m.get("direction") != "outbound"),
        "answered": statuses.count("completed"),
        "missed": statuses.count("no-answer"),
        "busyFailed": statuses.count("busy") + statuses.count("failed"),
        "durationSeconds": sum(_call_duration(m) for m in messages),
    }


def call_rep_label(m, users):
    # One rule for every call chart: the user on the call, else "No rep recorded"
    # (+ "(incoming)" only when the call really was incoming).
    if m.get("userId"):
        return users.get(m.get("userId")) or "Unknown user"
    return "No rep recorded (incoming)" if m.get("direction") != "outbound" else "No rep recorded"


def summarize_calls(name, messages, users):
    # Rep = the user on the call, which is HighLevel's "Call attended by" (the
    # contact's "Assigned To" is often a shared office line instead).
    groups = {}
    for m in messages:
        rep = call_rep_label(m, users)
        groups.setdefault(rep, []).append(m)
    by_rep = [dict(name=rep, **_call_tally(msgs)) for rep, msgs in groups.items()]
    return dict(
        name=name,
        uniqueContacts=len({m.get("contactId") for m in messages if m.get("contactId")}),
        byRep=sorted(by_rep, key=lambda r: -r["total"]),
        **_call_tally(messages),
    )


TODAY_CALLS_TTL_SECONDS = 300
CALL_FETCH_CHUNK_DAYS = 7  # a whole year in one export request times out on HighLevel's side
CALL_PREFETCH_DAYS = 90
CALL_SLIM_FIELDS = ("id", "direction", "status", "userId", "contactId")


class CallStore:
    # Calls for one HighLevel account, bucketed by business day. Finished days
    # never change, so they're kept (in memory and in a local JSON file) and
    # only today plus days not seen yet are fetched — a week at a time.
    def __init__(self, location_id, api_key):
        self.location_id = location_id
        self.api_key = api_key
        self.path = os.path.join(HERE, f"call_cache_{location_id}.json")
        self._days = {}
        self._today_fetched = {}  # day -> monotonic time today's (still changing) calls were last pulled
        self._lock = threading.Lock()
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self._days = json.load(f)
            except (OSError, ValueError):
                self._days = {}

    def _fetch(self, start, end):
        days = {}
        for m in fetch_call_messages(start, end, self.location_id, self.api_key):
            added = parse_date(m.get("dateAdded"))
            if added is None:
                continue
            slim = {k: m.get(k) for k in CALL_SLIM_FIELDS}
            call = (m.get("meta") or {}).get("call") or {}
            slim["status"] = call.get("status") or m.get("status")
            slim["duration"] = call.get("duration") or 0
            days.setdefault(local_date(added).isoformat(), []).append(slim)
        return days

    def _save(self, today):
        try:
            with open(self.path, "w") as f:
                json.dump({k: v for k, v in self._days.items() if k < today.isoformat()}, f)
        except OSError:
            pass

    def calls(self, start, end):
        today = datetime.now(BUSINESS_TZ).date()
        with self._lock:
            missing = []
            d = start
            while d <= end:
                # Today keeps changing, but re-pulling it on every request made the
                # Sales Overview take ~1 min (Cold Outbound's export is slow), so
                # today's calls are reused for TODAY_CALLS_TTL_SECONDS.
                fresh_today = d >= today and time.monotonic() - self._today_fetched.get(d.isoformat(), -1e9) < TODAY_CALLS_TTL_SECONDS
                if (d >= today and not fresh_today) or d.isoformat() not in self._days:
                    missing.append(d)
                d += timedelta(days=1)
        # Contiguous runs of missing days, fetched in week-sized chunks.
        runs, run = [], []
        for d in missing:
            if run and (d - run[-1]).days != 1 or len(run) == CALL_FETCH_CHUNK_DAYS:
                runs.append(run)
                run = []
            run.append(d)
        if run:
            runs.append(run)
        for run in runs:
            # HighLevel is called WITHOUT holding the lock: the startup prefetch pulls
            # months of calls, and holding the lock made every page that needs calls
            # wait behind it for minutes (Mika saw This Month never load, 2026-10-27).
            # A busy week can time out on HighLevel's side; retry it rather than
            # failing the whole range, and save each finished week.
            for attempt in range(3):
                try:
                    fetched = self._fetch(run[0], run[-1])
                    break
                except RuntimeError:
                    if attempt == 2:
                        raise
                    time.sleep(5 * (attempt + 1))
            with self._lock:
                for d in run:
                    self._days[d.isoformat()] = fetched.get(d.isoformat(), [])
                    if d >= today:
                        self._today_fetched[d.isoformat()] = time.monotonic()
                if run[0] < today:
                    self._save(today)
        with self._lock:
            result = []
            d = start
            while d <= end:
                result.extend(self._days.get(d.isoformat(), []))
                d += timedelta(days=1)
            return result


_call_stores = {}


def _refresh_today_calls(store):
    # Pull today's calls outside the lock (it can take ~1 min) and swap them in,
    # so page requests keep reading the previous copy meanwhile.
    today = datetime.now(BUSINESS_TZ).date()
    fetched = store._fetch(today, today)
    with store._lock:
        store._days[today.isoformat()] = fetched.get(today.isoformat(), [])
        store._today_fetched[today.isoformat()] = time.monotonic()


def call_store(location_id, api_key):
    if location_id not in _call_stores:
        _call_stores[location_id] = CallStore(location_id, api_key)
    return _call_stores[location_id]


def call_count_accounts():
    accounts = [("Inbound", LOCATION_ID, API_KEY)]
    if WARM_OUTBOUND_API_KEY and WARM_OUTBOUND_LOCATION_ID:
        accounts.append(("Warm Outbound", WARM_OUTBOUND_LOCATION_ID, WARM_OUTBOUND_API_KEY))
    if COLD_OUTBOUND_API_KEY and COLD_OUTBOUND_LOCATION_ID:
        accounts.append(("Cold Outbound", COLD_OUTBOUND_LOCATION_ID, COLD_OUTBOUND_API_KEY))
    return accounts


def handle_call_count(start, end):
    accounts = call_count_accounts()

    def one(account):
        name, location_id, api_key = account
        messages = call_store(location_id, api_key).calls(start, end)
        return summarize_calls(name, messages, location_user_names(location_id, api_key))

    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        return {"accounts": list(pool.map(one, accounts))}


def prefetch_calls():
    # Fill the last few months in the background so long ranges open fast.
    def _run():
        end = datetime.now(BUSINESS_TZ).date()
        for _, location_id, api_key in call_count_accounts():
            try:
                call_store(location_id, api_key).calls(end - timedelta(days=CALL_PREFETCH_DAYS), end)
            except Exception as e:
                print(f"call prefetch failed for {location_id}: {e}")
        # Keep today's calls warm so the Sales Overview never waits on HighLevel.
        while True:
            time.sleep(TODAY_CALLS_TTL_SECONDS - 60)
            for _, location_id, api_key in call_count_accounts():
                try:
                    _refresh_today_calls(call_store(location_id, api_key))
                except Exception as e:
                    print(f"today's call refresh failed for {location_id}: {e}")

    threading.Thread(target=_run, daemon=True).start()


def handle_performance_compare(start, end, prev_start, prev_end):
    # Fetch contacts once, covering whichever period starts earlier, so both
    # periods are computed from a single shared dataset instead of two
    # independent (and possibly racing/duplicated) fetches.
    earliest_start = min(start, prev_start)
    contacts = get_contacts_for(earliest_start)
    return {
        "current": compute_period_metrics(start, end, contacts),
        "previous": compute_period_metrics(prev_start, prev_end, contacts),
    }


def compute_live_power_dialer():
    # GHL's "Power Dialer Attempt Today" only reflects the current day and gets
    # reset/overwritten over time, so this can only ever answer "today" — past
    # days must come from our own saved daily snapshots instead.
    contacts = get_all_contacts_cached()

    with ThreadPoolExecutor(max_workers=len(REPS)) as pool:
        opps_by_rep = list(pool.map(lambda rep: get_opportunities_for(rep["pipelineId"], rep["closedWonStageId"]), REPS))
    opp_value_by_contact = {}
    for opps in opps_by_rep:
        for opp in opps:
            if is_test_opportunity(opp):
                continue
            contact_id = opp.get("contactId")
            if contact_id:
                opp_value_by_contact[contact_id] = opp_value_by_contact.get(contact_id, 0) + float(opp.get("monetaryValue") or 0)

    called = 0
    closed = 0
    contract_value = 0.0
    for c in contacts:
        attempts = custom_field_value(c.get("customFields"), POWER_DIALER_ATTEMPT_FIELD_ID)
        try:
            attempts_num = float(attempts) if attempts is not None else 0
        except (TypeError, ValueError):
            attempts_num = 0
        if attempts_num >= 1:
            called += 1
            lead_stage = custom_field_value(c.get("customFields"), LEAD_STAGE_FIELD_ID)
            if lead_stage == "Closed/Won":
                closed += 1
                contract_value += opp_value_by_contact.get(c.get("id"), 0)
    return {"called": called, "closed": closed, "contractValue": round(contract_value, 2)}


_history_lock = threading.Lock()


def load_power_dialer_history():
    with _history_lock:
        if not os.path.exists(POWER_DIALER_HISTORY_PATH):
            return {}
        with open(POWER_DIALER_HISTORY_PATH) as f:
            return json.load(f)


def save_power_dialer_snapshot(date_key, called, closed, contract_value):
    with _history_lock:
        history = {}
        if os.path.exists(POWER_DIALER_HISTORY_PATH):
            with open(POWER_DIALER_HISTORY_PATH) as f:
                history = json.load(f)
        history[date_key] = {"called": called, "closed": closed, "contractValue": contract_value}
        with open(POWER_DIALER_HISTORY_PATH, "w") as f:
            json.dump(history, f, indent=2)


def snapshot_power_dialer_today():
    live = compute_live_power_dialer()
    today_key = datetime.now(BUSINESS_TZ).date().isoformat()
    save_power_dialer_snapshot(today_key, live["called"], live["closed"], live["contractValue"])
    return live


def handle_power_dialer(start, end):
    today = datetime.now(BUSINESS_TZ).date()
    history = load_power_dialer_history()
    total_called = 0
    total_closed = 0
    total_contract_value = 0.0
    missing_days = []
    d = start
    while d <= end:
        if d == today:
            live = snapshot_power_dialer_today()
            total_called += live["called"]
            total_closed += live["closed"]
            total_contract_value += live["contractValue"]
        else:
            entry = history.get(d.isoformat())
            if entry:
                total_called += entry["called"]
                total_closed += entry["closed"]
                total_contract_value += entry.get("contractValue", 0)
            else:
                missing_days.append(d.isoformat())
        d += timedelta(days=1)
    return {
        "called": total_called,
        "closed": total_closed,
        "contractValue": round(total_contract_value, 2),
        "missingDays": missing_days,
    }



def tally_field(contacts, field_id, start, end):
    counts = {}
    unset = 0
    total_in_range = 0
    for c in contacts:
        added = local_date(parse_date(c.get("dateAdded")))
        if not in_range(added, start, end):
            continue
        total_in_range += 1
        value = custom_field_value(c.get("customFields"), field_id)
        if value is None or value == "" or value == []:
            unset += 1
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            counts[v] = counts.get(v, 0) + 1
    return {"counts": counts, "unset": unset, "totalInRange": total_in_range}


def handle_qualification(start, end):
    contacts = get_contacts_for(start)
    qualification_status = tally_field(contacts, QUALIFICATION_STATUS_FIELD_ID, start, end)
    first_touch = tally_field(contacts, FIRST_TOUCH_QUALIFIER_FIELD_ID, start, end)
    return {
        "qualificationStatus": qualification_status,
        "firstTouchQualifier": first_touch,
    }


def tally_field_by_date_field(contacts, value_field_id, date_field_id, start, end):
    # Like tally_field, but the date range is judged by a custom DATE field
    # (e.g. "Last Outbound Call") instead of the contact's dateAdded.
    counts = {}
    unset = 0
    total_in_range = 0
    for c in contacts:
        cf = c.get("customFields")
        d = utc_date_only(parse_date(custom_field_value(cf, date_field_id)))
        if not in_range(d, start, end):
            continue
        total_in_range += 1
        value = custom_field_value(cf, value_field_id)
        if value is None or value == "" or value == []:
            unset += 1
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            counts[v] = counts.get(v, 0) + 1
    return {"counts": counts, "unset": unset, "totalInRange": total_in_range}


def handle_cold_outbound(start, end):
    contacts = get_cold_outbound_contacts_cached()
    caller_counts = tally_field_by_date_field(
        contacts, COLD_OUTBOUND_LAST_CALLER_FIELD_ID, COLD_OUTBOUND_LAST_CALL_DATE_FIELD_ID, start, end
    )
    for name in COLD_OUTBOUND_CALLERS:
        caller_counts["counts"].setdefault(name, 0)
    disposition_counts = tally_field_by_date_field(
        contacts, COLD_OUTBOUND_DISPOSITION_FIELD_ID, COLD_OUTBOUND_DISPOSITION_DATE_FIELD_ID, start, end
    )
    for name in COLD_OUTBOUND_DISPOSITIONS:
        disposition_counts["counts"].setdefault(name, 0)
    return {"callerCounts": caller_counts, "dispositionCounts": disposition_counts}


# --- Chart drill-down ---------------------------------------------------------
# Clicking a bar on any chart lists the leads behind it, each linking to the
# contact in HighLevel (Mika, 2026-10-27). Every branch mirrors the exact rule the
# chart's number uses, so the list length matches the bar.

def _contact_name(c):
    name = " ".join(filter(None, [(c or {}).get("firstName"), (c or {}).get("lastName")])).strip()
    return name or (c or {}).get("contactName") or (c or {}).get("phone") or (c or {}).get("email") or "Unnamed"


def _lead_row(contact_id, location_id, name, detail=None, date=None):
    return {"name": name or "Unnamed", "contactId": contact_id, "locationId": location_id if contact_id else None,
            "detail": detail, "date": date}


def _channel_location(channel):
    if channel == "Warm Outbound":
        return WARM_OUTBOUND_LOCATION_ID
    if channel == "Cold Outbound":
        return COLD_OUTBOUND_LOCATION_ID
    return LOCATION_ID


def _channel_contacts(channel):
    if channel == "Warm Outbound":
        return warm_outbound_contacts.contacts()
    if channel == "Cold Outbound":
        return cold_outbound_contacts.contacts()
    if channel == "Commercial":
        return get_commercial_contacts()
    return get_all_contacts_cached()


def _added(c):
    return local_date(parse_date(c.get("dateAdded")))


def _overview_qualified(channel, start, end, rep):
    # Same rule as handle_sales_overview / handle_commercial.
    if channel in SUB_ACCOUNT_CHANNELS:
        cfg = SUB_ACCOUNT_CHANNELS[channel]
        contacts, qfield, rfield = cfg["store"]().contacts(), cfg["qualification_field"], cfg["rep_field"]
    elif channel == "Commercial":
        contacts, qfield, rfield = get_commercial_contacts(), QUALIFICATION_STATUS_FIELD_ID, FIRST_TOUCH_QUALIFIER_FIELD_ID
    else:
        contacts = inbound_source_leads(start)
        qfield, rfield = QUALIFICATION_STATUS_FIELD_ID, FIRST_TOUCH_QUALIFIER_FIELD_ID
    loc = _channel_location(channel)
    rows = []
    for c in contacts:
        added = _added(c)
        if not (start <= (added or date_min) <= end):
            continue
        if custom_field_value(c.get("customFields"), qfield) != "Qualified":
            continue
        if rep not in {overview_rep(r) for r in _rep_list(custom_field_value(c.get("customFields"), rfield))}:
            continue
        rows.append(_lead_row(c.get("id"), loc, _contact_name(c), "Qualified", added.isoformat() if added else None))
    return rows


def _overview_sales(channel, start, end, match):
    snapshot = load_revenue_snapshot()
    sales = with_live_channels(snapshot)["sales"] if snapshot else []
    loc = _channel_location(channel)
    inbound_ids = {c.get("id") for c in inbound_source_leads()} if channel == "Inbound" else None
    rows = []
    for sale in sales:
        if sale.get("channel") != channel or not (start.isoformat() <= sale["sold"] <= end.isoformat()) or not match(sale):
            continue
        if inbound_ids is not None and sale.get("leadContactId") not in inbound_ids:
            continue
        rows.append(_lead_row(sale.get("leadContactId"), loc, sale.get("customer"),
                              f"Sold {currency_text(sale.get('contractValue'))} · {sale.get('plan') or ''} · by {sale.get('rep') or ''}".strip(" ·"),
                              sale["sold"]))
    return rows


def currency_text(v):
    try:
        return "${:,.0f}".format(float(v or 0))
    except (TypeError, ValueError):
        return "$0"


def _calls_by_label(location_id, api_key, start, end, label, outbound_only=False):
    users = location_user_names(location_id, api_key)
    per_contact = {}
    for m in call_store(location_id, api_key).calls(start, end):
        if outbound_only and (m.get("direction") != "outbound" or not is_rep_call(m, users)):
            continue
        rep = call_rep_label(m, users)
        if rep != label:
            continue
        per_contact[m.get("contactId")] = per_contact.get(m.get("contactId"), 0) + 1
    by_id = {c.get("id"): c for c in (
        get_all_contacts_cached() if location_id == LOCATION_ID
        else warm_outbound_contacts.contacts() if location_id == WARM_OUTBOUND_LOCATION_ID
        else cold_outbound_contacts.contacts())}
    rows = [_lead_row(cid, location_id, _contact_name(by_id.get(cid)) if cid in by_id else "Contact not in list",
                      f"{n} call{'s' if n != 1 else ''}") for cid, n in per_contact.items() if cid]
    rows.sort(key=lambda r: -int(r["detail"].split()[0]))
    return rows


def handle_chart_leads(chart, key, start, end, channel=None):
    rep = next((r for r in REPS if r["name"] == key), None)
    if chart in ("team-deals", "inbound-deals"):
        if not rep:
            raise ValueError(f"Unknown rep: {key}")
        allowed = all_inbound_contact_ids() if chart == "inbound-deals" else None
        by_id = {c.get("id"): c for c in get_all_contacts_cached()}
        rows = []
        for d in rep_deals(rep, start, end, allowed, with_contact=True):
            rows.append(_lead_row(d["contactId"], LOCATION_ID, _contact_name(by_id[d["contactId"]]) if d["contactId"] in by_id else d["name"],
                                  f"Closed {currency_text(d['value'])}", d["closedDate"]))
        return {"title": f"{key} — Sold Contracts", "rows": rows}
    if chart in ("team-qualified", "inbound-qualified", "team-close", "inbound-close"):
        if not rep:
            raise ValueError(f"Unknown rep: {key}")
        contacts = get_contacts_for(start)
        if chart.startswith("inbound"):
            contacts = [c for c in contacts if is_inbound_source(c.get("source"))]
        rows = []
        for c in contacts:
            added = _added(c)
            if not in_range(added, start, end):
                continue
            cfs = c.get("customFields")
            qualified = custom_field_value(cfs, QUALIFICATION_STATUS_FIELD_ID) == "Qualified"
            ftq = key in _rep_list(custom_field_value(cfs, FIRST_TOUCH_QUALIFIER_FIELD_ID))
            if chart.endswith("qualified") or chart == "inbound-close":
                if qualified and ftq:
                    rows.append(_lead_row(c.get("id"), LOCATION_ID, _contact_name(c), "Qualified", added.isoformat()))
            else:  # team-close: closed leads (Sales Rep, Closed/Won) out of the rep's first-touch leads
                closed = custom_field_value(cfs, LEAD_STAGE_FIELD_ID) == "Closed/Won" and key in _rep_list(custom_field_value(cfs, SALES_REP_FIELD_ID))
                if ftq or closed:
                    rows.append(_lead_row(c.get("id"), LOCATION_ID, _contact_name(c),
                                          "Closed/Won" if closed else "First touch, not closed", added.isoformat()))
        if chart == "inbound-close":
            deals = handle_chart_leads("inbound-deals", key, start, end)["rows"]
            rows = deals + rows
        title = {"team-qualified": "Qualified Leads", "inbound-qualified": "Qualified Leads",
                 "team-close": "Leads & Closed", "inbound-close": "Sold & Qualified"}[chart]
        rows.sort(key=lambda r: r["date"] or "", reverse=True)
        rows.sort(key=lambda r: not (r["detail"] or "").startswith("Closed"))  # closed/sold first, newest first within each
        return {"title": f"{key} — {title}", "rows": rows}
    if chart in ("team-sources", "team-closed-sources"):
        rows = []
        for c in get_contacts_for(start):
            added = _added(c)
            if not in_range(added, start, end) or not is_inbound_source(c.get("source")):
                continue
            if chart == "team-closed-sources" and custom_field_value(c.get("customFields"), LEAD_STAGE_FIELD_ID) != "Closed/Won":
                continue
            rows.append(_lead_row(c.get("id"), LOCATION_ID, _contact_name(c), c.get("source"), added.isoformat()))
        return {"title": ("Closed Leads" if chart == "team-closed-sources" else "Leads") + f" — {key}", "rows": rows}
    if chart in ("tc-sold", "tc-qualified", "tc-close", "tc-sources", "tc-closed-sources"):
        rows = []
        if chart == "tc-sources":
            rows = [_lead_row(c.get("id"), loc, _contact_name(c), ch, (local_date(parse_date(c.get("dateAdded"))) or date_min).isoformat())
                    for ch, loc, c, platform in team_lead_contacts(start, end) if platform == key]
            return {"title": f"{key} — Leads (Inbound + Warm Outbound)", "rows": rows}
        for ch in TEAM_CHANNELS:
            if chart in ("tc-sources", "tc-closed-sources") and key != ch:
                continue
            if chart in ("tc-sold", "tc-close"):
                rows += _overview_sales(ch, start, end, lambda sale: sale["group"] == key)
            if chart in ("tc-qualified", "tc-close"):
                rows += _overview_qualified(ch, start, end, key)
            if chart == "tc-closed-sources":
                rows += _overview_sales(ch, start, end, lambda sale: True)
        titles = {"tc-sold": "Sold Contracts", "tc-qualified": "Qualified Leads", "tc-close": "Sold & Qualified",
                  "tc-sources": "Leads", "tc-closed-sources": "Sold Contracts"}
        return {"title": f"{key} — {titles[chart]} (Inbound + Warm + Cold)", "rows": rows}
    if chart == "ov-sold-channel":
        return {"title": f"{key} — Sold Contracts", "rows": _overview_sales(key, start, end, lambda sale: True)}
    if chart == "ov-sold":
        return {"title": f"{channel} — {key} — Sold Contracts", "rows": _overview_sales(channel, start, end, lambda sale: sale["group"] == key)}
    if chart == "ov-qualified":
        return {"title": f"{channel} — {key} — Qualified Leads", "rows": _overview_qualified(channel, start, end, key)}
    if chart == "ov-close":
        return {"title": f"{channel} — {key} — Sold & Qualified",
                "rows": _overview_sales(channel, start, end, lambda sale: sale["group"] == key) + _overview_qualified(channel, start, end, key)}
    if chart in ("ov-types", "ov-typesales"):
        contacts = warm_outbound_contacts.contacts()
        type_of = {c.get("id"): warm_lead_type(c) for c in contacts}
        if chart == "ov-typesales":
            return {"title": f"Warm Outbound — {key} — Sales",
                    "rows": _overview_sales("Warm Outbound", start, end, lambda sale: type_of.get(sale.get("leadContactId")) == key)}
        rows = [_lead_row(c.get("id"), WARM_OUTBOUND_LOCATION_ID, _contact_name(c), key, _added(c).isoformat())
                for c in contacts if start <= (_added(c) or date_min) <= end and type_of.get(c.get("id")) == key]
        return {"title": f"Warm Outbound — {key} — Leads Received", "rows": rows}
    if chart == "ov-calls":
        return {"title": f"Cold Outbound — {key} — Contacts Called",
                "rows": _calls_by_label(COLD_OUTBOUND_LOCATION_ID, COLD_OUTBOUND_API_KEY, start, end, key, outbound_only=True)}
    if chart == "cc-calls":
        acct = next((a for a in call_count_accounts() if a[0] == channel), None)
        if not acct:
            raise ValueError(f"Unknown account: {channel}")
        return {"title": f"{channel} — {key} — Contacts", "rows": _calls_by_label(acct[1], acct[2], start, end, key)}
    if chart in ("co-callers", "co-dispositions"):
        value_field = COLD_OUTBOUND_LAST_CALLER_FIELD_ID if chart == "co-callers" else COLD_OUTBOUND_DISPOSITION_FIELD_ID
        date_field = COLD_OUTBOUND_LAST_CALL_DATE_FIELD_ID if chart == "co-callers" else COLD_OUTBOUND_DISPOSITION_DATE_FIELD_ID
        rows = []
        for c in get_cold_outbound_contacts_cached():
            cf = c.get("customFields")
            d = utc_date_only(parse_date(custom_field_value(cf, date_field)))
            if not in_range(d, start, end):
                continue
            if key not in _rep_list(custom_field_value(cf, value_field)):
                continue
            rows.append(_lead_row(c.get("id"), COLD_OUTBOUND_LOCATION_ID, _contact_name(c), key, d.isoformat()))
        return {"title": f"Cold Outbound — {key}", "rows": rows}
    raise ValueError(f"Unknown chart: {chart}")


CACHE_TTL_SECONDS = 600
_cache = {}


def cached(key_parts, compute_fn):
    key = tuple(key_parts)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    result = compute_fn()
    _cache[key] = (now, result)
    return result


def _refresh_opportunities():
    with ThreadPoolExecutor(max_workers=len(REPS)) as pool:
        opps_by_rep = list(pool.map(lambda rep: fetch_all_opportunities(rep["pipelineId"], rep["closedWonStageId"]), REPS))
    now = time.monotonic()
    for rep, opps in zip(REPS, opps_by_rep):
        _opps_cache[(rep["pipelineId"], rep["closedWonStageId"])] = {"fetchedAt": now, "opps": opps}


def refresh_data_caches():
    # Best-effort and parallel: one fetch failing (e.g. a transient HighLevel
    # timeout) shouldn't stop the others from refreshing.
    jobs = [main_contacts.sync, _refresh_opportunities]
    if COLD_OUTBOUND_API_KEY and COLD_OUTBOUND_LOCATION_ID:
        jobs.append(cold_outbound_contacts.sync)
    if WARM_OUTBOUND_API_KEY and WARM_OUTBOUND_LOCATION_ID:
        jobs.append(warm_outbound_contacts.sync)
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        for future in [pool.submit(fn) for fn in jobs]:
            try:
                future.result()
            except Exception:
                pass
    _cache.clear()


# Refreshes just under the 10-minute cache TTL so data is always replaced
# *before* it goes stale — a real user request should never be the one
# paying for a live HighLevel fetch.
BACKGROUND_REFRESH_SECONDS = 540


def warm_cache():
    def _loop():
        while True:
            refresh_data_caches()
            time.sleep(BACKGROUND_REFRESH_SECONDS)

    threading.Thread(target=_loop, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def checker_authorized(self):
        # The routine sends the shared secret as a Bearer token.
        if TRANSCRIPT_CHECKER_TOKEN and self.headers.get("Authorization", "") == f"Bearer {TRANSCRIPT_CHECKER_TOKEN}":
            return True
        self.send_json({"error": "unauthorized"}, 401)
        return False

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/transcript-checker/verdicts":
            self.send_json({"error": "not found"}, 404)
            return
        if not self.checker_authorized():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            verdicts = json.loads(self.rfile.read(length) or b"[]")
        except ValueError:
            self.send_json({"error": "body must be a JSON list of verdicts"}, 400)
            return
        try:
            self.send_json(handle_transcript_checker_verdicts(verdicts))
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/performance-compare":
            try:
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
                prev_start = datetime.fromisoformat(qs["prevStart"][0]).date()
                prev_end = datetime.fromisoformat(qs["prevEnd"][0]).date()
            except (KeyError, ValueError):
                self.send_json({"error": "start, end, prevStart, prevEnd query params (YYYY-MM-DD) are required"}, 400)
                return
            try:
                self.send_json(
                    cached(
                        ("performance-compare", start, end, prev_start, prev_end),
                        lambda: handle_performance_compare(start, end, prev_start, prev_end),
                    )
                )
            except ConfigError as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/power-dialer":
            try:
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
            except (KeyError, ValueError):
                self.send_json({"error": "start and end query params (YYYY-MM-DD) are required"}, 400)
                return
            try:
                self.send_json(cached(("power-dialer", start, end), lambda: handle_power_dialer(start, end)))
            except ConfigError as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path in ("/api/sales", "/api/qualification", "/api/performance", "/api/inbound-calls"):
            try:
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
            except (KeyError, ValueError):
                self.send_json({"error": "start and end query params (YYYY-MM-DD) are required"}, 400)
                return
            try:
                if parsed.path == "/api/sales":
                    self.send_json(cached(("sales", start, end), lambda: handle_sales(start, end)))
                elif parsed.path == "/api/qualification":
                    self.send_json(cached(("qualification", start, end), lambda: handle_qualification(start, end)))
                elif parsed.path == "/api/inbound-calls":
                    self.send_json(cached(("inbound-calls", start, end), lambda: handle_inbound_calls(start, end)))
                else:
                    self.send_json(cached(("performance", start, end), lambda: handle_performance(start, end)))
            except ConfigError as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/call-count":
            try:
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
            except (KeyError, ValueError):
                self.send_json({"error": "start and end query params (YYYY-MM-DD) are required"}, 400)
                return
            try:
                self.send_json(cached(("call-count", start, end), lambda: handle_call_count(start, end)))
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path in ("/api/team-combined", "/api/team-combined-compare"):
            try:
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
                if parsed.path == "/api/team-combined":
                    self.send_json(handle_team_combined(start, end))
                else:
                    ps = datetime.fromisoformat(qs["prevStart"][0]).date()
                    pe = datetime.fromisoformat(qs["prevEnd"][0]).date()
                    self.send_json({"current": handle_team_combined(start, end), "previous": handle_team_combined(ps, pe)})
            except (KeyError, ValueError):
                self.send_json({"error": "start and end query params (YYYY-MM-DD) are required"}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/sales-overview":
            try:
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
            except (KeyError, ValueError):
                self.send_json({"error": "start and end query params (YYYY-MM-DD) are required"}, 400)
                return
            try:
                self.send_json(cached(("sales-overview", start, end), lambda: handle_sales_overview(start, end)))
            except ConfigError as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/commercial":
            try:
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
            except (KeyError, ValueError):
                self.send_json({"error": "start and end query params (YYYY-MM-DD) are required"}, 400)
                return
            try:
                self.send_json(cached(("commercial", start, end), lambda: handle_commercial(start, end)))
            except ConfigError as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/transcript-checker":
            self.send_json(_checker_status)
            return

        if parsed.path == "/api/transcript-checker/unaudited-calls":
            if not self.checker_authorized():
                return
            try:
                self.send_json(handle_transcript_checker_unaudited_calls())
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/transcript-checker/pending":
            if not self.checker_authorized():
                return
            try:
                self.send_json(handle_transcript_checker_pending())
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/inbound-call-log":
            try:
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
            except (KeyError, ValueError):
                self.send_json({"error": "start and end query params (YYYY-MM-DD) are required"}, 400)
                return
            try:
                self.send_json(cached(("inbound-call-log", start, end), lambda: handle_inbound_call_log(start, end)))
            except ConfigError as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/cold-outbound":
            try:
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
            except (KeyError, ValueError):
                self.send_json({"error": "start and end query params (YYYY-MM-DD) are required"}, 400)
                return
            try:
                self.send_json(cached(("cold-outbound", start, end), lambda: handle_cold_outbound(start, end)))
            except ConfigError as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/chart-leads":
            try:
                chart, key = qs["chart"][0], qs["key"][0]
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
                channel = (qs.get("channel") or [None])[0]
            except (KeyError, ValueError):
                self.send_json({"error": "chart, key, start and end query params are required"}, 400)
                return
            try:
                self.send_json(cached(("chart-leads", chart, key, channel, start, end), lambda: handle_chart_leads(chart, key, start, end, channel)))
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/rep-deals":
            try:
                rep_name = qs["rep"][0]
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
            except (KeyError, ValueError):
                self.send_json({"error": "rep, start and end query params are required"}, 400)
                return
            try:
                self.send_json(cached(("rep-deals", rep_name, start, end), lambda: handle_rep_deals(rep_name, start, end)))
            except ConfigError as e:
                self.send_json({"error": str(e)}, 400)
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path in ("/api/inbound-rep-deals", "/api/inbound-qualified-leads"):
            try:
                rep_name = qs["rep"][0]
                start = datetime.fromisoformat(qs["start"][0]).date()
                end = datetime.fromisoformat(qs["end"][0]).date()
            except (KeyError, ValueError):
                self.send_json({"error": "rep, start and end query params are required"}, 400)
                return
            try:
                if parsed.path == "/api/inbound-rep-deals":
                    self.send_json(
                        cached(("inbound-rep-deals", rep_name, start, end), lambda: handle_inbound_rep_deals(rep_name, start, end))
                    )
                else:
                    self.send_json(
                        cached(
                            ("inbound-qualified-leads", rep_name, start, end),
                            lambda: handle_inbound_qualified_leads(rep_name, start, end),
                        )
                    )
            except ConfigError as e:
                self.send_json({"error": str(e)}, 400)
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/revenue-snapshot":
            try:
                snapshot = load_revenue_snapshot()
            except Exception as e:
                self.send_json({"error": f"Could not load revenue snapshot: {e}"}, 500)
                return
            if snapshot is None:
                self.send_json({"error": "No revenue snapshot yet."}, 404)
                return
            live = with_live_channels(snapshot)
            # HighLevel account per channel, so customer names can link to their lead.
            live["locations"] = {"Inbound": LOCATION_ID, "Commercial": LOCATION_ID,
                                 "Warm Outbound": WARM_OUTBOUND_LOCATION_ID, "Cold Outbound": COLD_OUTBOUND_LOCATION_ID}
            self.send_json(live)
            return

        if parsed.path == "/api/refresh":
            refresh_data_caches()
            self.send_json({"status": "refreshed"})
            return

        if parsed.path == "/" or parsed.path == "/index.html":
            self.send_file(os.path.join(STATIC_DIR, "index.html"), "text/html")
            return
        if parsed.path == "/app.js":
            self.send_file(os.path.join(STATIC_DIR, "app.js"), "application/javascript")
            return
        if parsed.path == "/style.css":
            self.send_file(os.path.join(STATIC_DIR, "style.css"), "text/css")
            return
        if parsed.path == "/evo-logo.png":
            self.send_file(os.path.join(STATIC_DIR, "evo-logo.png"), "image/png")
            return
        if parsed.path in ("/favicon.png", "/favicon.ico"):
            self.send_file(os.path.join(STATIC_DIR, "favicon.png"), "image/png")
            return

        self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"evo-dashboard running on {host}:{port}")
    if not API_KEY:
        print("WARNING: HIGHLEVEL_API_KEY_INBOUND is not set — API calls will fail until it's added as an env var.")
    else:
        warm_cache()
        prefetch_calls()
    if not TRANSCRIPT_CHECKER_TOKEN:
        print("WARNING: TRANSCRIPT_CHECKER_TOKEN is not set — the transcript checker endpoints are disabled.")
    if not WARM_OUTBOUND_API_KEY or not WARM_OUTBOUND_LOCATION_ID:
        print("WARNING: HIGHLEVEL_API_KEY_WARM_OUTBOUND / HIGHLEVEL_LOCATION_ID_WARM_OUTBOUND are not set — Warm Outbound sales will show as not matched.")
    if not COLD_OUTBOUND_API_KEY or not COLD_OUTBOUND_LOCATION_ID:
        print("WARNING: HIGHLEVEL_API_KEY_COLD_OUTBOUND / HIGHLEVEL_LOCATION_ID_COLD_OUTBOUND are not set — the Cold Outbound tab will fail until they're added as env vars.")
    server.serve_forever()
