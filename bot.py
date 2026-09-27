from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


app = FastAPI(title="Vera deterministic engagement bot", docs_url=None, redoc_url=None)
STARTED_AT = time.time()
LOCK = threading.RLock()
CONTEXTS: dict[tuple[str, str], dict[str, Any]] = {}
CONVERSATIONS: dict[str, dict[str, Any]] = {}
DELIVERED_KEYS: set[str] = set()
OPTED_OUT_MERCHANTS: set[str] = set()
OPTED_OUT_CUSTOMERS: set[str] = set()
LAST_SENT: dict[str, datetime] = {}
TICK_CACHE: dict[str, dict[str, Any]] = {}
REPLY_CACHE: dict[str, dict[str, Any]] = {}
AUTO_REPLY_STREAK: dict[str, tuple[str, int]] = {}


CATEGORY_STYLE = {
    "dentists": {
        "register": "clinical and collegial",
        "metric": "calls",
        "customer_open": "Hi {customer}, {merchant} here.",
        "guardrail": "Avoid guarantees and unverified treatment claims.",
    },
    "salons": {
        "register": "warm and appointment-led",
        "metric": "calls",
        "customer_open": "Hi {customer}, {merchant} team here.",
        "guardrail": "Use service and occasion language; do not promise results.",
    },
    "restaurants": {
        "register": "busy operator-to-operator",
        "metric": "directions",
        "customer_open": "Hi {customer}, {merchant} here.",
        "guardrail": "Prioritize covers, ordering windows, locality, and existing menu offers.",
    },
    "gyms": {
        "register": "disciplined, supportive coaching",
        "metric": "calls",
        "customer_open": "Hi {customer}, {merchant} team here.",
        "guardrail": "Use no-shame retention language; never promise body or health outcomes.",
    },
    "pharmacies": {
        "register": "precise and safety-first",
        "metric": "calls",
        "customer_open": "Namaste, {merchant} here.",
        "guardrail": "Never advise on dosage, treatment, substitution, or safety; refer clinical questions to a pharmacist/prescriber.",
    },
}

KIND_PRIORITY = {
    "supply_alert": 100,
    "regulation_change": 96,
    "compliance_alert": 96,
    "active_planning_intent": 92,
    "recall_due": 88,
    "chronic_refill_due": 88,
    "appointment_tomorrow": 88,
    "trial_followup": 82,
    "wedding_package_followup": 82,
    "customer_lapsed_hard": 78,
    "customer_lapsed_soft": 72,
    "perf_dip": 70,
    "seasonal_perf_dip": 68,
    "renewal_due": 66,
    "review_theme_emerged": 62,
    "gbp_unverified": 60,
    "competitor_opened": 58,
    "perf_spike": 54,
    "research_digest": 52,
    "research_digest_release": 52,
    "cde_opportunity": 48,
    "category_seasonal": 46,
    "festival_upcoming": 44,
    "ipl_match_today": 44,
    "winback_eligible": 42,
    "milestone_reached": 38,
    "dormant_with_vera": 30,
    "curious_ask_due": 24,
}

CUSTOMER_CONSENT_SCOPES = {
    "recall_due": {"recall_reminders", "appointment_reminders"},
    "appointment_tomorrow": {"appointment_reminders"},
    "chronic_refill_due": {"refill_reminders"},
    "trial_followup": {"appointment_reminders", "kids_program_updates", "program_updates"},
    "wedding_package_followup": {"bridal_package_followup", "appointment_reminders"},
    "customer_lapsed_soft": {"promotional_offers", "recall_reminders"},
    "customer_lapsed_hard": {"winback_offers", "renewal_reminders", "promotional_offers"},
}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")[:80] or "unknown"


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _display_datetime(value: Any) -> str:
    parsed = _parse_datetime(value)
    if not parsed:
        return _text(value)
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return parsed.strftime("%d %b %Y").lstrip("0")
    hour = parsed.hour % 12 or 12
    suffix = "am" if parsed.hour < 12 else "pm"
    return f"{parsed.day} {parsed.strftime('%b %Y')}, {hour}:{parsed.minute:02d}{suffix}"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value: Any, signed: bool = True) -> str:
    number = _number(value)
    if number is None:
        return ""
    points = number * 100 if abs(number) <= 1 else number
    return f"{points:+.0f}%" if signed else f"{points:.0f}%"


def _owner(merchant: dict[str, Any], category_slug: str) -> str:
    identity = _dict(merchant.get("identity"))
    name = _text(identity.get("owner_first_name"))
    if category_slug == "dentists":
        if name and not name.lower().startswith("dr"):
            return f"Dr. {name}"
        return name or _text(identity.get("name")) or "Doctor"
    return name or _text(identity.get("name")) or "there"


def _merchant_name(merchant: dict[str, Any]) -> str:
    return _text(_dict(merchant.get("identity")).get("name")) or "your business"


def _active_offers(merchant: dict[str, Any]) -> list[str]:
    return [
        _text(_dict(offer).get("title"))
        for offer in _list(merchant.get("offers"))
        if _dict(offer).get("status") == "active" and _text(_dict(offer).get("title"))
    ]


def _find_digest_item(category: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any]:
    payload = _dict(trigger.get("payload"))
    item_id = _text(payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id"))
    items = [_dict(item) for item in _list(category.get("digest"))]
    if item_id:
        for item in items:
            if item.get("id") == item_id:
                return item
    kind = _text(trigger.get("kind"))
    wanted = {
        "research_digest": {"research", "trend", "tech", "seasonal", "compliance", "supply", "alert"},
        "research_digest_release": {"research", "trend", "tech", "seasonal", "compliance", "supply", "alert"},
        "regulation_change": {"compliance"},
        "cde_opportunity": {"cde"},
        "supply_alert": {"alert", "supply"},
    }.get(kind, set())
    for item in items:
        if _text(item.get("kind")).lower() in wanted:
            return item
    return {}


def _evidence_delta(merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str, float]:
    payload = _dict(trigger.get("payload"))
    performance = _dict(merchant.get("performance"))
    delta = _dict(performance.get("delta_7d"))
    metric = _text(payload.get("metric")) or "views"
    value = payload.get("delta_pct")
    if _number(value) is None:
        value = delta.get(f"{metric}_pct")
    formatted = _pct(value)
    metric_value = performance.get(metric)
    if formatted:
        evidence = f"{metric.replace('_', ' ')} {formatted} over {payload.get('window', '7d')}"
        magnitude = abs(_number(value) or 0.0)
        return metric, evidence, magnitude
    if _number(metric_value) is not None:
        return metric, f"{int(float(metric_value)):,} {metric.replace('_', ' ')} in {performance.get('window_days', 30)} days", 0.0
    return metric, "", 0.0


def _topic_words(trigger: dict[str, Any]) -> str:
    payload = _dict(trigger.get("payload"))
    topic = _text(payload.get("intent_topic") or payload.get("topic"))
    if topic:
        return topic.replace("_", " ")
    return "the plan we discussed"


def _is_customer_eligible(customer: dict[str, Any], trigger: dict[str, Any]) -> bool:
    consent = _dict(customer.get("consent"))
    preferences = _dict(customer.get("preferences"))
    opted_in = preferences.get("reminder_opt_in")
    if opted_in is False:
        return False
    scopes = set(_list(consent.get("scope")))
    required = CUSTOMER_CONSENT_SCOPES.get(_text(trigger.get("kind")), set())
    return bool(required and scopes.intersection(required) and consent.get("opted_in_at"))


def _customer_message(category_slug: str, merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any]) -> tuple[str, str, str] | None:
    kind = _text(trigger.get("kind"))
    payload = _dict(trigger.get("payload"))
    identity = _dict(customer.get("identity"))
    relationship = _dict(customer.get("relationship"))
    preferences = _dict(customer.get("preferences"))
    customer_name = _text(identity.get("name"))
    language = _text(identity.get("language_pref")).lower()
    hindi_mix = "hi" in language
    merchant_name = _merchant_name(merchant)
    owner = _owner(merchant, category_slug)
    slots = [_dict(slot) for slot in _list(payload.get("available_slots") or payload.get("next_session_options"))]
    slots = [slot for slot in slots if _text(slot.get("label"))]
    offers = _active_offers(merchant)
    offer = offers[0] if offers else ""

    if kind in {"recall_due", "appointment_tomorrow"}:
        due = _text(payload.get("due_date") or payload.get("appointment_date"))
        due_text = f" (due {due})" if due else ""
        service = _text(payload.get("service_due") or payload.get("service"))
        if not service:
            service = "appointment" if kind == "appointment_tomorrow" else "follow-up"
        last_visit = _text(relationship.get("last_visit") or payload.get("last_service_date"))
        months = f"Your last visit was {last_visit}; " if last_visit else ""
        matching_offer = next((title for title in offers if category_slug == "dentists" and "clean" in service.lower() and "clean" in title.lower()), "")
        offer_text = f" The listed offer is {matching_offer}." if matching_offer else ""
        if category_slug == "dentists":
            lead = f"Hi {customer_name}, {merchant_name} here. "
            detail = f"{months}your {service.replace('_', ' ')} reminder is due{due_text}."
        elif category_slug == "salons":
            lead = f"Hi {customer_name}, {merchant_name} team here. "
            detail = f"A quick appointment reminder: {service.replace('_', ' ')}{due_text}."
        elif category_slug == "restaurants":
            lead = f"Hi {customer_name}, {merchant_name} here. "
            detail = f"A quick reminder about your {service.replace('_', ' ')}{due_text}."
        elif category_slug == "gyms":
            lead = f"Hi {customer_name}, {merchant_name} team here. "
            detail = f"A quick, no-pressure reminder about {service.replace('_', ' ')}{due_text}."
        else:
            lead = f"Namaste {customer_name}, {merchant_name} here. "
            detail = f"A reminder about your refill request{due_text}. Please confirm the items with our pharmacist; we cannot infer or change your prescription."
        if kind == "appointment_tomorrow" and not slots:
            change_prompt = "Agar time confirm ya change karna ho, reply karein." if hindi_mix else "If you need to confirm the time or make a change, reply here."
            body = f"{lead}Your appointment is tomorrow. {change_prompt}"
            return body, "open_ended", "Appointment reminder uses only the trigger's stated day and does not invent a time or service."
        if slots:
            slot_text = " or ".join(_text(slot.get("label")) for slot in slots[:2])
            availability = f"Apke liye {slot_text}" if hindi_mix else f"Available: {slot_text}"
            body = f"{lead}{detail}{offer_text} {availability}. Which works for you?"
            return body, "multi_choice_slot", f"Customer appointment reminder; offered only supplied slots {slot_text}."
        if category_slug == "pharmacies":
            body = f"{lead}{detail} Reply here if you would like the pharmacist to confirm availability."
            return body, "open_ended", "Pharmacy reminder stays within the supplied refill context and routes confirmation to a pharmacist."
        body = f"{lead}{detail} Would you like us to help arrange a time?"
        return body, "binary_yes_no", f"Appointment reminder grounded in the supplied customer and trigger context for {owner}."

    if kind == "chronic_refill_due":
        molecules = [_text(item) for item in _list(payload.get("molecule_list")) if _text(item)]
        runout = _parse_datetime(payload.get("stock_runs_out_iso"))
        due = runout.date().isoformat() if runout else _text(payload.get("due_date"))
        list_text = ", ".join(molecules)
        details = f"The refill reminder you requested lists {list_text}" if list_text else "Your refill reminder is due"
        if due:
            details += f" by {due}"
        address_saved = payload.get("delivery_address_saved") is True or preferences.get("delivery_address") == "saved"
        delivery = " A saved delivery address is on file." if address_saved else ""
        via_proxy = "via_son" in _text(preferences.get("channel"))
        greeting = f"Namaste, {merchant_name} here." if via_proxy else f"Namaste {customer_name}, {merchant_name} here."
        refill_detail = f"The saved refill reminder lists {list_text}" if via_proxy and list_text else details
        if via_proxy and due:
            refill_detail += f" by {due}"
        body = f"{greeting} {refill_detail}.{delivery} Please confirm the medicines and prescription with our pharmacist before dispatch. Reply if you'd like us to check availability."
        return body, "open_ended", "Refill reminder uses only supplied medicine names/date and explicitly defers prescription checks to the pharmacist."

    if kind in {"customer_lapsed_soft", "customer_lapsed_hard"}:
        if category_slug == "gyms":
            lead = f"Hi {customer_name}, {merchant_name} here. It has been a while since your last visit; no pressure, routines change."
            if offer:
                lead += f" The current offer is {offer}."
            return f"{lead} Would a low-pressure return visit be useful?", "binary_yes_no", "Gym win-back uses non-judgmental language and only an active listed offer."
        if category_slug == "salons":
            detail = f"Your last visit was {relationship.get('last_visit')}. " if relationship.get("last_visit") else ""
            if offer:
                detail += f"The current offer is {offer}. "
            return f"Hi {customer_name}, {merchant_name} team here. {detail}Would you like to see an appointment option?", "binary_yes_no", "Salon re-engagement offers a simple appointment step without invented availability."
        if category_slug == "restaurants":
            detail = f"Your past visits included {', '.join(_list(relationship.get('services_received'))[-2:])}. " if relationship.get("services_received") else ""
            if offer:
                detail += f"Our current offer is {offer}. "
            return f"Hi {customer_name}, {merchant_name} here. {detail}Would you like the current menu or offer details?", "binary_yes_no", "Restaurant win-back is grounded in actual visit history and active offers."
        if category_slug == "dentists":
            last_visit = _text(relationship.get("last_visit"))
            detail = f"Your last visit here was {last_visit}. " if last_visit else ""
            return f"Hi {customer_name}, {merchant_name} here. {detail}If you need to arrange a follow-up, would you like us to check appointment options?", "binary_yes_no", "Dental follow-up avoids diagnosis and treatment claims."
        if category_slug == "pharmacies":
            last_visit = _text(relationship.get("last_visit"))
            detail = f"We last saw you on {last_visit}. " if last_visit else ""
            offer_text = f"Our current service is {offer}. " if offer else ""
            return f"Namaste {customer_name}, {merchant_name} here. {detail}{offer_text}If you need a refill, our pharmacist can confirm availability and prescription details. Would that help?", "binary_yes_no", "Pharmacy re-engagement avoids inferring a diagnosis or recommending a medicine."
        return None

    if kind in {"trial_followup", "wedding_package_followup"}:
        if kind == "wedding_package_followup" and category_slug == "salons":
            wedding_date = _text(payload.get("wedding_date") or preferences.get("wedding_date"))
            date_text = f" for your {wedding_date} wedding" if wedding_date else ""
            return f"Hi {customer_name}, {merchant_name} team here. Following up on your bridal trial{date_text}. Would you like to review the next appointment step?", "binary_yes_no", "Bridal follow-up references the recorded trial and wedding date only."
        trial_date = _text(payload.get("trial_date"))
        date_text = f" on {trial_date}" if trial_date else ""
        slot_text = f" The next supplied option is {slots[0]['label']}." if slots else ""
        return f"Hi {customer_name}, {merchant_name} team here. Checking in after your trial{date_text}.{slot_text} Would you like to discuss the next session?", "binary_yes_no", "Trial follow-up uses the supplied trial date and session option."
    return None


def _merchant_message(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str, str] | None:
    category_slug = _text(category.get("slug") or merchant.get("category_slug"))
    if category_slug not in CATEGORY_STYLE:
        return None
    kind = _text(trigger.get("kind"))
    payload = _dict(trigger.get("payload"))
    identity = _dict(merchant.get("identity"))
    owner = _owner(merchant, category_slug)
    business = _merchant_name(merchant)
    locality = _text(identity.get("locality"))
    location = f" in {locality}" if locality else ""
    offers = _active_offers(merchant)
    offer = offers[0] if offers else ""

    if kind in {"research_digest", "research_digest_release", "regulation_change", "cde_opportunity", "supply_alert"}:
        if payload.get("placeholder") is True and not any(payload.get(key) for key in ("top_item_id", "digest_item_id", "alert_id")):
            return None
        item = _find_digest_item(category, trigger)
        title = _text(item.get("title"))
        source = _text(item.get("source"))
        summary = _text(item.get("summary"))
        actionable = _text(item.get("actionable"))
        if kind == "supply_alert":
            molecule = _text(payload.get("molecule"))
            batches = [_text(value) for value in _list(payload.get("affected_batches")) if _text(value)]
            manufacturer = _text(payload.get("manufacturer"))
            if not (molecule and batches):
                return None
            batch_text = ", ".join(batches[:3])
            evidence = f"{molecule} batches {batch_text}"
            if manufacturer:
                evidence += f" ({manufacturer})"
            source_text = f"; {source}" if source else ""
            body = f"{owner}, the supplied alert flags {evidence}{source_text}. Please verify the distributor notice and batch records before contacting customers. Want me to format a batch-check checklist from these alert details?"
            return body, "open_ended", "High-priority pharmacy alert; exact batch identifiers are supplied, with customer action held until verification."
        if not title:
            return None
        fact = summary[:260].strip().rstrip(" .") if summary else title
        if fact and fact[-1] not in ".!?":
            fact += "."
        trial_n = _number(item.get("trial_n"))
        if trial_n is not None:
            fact = f"{int(trial_n):,}-participant evidence: {fact}"
        citation = f" Source: {source}." if source else ""
        if kind == "regulation_change" or _text(item.get("kind")) == "compliance":
            deadline = _text(payload.get("deadline_iso")) or _text(item.get("effective_date"))
            deadline_text = f" Effective {deadline}." if deadline else ""
            body = f"{owner}, {title}. {fact}{deadline_text}{citation} Please verify the circular against your setup; want me to format the checklist?"
            return body, "open_ended", f"Compliance trigger prioritized for this {category_slug} merchant; source is cited and implementation requires verification."
        if kind == "cde_opportunity":
            date = _display_datetime(item.get("date"))
            credits = payload.get("credits", item.get("credits"))
            fee = (_text(payload.get("fee")) or _text(item.get("actionable"))).replace("_", " ")
            detail = f" {credits} credits" if credits is not None else ""
            if date:
                detail += f" on {date}"
            if fee:
                detail += f"; {fee}"
            body = f"{owner}, {title}{detail}. {fact}{citation} Worth considering for your team; should I summarize the registration details?"
            return body, "open_ended", "Category education opportunity is grounded in the linked digest entry and its stated date/credit details."
        cohort = _dict(merchant.get("customer_aggregate"))
        relevant = ""
        if category_slug == "dentists" and _number(cohort.get("high_risk_adult_count")):
            relevant = f" It may be relevant to your {int(cohort['high_risk_adult_count'])} high-risk adult patients."
        elif category_slug == "salons" and "bridal" in title.lower():
            relevant = f" It could fit bridal bookings{location}."
        elif category_slug == "restaurants" and "ipl" in title.lower():
            relevant = " Check the match day before changing the offer."
        elif category_slug == "gyms" and "pt" in title.lower():
            relevant = " The digest identifies PT demand as the opportunity, not a guaranteed result."
        elif category_slug == "pharmacies" and "metformin" in title.lower():
            relevant = " Verify current supplier pricing before changing a shelf or recommendation."
        action_hint = f" The digest suggests: {actionable}." if actionable else ""
        body = f"{owner}, {title}. {fact}{relevant}{action_hint}{citation} Want me to draft a short, reviewable next step based on this item?"
        return body, "open_ended", f"{category_slug.title()} digest item selected and cited; merchant relevance is limited to matching supplied context."

    if kind in {"perf_dip", "seasonal_perf_dip", "perf_spike"}:
        metric, evidence, magnitude = _evidence_delta(merchant, trigger)
        if not evidence:
            return None
        delta_value = payload.get("delta_pct")
        if _number(delta_value) is None:
            delta_value = _dict(_dict(merchant.get("performance")).get("delta_7d")).get(f"{metric}_pct")
        is_dip = kind != "perf_spike" and (_number(delta_value) or 0) < 0
        if kind == "perf_dip" and _number(delta_value) is not None and _number(delta_value) >= 0:
            return None
        if kind == "perf_spike" and _number(delta_value) is not None and _number(delta_value) <= 0:
            return None
        if kind == "seasonal_perf_dip" and payload.get("is_expected_seasonal") is True:
            season = _text(payload.get("season_note")).replace("_", " ")
            detail = f" The supplied context flags this as expected seasonality{': ' + season if season else ''}; focus on retention rather than reacting with ad spend."
        elif is_dip:
            detail = " Worth checking the listing and recent customer feedback before increasing spend."
        else:
            detail = " Useful signal to identify what is bringing people in and repeat it."
        if category_slug == "dentists":
            body = f"{owner}, {business}{location}: {evidence}.{detail} Want me to check the profile signals behind it?"
        elif category_slug == "salons":
            body = f"{owner}, {business}{location}: {evidence}.{detail} Want me to shape a service-led post around it?"
        elif category_slug == "restaurants":
            body = f"{owner}, {business}{location}: {evidence}.{detail} Want me to help check the menu or ordering path?"
        elif category_slug == "gyms":
            body = f"{owner}, {business}{location}: {evidence}.{detail} Want me to outline a retention-focused next step?"
        else:
            body = f"{owner}, {business}{location}: {evidence}.{detail} Want me to help review your profile visibility?"
        return body, "open_ended", f"Performance message uses the trigger delta or merchant's matching 7-day snapshot; magnitude={magnitude:.2f}."

    if kind == "active_planning_intent":
        topic = _topic_words(trigger)
        recent_merchant_reply = next(
            (_text(_dict(turn).get("body")) for turn in reversed(_list(merchant.get("conversation_history")))
             if _dict(turn).get("from") == "merchant" and _text(_dict(turn).get("body"))),
            _text(payload.get("merchant_last_message")),
        )
        if not recent_merchant_reply and payload.get("placeholder"):
            return None
        if category_slug == "restaurants":
            thali = next((title for title in offers if "thali" in title.lower()), "")
            history_text = " ".join(_text(_dict(turn).get("body")) for turn in _list(merchant.get("conversation_history")))
            orders = re.search(r"\b(\d+)\s+orders?/day\b", history_text, re.IGNORECASE)
            volume = f" Recent context records {orders.group(1)} orders/day." if orders else ""
            basis = f" The draft uses your active offer: {thali}." if thali else " No active menu offer is available as a draft anchor."
            return f"{owner}, here's a first-pass corporate lunch draft for {business}: ‘Weekday office thali orders: message us to discuss bulk quantities.’{basis}{volume} Minimum quantity and delivery terms aren't in the context; confirm those before sharing. What minimum quantity should I add?", "open_ended", "Planning intent advanced to a reviewable draft using active offer and supplied volume; missing terms are not invented."
        if category_slug == "gyms" and "kids yoga" in topic:
            return f"{owner}, here's the first-pass outline for {business}: ‘Kids Yoga Summer Camp’. Age range, schedule, duration, and price need confirmation before sharing. Which age range should the outline target?", "open_ended", "Planning intent moved to a category-specific outline; unconfirmed schedule and price are omitted."
        if category_slug == "salons":
            return f"{owner}, I've started a {topic} outline for {business}. Service focus and appointment window need your input before I add any price or availability. Which service should anchor it?", "open_ended", "Planning intent advances to an outline while requesting one missing, merchant-controlled detail."
        if category_slug == "dentists":
            return f"{owner}, I've started a {topic} outline. I'll use only the clinical content already supplied and leave any treatment claim out unless you verify it. Which patient question should the draft answer?", "open_ended", "Dental planning stays evidence-based and asks for one relevant content input."
        return f"{owner}, I've started an operational checklist for {topic}. I won't add medicine, pricing, or stock claims without verified context. Which operational step should it cover first?", "open_ended", "Pharmacy planning remains within operational information and asks one focused question."

    if kind == "review_theme_emerged":
        theme = _text(payload.get("theme")).replace("_", " ")
        occurrences = payload.get("occurrences_30d")
        if not theme:
            return None
        count_text = f"{occurrences} reviews in 30 days mention " if _number(occurrences) is not None else "Recent reviews mention "
        quote = _text(payload.get("common_quote"))
        quote_text = f" One supplied quote: ‘{quote[:100]}’." if quote else ""
        if category_slug == "restaurants":
            action = "Want me to draft a short delivery-expectation reply for review?"
        elif category_slug == "salons":
            action = "Want me to draft a tactful response for review?"
        elif category_slug == "dentists":
            action = "Want me to draft a patient-first response for your review?"
        else:
            action = "Want me to draft a concise response for your review?"
        return f"{owner}, {count_text}{theme}.{quote_text} A consistent response may help set expectations. {action}", "open_ended", "Review theme is grounded in the trigger's count/theme and uses one category-appropriate response action."

    if kind == "ipl_match_today" and category_slug == "restaurants":
        match = _text(payload.get("match"))
        time_text = _display_datetime(payload.get("match_time_iso"))
        if not match:
            return None
        digest_item = next((item for item in _list(category.get("digest")) if "ipl" in _text(_dict(item).get("title")).lower()), {})
        summary = _text(_dict(digest_item).get("summary"))
        source = _text(_dict(digest_item).get("source"))
        is_weeknight = payload.get("is_weeknight") is True
        if summary and source:
            impact = "+18%" if is_weeknight else "-12%"
            direction = "weeknight matches drove higher covers" if is_weeknight else "Saturday match covers were lower than the Saturday average"
            evidence = f"{impact} covers: {direction} ({source})"
        else:
            evidence = f"the supplied match is {match}"
        timing = f" at {time_text}" if time_text else ""
        recommendation = "Keep any match offer focused on delivery" if not is_weeknight else "A match-night offer may fit the weeknight pattern"
        schedule_match = re.search(r"\(([^)]*)\)", offer)
        if not is_weeknight and schedule_match and any(day in schedule_match.group(1).lower() for day in ("tue", "wed", "thu")):
            offer_note = f" Your active BOGO is {offer}, so it does not cover this non-weeknight match."
            ask = " Want me to draft a delivery post after you confirm an offer for today?"
        else:
            offer_note = f" Your active offer is {offer}." if offer else ""
            ask = " Want me to draft a short customer post using the offer already active?"
        return f"{owner}, {match}{timing}. {evidence}. {recommendation}.{offer_note}{ask}", "open_ended", "Restaurant match recommendation uses the weekday flag, sourced covers pattern, and actual offer schedule."

    if kind == "competitor_opened":
        competitor = _text(payload.get("competitor_name"))
        distance = _number(payload.get("distance_km"))
        their_offer = _text(payload.get("their_offer"))
        if not competitor:
            if payload.get("placeholder") is not True:
                return None
            if category_slug == "pharmacies":
                question = "Want me to review your current profile details for clarity, without making medicine-price comparisons?"
            elif category_slug == "restaurants":
                question = "Want me to check whether your current menu and active offer are easy to find?"
            elif category_slug == "salons":
                question = "Want me to check whether your strongest service and verified photos are easy to find?"
            elif category_slug == "gyms":
                question = "Want me to make your coaching and schedule details clearer on the profile?"
            else:
                question = "Want me to check whether your profile clearly explains your services and current offer?"
            return f"{owner}, a competitor-opening signal was flagged{location}, but no name or offer was supplied, so I won't guess. {question}", "open_ended", "Competitor signal is acknowledged without inventing the competitor, distance, or price."
        distance_text = f" {distance:g} km away" if distance is not None else " nearby"
        offer_text = f" They list {their_offer}." if their_offer else ""
        if category_slug == "dentists":
            response = f"Don't match price without checking your own service economics{'; your listed offer is ' + offer if offer else ''}."
        elif category_slug == "salons":
            response = "Lead with your strongest service and verified work photos rather than an unplanned discount."
        elif category_slug == "restaurants":
            response = "Make your strongest existing dish or offer easy to find; don't change pricing without checking margins."
        elif category_slug == "gyms":
            response = "Differentiate on coaching and schedule fit, not an unsupported price promise."
        else:
            response = "Keep any comparison factual and avoid making a medicine-price claim without checking MRP."
        return f"{owner}, {competitor} opened{distance_text}{location}.{offer_text} {response} Want me to draft one profile update that highlights your actual offer?", "open_ended", "Competitor message uses only supplied competitor facts and category-appropriate differentiation."

    if kind == "renewal_due":
        subscription = _dict(merchant.get("subscription"))
        days = _number(payload.get("days_remaining", subscription.get("days_remaining")))
        if days is None:
            return None
        plan = _text(payload.get("plan") or subscription.get("plan"))
        plan_text = f" on {plan}" if plan else ""
        amount = _number(payload.get("renewal_amount"))
        amount_text = f"; listed renewal amount ₹{amount:,.0f}" if amount is not None else ""
        return f"{owner}, your {business}{plan_text} renewal is in {int(days)} days{amount_text}. Want me to share the renewal steps?", "binary_yes_no", "Renewal nudge uses the supplied remaining days and only includes price when the trigger provides it."

    if kind == "gbp_unverified":
        if identity.get("verified") is not False and payload.get("verified") is not False:
            return None
        estimate = _pct(payload.get("estimated_uplift_pct"), signed=False)
        claim = f" The trigger estimates {estimate} potential uplift; treat it as an estimate, not a guarantee." if estimate else ""
        path = _text(payload.get("verification_path"))
        path_text = f" The supplied verification route is {path.replace('_', ' ')}." if path else ""
        return f"{owner}, {business}'s Google profile is still unverified.{claim}{path_text} Want me to walk you through the verification checklist?", "binary_yes_no", "Verification message uses the explicit status and labels any provided uplift as an estimate."

    if kind == "category_seasonal":
        trends = [_text(value).replace("_", " ") for value in _list(payload.get("trends")) if _text(value)]
        item = next((item for item in _list(category.get("digest")) if _text(_dict(item).get("kind")) == "seasonal"), {})
        title = _text(_dict(item).get("title"))
        source = _text(_dict(item).get("source"))
        if not trends and not title:
            return None
        evidence = ", ".join(trends[:2]) if trends else title
        citation = f" ({source})" if source else ""
        if category_slug == "pharmacies":
            ask = " Want me to make a shelf-priority checklist, subject to your stock data?"
        elif category_slug == "restaurants":
            ask = " Want me to outline one menu or ordering update using what you already offer?"
        elif category_slug == "salons":
            ask = " Want me to draft a seasonal service post using your active menu?"
        elif category_slug == "gyms":
            ask = " Want me to outline a retention-focused seasonal message?"
        else:
            ask = " Want me to draft a season-appropriate profile update?"
        return f"{owner}, this category's seasonal signal is {evidence}{citation}. Check it against your own stock and capacity before changing plans.{ask}", "open_ended", "Seasonal action is category-specific and framed as a signal to validate against merchant capacity."

    if kind == "festival_upcoming":
        festival = _text(payload.get("festival"))
        date = _text(payload.get("date"))
        if not festival:
            if payload.get("placeholder") is not True:
                return None
            if category_slug == "salons":
                ask = "Which local event should we plan around for appointments?"
            elif category_slug == "restaurants":
                ask = "Which local event should we plan around for bookings or menu visibility?"
            elif category_slug == "pharmacies":
                ask = "Is there a holiday-hours update customers should see?"
            elif category_slug == "gyms":
                ask = "Which holiday or local event should we account for in the class schedule?"
            else:
                ask = "Is there a holiday-hours update patients should see?"
            return f"{owner}, a festival-planning opportunity was flagged for {business}, but no event name or date was supplied, so I won't guess. {ask}", "open_ended", "Festival follow-up states the missing event details and asks one category-relevant planning question."
        date_text = f" on {date}" if date else ""
        days = _number(payload.get("days_until"))
        countdown = f" in {int(days)} days" if days is not None else ""
        offer_text = f" Your active offer is {offer}." if offer else ""
        if category_slug == "salons":
            next_step = "Would you like a festive appointment post around your current services?"
        elif category_slug == "restaurants":
            next_step = "Want me to draft a booking or menu post using an offer you already run?"
        elif category_slug == "pharmacies":
            next_step = "Want a factual seasonal-hours post, if your hours will change?"
        elif category_slug == "dentists":
            next_step = "Want a simple holiday-hours update for your profile?"
        else:
            next_step = "Want a schedule update for members around the holiday?"
        return f"{owner}, {festival}{countdown}{date_text} is coming up.{offer_text} {next_step}", "open_ended", "Festival message uses only the provided date/countdown and the category's relevant operational action."

    if kind == "winback_eligible":
        subscription = _dict(merchant.get("subscription"))
        days = _number(payload.get("days_since_expiry", subscription.get("days_since_expiry")))
        lapsed = _number(payload.get("lapsed_customers_added_since_expiry"))
        evidence = f" Your subscription expired {int(days)} days ago." if days is not None else ""
        if lapsed is not None:
            evidence += f" {int(lapsed)} customers are newly lapsed since then."
        if not evidence:
            return None
        return f"{owner}, {business}{evidence} Would a quick account review help you decide whether to restart?", "binary_yes_no", "Merchant win-back references supplied expiry/lapse details and asks for a review rather than assuming renewal."

    if kind == "milestone_reached":
        metric = _text(payload.get("metric")).replace("_", " ")
        value = payload.get("value_now")
        milestone = payload.get("milestone_value")
        if not metric or _number(value) is None:
            if payload.get("placeholder") is not True:
                return None
            return f"{owner}, a business milestone was flagged for {business}, but its metric and count weren't supplied. Should I verify those details before drafting an update?", "open_ended", "Milestone follow-up does not invent the metric or count omitted from the trigger."
        milestone_text = f" and are close to {milestone}" if _number(milestone) is not None and payload.get("is_imminent") else ""
        if metric == "review count":
            action = " Want me to draft a thank-you post that invites happy customers to share honest feedback?"
        elif category_slug == "restaurants":
            action = " Want me to help turn the milestone into a short customer update?"
        elif category_slug == "salons":
            action = " Want a short client thank-you post for the milestone?"
        else:
            action = " Want me to draft a concise milestone update?"
        return f"{owner}, {business} reached {int(float(value))} {metric}{milestone_text}.{action}", "open_ended", "Milestone uses supplied count and avoids incentivizing or fabricating reviews."

    if kind == "dormant_with_vera":
        days = _number(payload.get("days_since_last_merchant_message"))
        if days is None:
            if payload.get("placeholder") is not True:
                return None
            if category_slug == "salons":
                question = "What service are customers asking about most this week? I can shape one short service update."
            elif category_slug == "restaurants":
                question = "Which dish or time slot would you most like to focus on this week?"
            elif category_slug == "gyms":
                question = "Which class or time slot needs attention this week?"
            elif category_slug == "pharmacies":
                question = "Which service question would be useful to answer clearly on your profile?"
            else:
                question = "Which patient question would be most useful to answer this week?"
            return f"{owner}, quick check-in from Vera: {question}", "open_ended", "Low-pressure category question uses the dormancy trigger without inventing a duration or prior topic."
        last_topic = _text(payload.get("last_topic")).replace("_", " ")
        topic_text = f" Your last topic was {last_topic}." if last_topic else ""
        if category_slug == "salons":
            question = "What's one service customers have asked about most this week? I can turn your answer into a concise profile post."
        elif category_slug == "restaurants":
            question = "Which dish or day needs more orders this week? I can help shape one practical update."
        elif category_slug == "gyms":
            question = "Which class or time slot would you most like to fill? I can help draft one retention or schedule message."
        elif category_slug == "pharmacies":
            question = "Is there one service or opening-hours update customers keep asking about? I can help draft a factual profile note."
        else:
            question = "Which patient-facing topic would be most useful this week? I can help draft a reviewable, evidence-based note."
        return f"{owner}, it's been {int(days)} days since our last merchant conversation.{topic_text} {question}", "open_ended", "Category-specific, low-pressure question follows the supplied dormancy trigger."

    if kind == "curious_ask_due":
        if category_slug == "salons":
            question = f"What service has been asked about most at {business} this week? I can turn your answer into a short service post."
        elif category_slug == "restaurants":
            question = f"Which item or time slot would you most like to fill at {business} this week? I can shape one practical post around your answer."
        elif category_slug == "gyms":
            question = f"Which class or membership question is coming up most at {business}? I can help turn your answer into a clear member update."
        elif category_slug == "pharmacies":
            question = f"Which service question comes up most at {business}? I can help draft a factual, pharmacist-reviewed answer."
        else:
            question = f"Which patient question has come up most at {business} this week? I can help draft a careful, evidence-based answer."
        return f"{owner}, quick operator check: {question}", "open_ended", "Curiosity cadence asks one category-relevant question and offers a bounded, reviewable follow-up."

    return None


def _compose_candidate(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any] | None = None) -> dict[str, Any] | None:
    scope = _text(trigger.get("scope"))
    if scope not in {"merchant", "customer"}:
        return None
    if scope == "customer":
        if not customer or not _is_customer_eligible(customer, trigger):
            return None
        composed = _customer_message(_text(category.get("slug")), merchant, trigger, customer)
        send_as = "merchant_on_behalf"
    else:
        composed = _merchant_message(category, merchant, trigger)
        send_as = "vera"
    if not composed:
        return None
    body, cta, rationale = composed
    body = re.sub(r"\s+", " ", body).strip()
    if not body or len(body) > 900 or re.search(r"https?://|www\.", body, re.IGNORECASE):
        return None
    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": _text(trigger.get("suppression_key")) or f"{_text(trigger.get('id'))}:{_text(trigger.get('kind'))}",
        "rationale": rationale,
    }


def compose(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compose one grounded deterministic message, or return an empty result when unsafe/unsupported."""
    return _compose_candidate(category, merchant, trigger, customer) or {
        "body": "", "cta": "none", "send_as": "vera",
        "suppression_key": _text(trigger.get("suppression_key")),
        "rationale": "No safe, sufficiently grounded action was available.",
    }


def _magnitude(trigger: dict[str, Any], merchant: dict[str, Any]) -> float:
    payload = _dict(trigger.get("payload"))
    magnitudes = [abs(_number(payload.get(field)) or 0.0) for field in ("delta_pct", "perf_dip_pct", "estimated_uplift_pct")]
    delta = _dict(_dict(merchant.get("performance")).get("delta_7d"))
    magnitudes.extend(abs(_number(value) or 0.0) for key, value in delta.items() if key.endswith("_pct"))
    return min(max(magnitudes, default=0.0), 100.0)


def _rank(trigger: dict[str, Any], merchant: dict[str, Any], now: datetime) -> tuple[int, int, int, float, str]:
    kind = _text(trigger.get("kind"))
    urgency = int(_number(trigger.get("urgency")) or 0)
    expiry = _parse_datetime(trigger.get("expires_at"))
    expiring = int(expiry is not None and 0 <= (expiry - now).total_seconds() <= 86400)
    magnitude = _magnitude(trigger, merchant)
    return (KIND_PRIORITY.get(kind, 0), urgency, expiring, magnitude, _text(trigger.get("id")))


def _context(scope: str, context_id: str) -> dict[str, Any] | None:
    stored = CONTEXTS.get((scope, context_id))
    return _dict(stored.get("payload")) if stored else None


def _template_name(trigger: dict[str, Any], customer: dict[str, Any] | None) -> str:
    kind = _safe_id(_text(trigger.get("kind")) or "update").lower()
    prefix = "merchant" if customer else "vera"
    return f"{prefix}_{kind}_v1"[:64]


def _conversation_id(merchant_id: str, customer_id: str | None, suppression_key: str) -> str:
    digest = hashlib.sha256(suppression_key.encode("utf-8")).hexdigest()[:10]
    audience = _safe_id(customer_id) if customer_id else "merchant"
    return f"conv_{_safe_id(merchant_id)}_{audience}_{digest}"[:180]


def _is_first_touch(merchant: dict[str, Any], now: datetime) -> bool:
    for turn in _list(merchant.get("conversation_history")):
        turn = _dict(turn)
        if turn.get("from") != "merchant":
            continue
        timestamp = _parse_datetime(turn.get("ts"))
        if timestamp and 0 <= (now - timestamp).total_seconds() < 86400:
            return False
    return True


def _valid_request_fields(value: Any, required: tuple[str, ...]) -> str | None:
    if not isinstance(value, dict):
        return "body_must_be_object"
    for key in required:
        if key not in value:
            return f"missing_{key}"
    return None


@app.get("/v1/healthz")
def healthz() -> dict[str, Any]:
    with LOCK:
        counts = {scope: 0 for scope in ("category", "merchant", "customer", "trigger")}
        for scope, _ in CONTEXTS:
            counts[scope] = counts.get(scope, 0) + 1
    return {"status": "ok", "uptime_seconds": int(time.time() - STARTED_AT), "contexts_loaded": counts}


@app.get("/v1/metadata")
def metadata() -> dict[str, Any]:
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera Challenge Submission"),
        "team_members": [item.strip() for item in os.getenv("TEAM_MEMBERS", "").split(",") if item.strip()],
        "model": "deterministic-rules-v1",
        "approach": "Grounded signal ranking with category strategies, consent checks, suppression, and deterministic replies",
        "contact_email": os.getenv("CONTACT_EMAIL", ""),
        "version": "1.0.0",
        "submitted_at": os.getenv("SUBMITTED_AT", ""),
    }


@app.post("/v1/context")
async def push_context(request: Request) -> JSONResponse:
    raw = await request.body()
    if len(raw) > 500_000:
        return JSONResponse({"accepted": False, "reason": "payload_too_large", "details": "Maximum context body is 500 KB."}, status_code=413)
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"accepted": False, "reason": "malformed_json", "details": "Request body must be valid UTF-8 JSON."}, status_code=400)
    problem = _valid_request_fields(body, ("scope", "context_id", "version", "payload"))
    if problem:
        return JSONResponse({"accepted": False, "reason": "invalid_request", "details": problem}, status_code=400)
    scope = _text(body.get("scope"))
    context_id = _text(body.get("context_id"))
    version = body.get("version")
    payload = body.get("payload")
    if scope not in {"category", "merchant", "customer", "trigger"}:
        return JSONResponse({"accepted": False, "reason": "invalid_scope", "details": "scope must be category, merchant, customer, or trigger."}, status_code=400)
    if not context_id or isinstance(version, bool) or not isinstance(version, int) or version < 1 or not isinstance(payload, dict):
        return JSONResponse({"accepted": False, "reason": "invalid_context", "details": "context_id, positive integer version, and object payload are required."}, status_code=400)
    key = (scope, context_id)
    with LOCK:
        current = CONTEXTS.get(key)
        if current and version <= current["version"]:
            return JSONResponse({"accepted": False, "reason": "stale_version", "current_version": current["version"]}, status_code=409)
        CONTEXTS[key] = {"version": version, "payload": payload, "delivered_at": body.get("delivered_at")}
    return JSONResponse({"accepted": True, "ack_id": f"ack_{_safe_id(context_id)}_v{version}", "stored_at": _iso_now()}, status_code=200)


@app.post("/v1/tick")
async def tick(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"actions": [], "error": "malformed_json"}, status_code=400)
    problem = _valid_request_fields(body, ("now", "available_triggers"))
    if problem:
        return JSONResponse({"actions": [], "error": problem}, status_code=400)
    now = _parse_datetime(body.get("now"))
    ids = body.get("available_triggers")
    if now is None or not isinstance(ids, list):
        return JSONResponse({"actions": [], "error": "invalid_tick"}, status_code=400)
    with LOCK:
        context_versions = sorted((scope, context_id, stored["version"]) for (scope, context_id), stored in CONTEXTS.items())
        tick_material = json.dumps([now.isoformat(), sorted(str(value) for value in ids), context_versions], separators=(",", ":"))
        tick_key = hashlib.sha256(tick_material.encode("utf-8")).hexdigest()
        if tick_key in TICK_CACHE:
            return JSONResponse(TICK_CACHE[tick_key])
        choices: dict[str, tuple[tuple[int, int, int, float, str], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None, str]] = {}
        for trigger_id in ids:
            trigger = _context("trigger", _text(trigger_id))
            if not trigger:
                continue
            merchant_id = _text(trigger.get("merchant_id"))
            if not merchant_id or merchant_id in OPTED_OUT_MERCHANTS:
                continue
            merchant = _context("merchant", merchant_id)
            if not merchant:
                continue
            expiry = _parse_datetime(trigger.get("expires_at"))
            if expiry and now > expiry:
                continue
            category_slug = _text(merchant.get("category_slug"))
            category = _context("category", category_slug)
            if not category:
                continue
            customer = None
            customer_id = _text(trigger.get("customer_id")) or _text(_dict(trigger.get("payload")).get("customer_id"))
            if _text(trigger.get("scope")) == "customer":
                if customer_id in OPTED_OUT_CUSTOMERS:
                    continue
                customer = _context("customer", customer_id)
                if not customer or _text(customer.get("merchant_id")) != merchant_id:
                    continue
            candidate = _compose_candidate(category, merchant, trigger, customer)
            if not candidate:
                continue
            suppression_key = candidate["suppression_key"]
            if suppression_key in DELIVERED_KEYS:
                continue
            audience_key = f"{merchant_id}:{customer_id or 'merchant'}"
            last_sent = LAST_SENT.get(audience_key)
            if last_sent and (now - last_sent).total_seconds() < 4 * 3600:
                continue
            rank = _rank(trigger, merchant, now)
            prior = choices.get(merchant_id)
            entry = (rank, trigger, merchant, category, customer, customer_id)
            if prior is None or rank > prior[0]:
                choices[merchant_id] = entry

        actions = []
        for merchant_id, (_, trigger, merchant, _, customer, customer_id) in sorted(choices.items()):
            candidate = _compose_candidate(_context("category", _text(merchant.get("category_slug"))) or {}, merchant, trigger, customer)
            if not candidate:
                continue
            suppression_key = candidate["suppression_key"]
            conversation_id = _conversation_id(merchant_id, customer_id or None, suppression_key)
            first_touch = _is_first_touch(merchant, now)
            action = {
                "conversation_id": conversation_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id or None,
                "send_as": candidate["send_as"],
                "trigger_id": _text(trigger.get("id")),
                "template_name": _template_name(trigger, customer) if first_touch else None,
                "template_params": [(_text(_dict(customer.get("identity")).get("name")) if customer else _owner(merchant, _text(merchant.get("category_slug")))), candidate["body"]] if first_touch else [],
                "body": candidate["body"],
                "cta": candidate["cta"],
                "suppression_key": suppression_key,
                "rationale": candidate["rationale"],
            }
            actions.append(action)
            DELIVERED_KEYS.add(suppression_key)
            LAST_SENT[f"{merchant_id}:{customer_id or 'merchant'}"] = now
            CONVERSATIONS.setdefault(conversation_id, {"merchant_id": merchant_id, "customer_id": customer_id or None, "turns": [], "sent_bodies": [], "auto_replies": 0, "ended": False, "last_action": action})
            CONVERSATIONS[conversation_id]["sent_bodies"].append(candidate["body"])
            CONVERSATIONS[conversation_id]["last_action"] = action
        result = {"actions": actions[:20]}
        TICK_CACHE[tick_key] = result
        return JSONResponse(result)


def _classify_reply(message: str) -> str:
    normalized = re.sub(r"[^a-z0-9\s]", " ", message.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return "unclear"
    if normalized in {"stop", "stop vera", "stop messages"} or any(pattern in normalized for pattern in ("stop messaging", "stop sending", "unsubscribe", "do not message", "dont message", "not interested", "leave me alone", "useless spam", "shut up")):
        return "opt_out"
    if any(phrase in normalized for phrase in ("thank you for contacting", "thanks for contacting", "our team will respond", "respond shortly", "team will get back", "automated assistant", "working hours are", "we are currently closed")):
        return "auto_reply"
    if any(token in normalized for token in ("gst", "tax filing", "unrelated", "website development", "personal loan")):
        return "off_topic"
    if re.search(r"\b(yes|yep|yeah|sure|go ahead|lets do it|let s do it|proceed|confirm|send it|please do|ok do it)\b", normalized):
        return "acceptance"
    if any(token in normalized for token in ("how much", "what does", "which", "when", "where", "how do", "can you explain", "tell me more", "details")) or "?" in message:
        return "question"
    if any(token in normalized for token in ("too expensive", "no time", "not now", "busy", "already have", "already doing", "concern", "worried", "but ")):
        return "objection"
    if any(token in normalized for token in ("no thanks", "no thank you", "maybe later", "not interested in this")):
        return "rejection"
    if normalized in {"ok", "okay", "thanks", "thank you", "noted", "understood", "fine", "great"}:
        return "acknowledgment"
    if len(normalized.split()) <= 2:
        return "unclear"
    return "clarification"


def _reply_result(action: str, rationale: str, body: str = "", cta: str = "none", wait_seconds: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"action": action, "rationale": rationale}
    if action == "send":
        result.update({"body": body, "cta": cta})
    elif action == "wait":
        result["wait_seconds"] = wait_seconds or 1800
    return result


@app.post("/v1/reply")
async def reply(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_reply_result("end", "Malformed reply payload; no safe response can be composed."), status_code=400)
    problem = _valid_request_fields(body, ("conversation_id", "from_role", "message", "turn_number"))
    if problem:
        return JSONResponse(_reply_result("end", f"Invalid reply payload: {problem}."), status_code=400)
    conversation_id = _text(body.get("conversation_id"))
    message = _text(body.get("message"))[:4000]
    from_role = _text(body.get("from_role"))
    turn_number = body.get("turn_number")
    if not conversation_id or from_role not in {"merchant", "customer"} or isinstance(turn_number, bool) or not isinstance(turn_number, int):
        return JSONResponse(_reply_result("end", "Invalid conversation, role, or turn number."), status_code=400)
    normalized = re.sub(r"\s+", " ", message).strip().lower()
    reply_cache_key = hashlib.sha256(f"{conversation_id}|{turn_number}|{from_role}|{normalized}".encode("utf-8")).hexdigest()
    with LOCK:
        cached = REPLY_CACHE.get(reply_cache_key)
        if cached is not None:
            return JSONResponse(cached)
        state = CONVERSATIONS.setdefault(conversation_id, {"merchant_id": _text(body.get("merchant_id")), "customer_id": _text(body.get("customer_id")) or None, "turns": [], "sent_bodies": [], "auto_replies": 0, "ended": False})

        def respond(result: dict[str, Any]) -> JSONResponse:
            if result.get("action") == "send":
                sent_bodies = state.setdefault("sent_bodies", [])
                response_body = _text(result.get("body"))
                if response_body in sent_bodies:
                    result = _reply_result("wait", "This response would repeat an earlier message; pausing instead of sending it again.", wait_seconds=1800)
                else:
                    sent_bodies.append(response_body)
            REPLY_CACHE[reply_cache_key] = result
            return JSONResponse(result)

        if state.get("ended"):
            return respond(_reply_result("end", "This conversation has already been closed."))
        if any(_dict(turn).get("message_normalized") == normalized for turn in state["turns"] if _dict(turn).get("classification") == "auto_reply"):
            state["auto_replies"] += 1
        classification = _classify_reply(message)
        if classification == "auto_reply":
            state["auto_replies"] += 1 if not any(_dict(turn).get("message_normalized") == normalized for turn in state["turns"]) else 0
            merchant_id = _text(state.get("merchant_id") or body.get("merchant_id"))
            if from_role == "merchant" and merchant_id:
                previous_message, previous_count = AUTO_REPLY_STREAK.get(merchant_id, ("", 0))
                streak_count = previous_count + 1 if previous_message == normalized else 1
                AUTO_REPLY_STREAK[merchant_id] = (normalized, streak_count)
                state["auto_replies"] = max(state["auto_replies"], streak_count)
        elif from_role == "merchant":
            AUTO_REPLY_STREAK.pop(_text(state.get("merchant_id") or body.get("merchant_id")), None)
        state["turns"].append({"role": from_role, "message": message, "message_normalized": normalized, "classification": classification, "turn_number": turn_number})
        if classification in {"opt_out", "rejection"}:
            state["ended"] = True
            if classification == "opt_out":
                if from_role == "customer" and state.get("customer_id"):
                    OPTED_OUT_CUSTOMERS.add(state["customer_id"])
                elif state.get("merchant_id"):
                    OPTED_OUT_MERCHANTS.add(state["merchant_id"])
            return respond(_reply_result("end", "Acknowledged the explicit rejection/opt-out and closed the conversation without another pitch."))
        if classification == "auto_reply":
            if state["auto_replies"] >= 3:
                state["ended"] = True
                return respond(_reply_result("end", "Repeated identical automated replies indicate no owner engagement; closing rather than consuming more turns."))
            return respond(_reply_result("wait", "This looks like a canned business-hours reply. Waiting for a person rather than continuing the pitch.", wait_seconds=14400 if state["auto_replies"] == 1 else 86400))
        if classification == "acceptance":
            previous = _dict(state.get("last_action"))
            trigger_id = _text(previous.get("trigger_id"))
            trigger = _context("trigger", trigger_id) if trigger_id else {}
            trigger = _dict(trigger)
            merchant_id = _text(state.get("merchant_id") or body.get("merchant_id"))
            merchant = _context("merchant", merchant_id) or {}
            category = _context("category", _text(merchant.get("category_slug"))) or {}
            kind = _text(trigger.get("kind"))
            topic = _topic_words(trigger) if trigger else "the next step we discussed"
            if kind in {"research_digest", "research_digest_release", "cde_opportunity", "regulation_change"}:
                item = _find_digest_item(category, trigger)
                title = _text(item.get("title"))
                summary = _text(item.get("summary"))
                source = _text(item.get("source"))
                if title and summary:
                    body_text = f"You asked for the {title} details. {summary}"
                    if source:
                        body_text += f" Source: {source}."
                    body_text += " I can format this into a patient/customer draft in a follow-up; this endpoint cannot attach files or send it externally."
                else:
                    body_text = f"You asked to proceed with {topic}. I don't have the linked source details in the current context, so I won't invent them."
            elif kind == "active_planning_intent":
                body_text = f"You asked to proceed with {topic}. I can prepare a reviewable starter using the merchant details already supplied; any missing price, schedule, quantity, or availability still needs confirmation before sharing."
            else:
                body_text = f"Understood. I'll move straight to the next step for {topic}, using only the details already in this conversation. This reply endpoint cannot publish changes or send messages to customers."
            state["ended"] = True
            return respond(_reply_result(
                "send",
                "Acknowledged acceptance and returned the supported next-step material without another qualifying question or claiming an external action.",
                body=body_text,
                cta="none",
            ))
        if classification == "question":
            lower = message.lower()
            if any(word in lower for word in ("price", "cost", "fee", "₹")):
                response = "I don't have a verified amount in this conversation, so I won't guess."
            elif any(word in lower for word in ("how", "what", "details", "explain")):
                response = "I can clarify the specific item we were discussing, using only the details provided here."
            else:
                response = "I heard your question. I don't have enough verified context to answer that accurately yet."
            return respond(_reply_result("send", "Answered cautiously and asked one focused clarification.", body=f"{response} Which detail should I clarify?", cta="open_ended"))
        if classification == "objection":
            return respond(_reply_result("send", "Acknowledged the objection and reduced pressure without repeating the original pitch.", body="Understood. I won't push this now. Would a brief summary here be more useful?", cta="open_ended"))
        if classification == "off_topic":
            return respond(_reply_result("send", "Politely declined the unrelated request and kept the response within Vera's scope.", body="I can't help with that request here. I can stay with the Vera topic we were discussing, or we can close this conversation.", cta="open_ended"))
        if classification == "acknowledgment":
            return respond(_reply_result("wait", "Acknowledged the merchant's brief response; pausing instead of sending an unnecessary follow-up.", wait_seconds=1800))
        if turn_number >= 6:
            state["ended"] = True
            return respond(_reply_result("end", "The reply remains unclear after several turns; closing to avoid repetitive outreach."))
        if len(state["turns"]) == 1:
            return respond(_reply_result("send", "Asked one low-friction clarification rather than guessing at intent.", body=f"I may have missed your point: “{message[:160]}”. What would be the most useful next step on the topic we raised?", cta="open_ended"))
        return respond(_reply_result("wait", "The reply is still ambiguous; pausing rather than guessing at intent.", wait_seconds=3600))


@app.post("/v1/teardown")
def teardown() -> dict[str, bool]:
    with LOCK:
        CONTEXTS.clear()
        CONVERSATIONS.clear()
        DELIVERED_KEYS.clear()
        OPTED_OUT_MERCHANTS.clear()
        OPTED_OUT_CUSTOMERS.clear()
        LAST_SENT.clear()
        TICK_CACHE.clear()
        REPLY_CACHE.clear()
        AUTO_REPLY_STREAK.clear()
    return {"cleared": True}