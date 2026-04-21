from __future__ import annotations
from datetime import date, timedelta
from django.utils import timezone
from typing import Optional
import hmac
import hashlib
from django.conf import settings
def last10_digits(raw: str) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""

def phone_hash(raw: str) -> str:
    """Stable lookup hash for last 10 digits; uses a server-side salt."""
    l10 = last10_digits(raw)
    if not l10:
        return ""
    return hashlib.sha256((settings.PHONE_HASH_SALT + l10).encode("utf-8")).hexdigest()

def hmac_sha256(value: bytes) -> str:
    """
    Returns HMAC-SHA256 digest as hex string using SEARCH_PEPPER key.
    """
    key = settings.SEARCH_PEPPER.encode("utf-8")  # ya jo bhi aapka key hai
    return hmac.new(key, value, hashlib.sha256).hexdigest()

def birth_window(dob: date, min_offset_days: int, max_offset_days: Optional[int]):
    dd = dob + timedelta(days=min_offset_days)
    du = None if max_offset_days is None else dob + timedelta(days=max_offset_days)
    return dd, du

def booster_window(prev_given: date, booster_min: int, prev_min: int, booster_max: Optional[int]):
    delta_min = max(0, booster_min - prev_min)
    dd = prev_given + timedelta(days=delta_min)
    if booster_max is None:
        return dd, None
    delta_max = max(0, booster_max - prev_min)
    return dd, prev_given + timedelta(days=delta_max)

def _later(a: Optional[date], b: Optional[date]) -> Optional[date]:
    if a and b: return max(a, b)
    return a or b

def series_window(dob: date, dep, prev_given: Optional[date], prev_min_override: Optional[int] = None):
    """
    Compute due window; if a logical previous exists but isn't given -> (None, None).
    """
    # first-in-series if neither explicit previous nor series fallback exists
    if not dep.previous_dose_id and prev_min_override is None:
        return birth_window(dob, dep.min_offset_days, dep.max_offset_days)

    if not prev_given:
        return None, None  # not eligible

    dd_birth, du_birth = birth_window(dob, dep.min_offset_days, dep.max_offset_days)
    prev_min = prev_min_override if prev_min_override is not None else dep.previous_dose.min_offset_days
    dd_catch, du_catch = booster_window(prev_given, dep.min_offset_days, prev_min, dep.max_offset_days)

    policy = getattr(dep, "anchor_policy", "L")
    if policy == "I":
        return dd_catch, du_catch
    elif policy == "A":
        return dd_birth, du_birth
    else:
        return _later(dd_birth, dd_catch), _later(du_birth, du_catch)


def today() -> date:
    return timezone.localdate()

def status_code_for(due_date: date | None, due_until: date | None, given_date: date | None) -> str:
    if given_date:
        return "given-on-date"
    if not due_date:
        return "due-on-a-future-date"
    t = today()
    if t < due_date:
        return "due-on-a-future-date"
    if (due_until is None and t >= due_date) or (due_until is not None and due_date <= t <= due_until):
        return "due-as-on-date"
    if due_until is not None and t > due_until:
        return "overdue"
    return "due-on-a-future-date"

# Simple Indian E.164 normaliser for WhatsApp (minimal; adapt as needed)
def normalize_msisdn(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return raw.strip()
    if digits.startswith("91") and len(digits) == 12:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+91{digits}"
    if raw.strip().startswith("+"):
        return raw.strip()
    return f"+{digits}"

# Map state -> primary language code
STATE_LANG = {
    "Andaman and Nicobar": "ta",   # treat Islands synonym below
    "Andaman and Nicobar Islands": "ta",
    "Andhra Pradesh": "te",
    "Arunachal Pradesh": "hi",
    "Assam": "bn",
    "Bihar": "hi",
    "Chandigarh": "hi",
    "Chhattisgarh": "hi",
    "Dadra and Nagar Haveli": "gu",
    "Daman and Diu": "gu",
    "Dadra and Nagar Haveli and Daman and Diu": "gu",
    "Delhi": "hi",
    "Goa": "mr",
    "Gujarat": "gu",
    "Haryana": "hi",
    "Himachal Pradesh": "hi",
    "Jammu and Kashmir": "hi",
    "Jharkhand": "hi",
    "Karnataka": "kn",
    "Kerala": "ml",
    "Lakshadweep": "ml",
    "Madhya Pradesh": "hi",
    "Maharashtra": "mr",
    "Manipur": "hi",
    "Meghalaya": "hi",
    "Mizoram": "hi",
    "Nagaland": "hi",
    "Odisha": "hi",
    "Puducherry": "ta",
    "Punjab": "hi",
    "Rajasthan": "hi",
    "Sikkim": "en",
    "Tamil Nadu": "ta",
    "Telangana": "te",
    "Tripura": "bn",
    "Uttar Pradesh": "hi",
    "Uttarakhand": "hi",
    "West Bengal": "bn",
    "Ladakh": "hi",
    "Ladak": "hi",  # alias as requested
}

# Language templates (from VaccinesMessages.pdf). Placeholders: {link} {doctor}
WHATSAPP_PATIENT_CARD_MESSAGES = {
    "en": (
        "Dear Parent,\n"
        "I'm sending you a vaccination card to help you keep track of your child's vaccines and to "
        "ensure you receive timely reminders for any upcoming doses.\n"
        "Please enter the dates of the vaccines your child has already received.\n"
        "Link: {link}\n"
        "Best regards,\n"
        "Dr. {doctor}"
    ),
    "hi": (
        "प्रिय अभिभावक,\n"
        "मैं आपको आपके बच्चे के टीकाकरण को ट्रैक करने और आगामी खुराक के लिए समय पर अनुस्मारक प्राप्त करने में "
        "मदद के लिए एक टीकाकरण कार्ड भेज रहा/रही हूँ।\n"
        "कृपया अपने बच्चे को पहले से दी गई टीकों की तिथियाँ दर्ज करें।\n"
        "लिंक: {link}\n"
        "सादर,\n"
        "डॉ. {doctor}"
    ),
    "mr": (
        "प्रिय पालक,\n"
        "आपल्या मुलाच्या लसीकरणाचे वेळापत्रक ठेवण्यासाठी आणि येणाऱ्या डोसच्या आठवणीसाठी मी आपल्याला "
        "एक लसीकरण कार्ड पाठवत आहे.\n"
        "कृपया आपल्या मुलाला आधीपासून दिलेल्या लसींच्या तारखा नमूद करा.\n"
        "लिंक: {link}\n"
        "धन्यवाद,\n"
        "डॉ. {doctor}"
    ),
    "kn": (
        "ಪೋಷಕರೇ,\n"
        "ನಿಮ್ಮ ಮಗುವಿನ ಲಸಿಕೆಗಳನ್ನು ಟ್ರ್ಯಾಕ್ ಮಾಡಲು ಮತ್ತು ಮುಂದಿನ ಡೋಸ್‌ಗಳ ಸಮಯೋಚಿತ ನೆನಪಿಕೆಗಳನ್ನು ಪಡೆಯಲು "
        "ನಾನು ನಿಮಗೆ ಲಸಿಕೆ ಕಾರ್ಡ್ ಅನ್ನು ಕಳುಹಿಸುತ್ತಿದ್ದೇನೆ.\n"
        "ದಯವಿಟ್ಟು ನಿಮ್ಮ ಮಗುವಿಗೆ ಈಗಾಗಲೇ ನೀಡಿರುವ ಲಸಿಕೆಗಳ ದಿನಾಂಕಗಳನ್ನು ನಮೂದಿಸಿ.\n"
        "ಲಿಂಕ್: {link}\n"
        "ಧನ್ಯವಾದಗಳು,\n"
        "ಡಾ. {doctor}"
    ),
    "ml": (
        "പ്രിയ മാതാപിതാക്കളേ,\n"
        "നിങ്ങളുടെ കുഞ്ഞിന്റെ വാക്സിനുകൾ ട്രാക്ക് ചെയ്യാനും വരാനിരിക്കുന്ന ഡോസുകളുടെ സമയോചിത ഓർമ്മപ്പെടുത്തലുകൾ "
        "ലഭിക്കാനും നിങ്ങളെ സഹായിക്കുന്നതിനായി ഞാൻ ഒരു വാക്സിനേഷൻ കാർഡ് അയക്കുന്നു.\n"
        "കുഞ്ഞിന് ഇതുവരെ നൽകിയ വാക്സിനുകളുടെ തീയതികൾ രേഖപ്പെടുത്തുക.\n"
        "ലിങ്ക്: {link}\n"
        "ആശംസകളോടെ,\n"
        "ഡോ. {doctor}"
    ),
    "te": (
        "ప్రియ తల్లిదండ్రులకు,\n"
        "మీ బిడ్డకు ఇవ్వబడిన టీకాలను ట్రాక్ చేసుకోవడానికి మరియు రాబోయే డోసుల గురించి సమయానుకూల రిమైండర్లు "
        "పొందడానికి మీకు ఒక టీకా కార్డ్ పంపిస్తున్నాను.\n"
        "ఇప్పటికే ఇచ్చిన టీకాల తేదీలను నమోదు చేయండి.\n"
        "లింక్: {link}\n"
        "ధన్యవాదాలు,\n"
        "డా. {doctor}"
    ),
    "ta": (
        "அன்புள்ள பெற்றோர்களே,\n"
        "உங்கள் குழந்தையின் தடுப்பூசிகளைக் கண்காணிக்கவும், வரவிருக்கும் டோஸ்களுக்கு நேர்மையான நினைவூட்டல்கள் "
        "பெறவும் உதவும் ஒரு தடுப்பூசி அட்டையை அனுப்புகிறேன்.\n"
        "ஏற்கனவே பெற்ற தடுப்பூசிகளின் தேதிகளை உள்ளிடவும்.\n"
        "இணைப்பு: {link}\n"
        "நன்றி,\n"
        "டாக்டர் {doctor}"
    ),
}

def build_patient_message(lang_code: str, doctor_name: str, link: str) -> str:
    tpl = WHATSAPP_PATIENT_CARD_MESSAGES.get(lang_code) or WHATSAPP_PATIENT_CARD_MESSAGES["en"]
    return tpl.format(link=link, doctor=doctor_name)

def choose_two_languages_for_state(state: str) -> tuple[str, str]:
    primary = STATE_LANG.get(state) or "en"
    secondary = "hi" if primary == "en" else "en"
    # If primary not in templates (e.g., bn/gu), degrade to 'en'; secondary becomes 'hi'
    if primary not in WHATSAPP_PATIENT_CARD_MESSAGES:
        primary = "en"
        secondary = "hi"
    return primary, secondary

def build_whatsapp_url(e164_number: str, message: str, request=None) -> str:
    """
    Build a WhatsApp API deep link with a prefilled message.
    """
    to_e164 = normalize_msisdn(e164_number or "")
    to = to_e164.replace("+", "")
    import urllib.parse
    return f"https://api.whatsapp.com/send?phone={to}&text={urllib.parse.quote(message)}"

def build_whatsapp_web_url(e164_number: str, message: str) -> str:
    # Direct WhatsApp Web URL (requires user to be logged in)
    to = (e164_number or "").replace("+", "")
    import urllib.parse
    return f"https://web.whatsapp.com/send?phone={to}&text={urllib.parse.quote(message)}"

# --- Reminder helpers (status & messages) ---

from datetime import timedelta
from django.utils import timezone
from .messages import VACCINE_OVERDUE_TEMPLATES

def classify_reminder_status(cd) -> str:
    """
    Returns one of: 'given', 'upcoming_24h', 'overdue_today', 'not_eligible'
    Rules:
      - Given -> 'given'
      - Upcoming (next 24h): due_date == (today + 1)
      - Overdue Today (with 3-day retention): due_date in {today, today-1, today-2}
      - Else -> 'not_eligible'
    """
    if cd.given_date:
        return "given"
    if not cd.due_date:
        return "not_eligible"

    today_ = timezone.localdate()
    if cd.due_date == today_ + timedelta(days=1):
        return "upcoming_24h"
    if cd.due_date in {today_, today_ - timedelta(days=1), today_ - timedelta(days=2)}:
        return "overdue_today"
    return "not_eligible"

def _template_for(lang: str) -> str:
    # Fallback to English if language not present
    return VACCINE_OVERDUE_TEMPLATES.get(lang) or VACCINE_OVERDUE_TEMPLATES["en"]

def build_vaccine_reminder_message(lang: str, *, child_name: str, vaccine_name: str,
                                   due_date, education_url: str, doctor_name: str) -> str:
    """
    Interpolate the localized template with placeholders.
    Supported placeholders: {child}, {vaccine}, {due_date}, {link}, {doctor}
    """
    tpl = _template_for(lang)
    return tpl.format(
        child=child_name,
        vaccine=vaccine_name,
        due_date=due_date.strftime("%d %b %Y") if due_date else "",
        link=education_url or "",
        doctor=doctor_name,
    )

def build_bilingual_vaccine_message(child, vaccine, due_date, doctor, education_link: Optional[str] = None) -> str:
    """
    Compose bilingual reminder text. If `education_link` is provided, it is
    used for both languages as the {link} placeholder so parents see a
    page with videos in 8 languages. Fallback remains the per-language
    VaccineEducationPatient.video_url logic if `education_link` is None.
    """
    primary, secondary = choose_two_languages_for_state(child.state or doctor.clinic.state)

    if education_link:
        # Use the public, language-agnostic page for both messages
        edu_primary = edu_secondary = education_link
    else:
        # existing logic (kept intact) that looks up per-language URLs
        def _education_url_for(lang_code: str) -> str:
            default_url = getattr(vaccine, "education_parent_url", "") or ""
            try:
                from .models import VaccineEducationPatient
                pe = (VaccineEducationPatient.objects.using("masters")
                      .filter(vaccine=vaccine, is_active=True, language=lang_code)
                      .order_by("rank")
                      .first())
                if pe and pe.video_url:
                    return pe.video_url
                pe_en = (VaccineEducationPatient.objects.using("masters")
                         .filter(vaccine=vaccine, is_active=True, language="en")
                         .order_by("rank")
                         .first())
                if pe_en and pe_en.video_url:
                    return pe_en.video_url
            except Exception:
                pass
            return default_url

        edu_primary = _education_url_for(primary)
        edu_secondary = _education_url_for(secondary)

    msg1 = build_vaccine_reminder_message(
        primary,
        child_name=child.full_name,
        vaccine_name=vaccine.name,
        due_date=due_date,
        education_url=edu_primary,
        doctor_name=doctor.full_name,
    )
    msg2 = build_vaccine_reminder_message(
        secondary,
        child_name=child.full_name,
        vaccine_name=vaccine.name,
        due_date=due_date,
        education_url=edu_secondary,
        doctor_name=doctor.full_name,
    )
    return f"{msg1}\n\n——————————\n\n{msg2}"

# --- New reminder status helpers ---

REMINDER_RETENTION_DAYS = 2  # D, D+1, D+2

def reminder_status_for_cd(cd, today: date) -> tuple[str, bool]:
    """
    Returns (status, eligible_for_whatsapp_button) using full due window logic.
    Status values:
      - 'given'             -> already given; never eligible
      - 'due'               -> inside due window (due_date <= today <= due_until_date or no until); eligible
      - 'overdue_today'     -> just past the due window (within retention days); eligible
      - 'upcoming_24h'      -> due within next 24h; eligible
      - 'future'            -> scheduled later than next 24h; not eligible
      - 'overdue_old'       -> overdue beyond retention window; not eligible
      - 'unscheduled'       -> no due_date; not eligible
    """
    if getattr(cd, "given_date", None):
        return "given", False
    due_date = getattr(cd, "due_date", None)
    if not due_date:
        return "unscheduled", False
    due_until = getattr(cd, "due_until_date", None)

    # Future cases
    if today < due_date:
        return ("upcoming_24h", True) if (due_date - today).days <= 1 else ("future", False)

    # Inside due window
    if due_until is None or today <= due_until:
        return "due", True

    # Past the window -> compute retention
    days_over = (today - due_until).days
    if 1 <= days_over <= REMINDER_RETENTION_DAYS:
        return "overdue_today", True
    # Make old overdue vaccines also eligible for reminders (important for health)
    return "overdue_old", True  # Changed from False to True

def reminder_status(due_date, due_until_date, given_date, now=None):
    """
    Map a ChildDose's dates to reminder status used by UI:
    - 'given'                 → given_date is set (no reminder needed)
    - 'upcoming_24h'          → due tomorrow/within next 24h
    - 'overdue_today'         → first calendar day after the due window
    - 'due'                   → due as on today (inside window)
    - 'future' / 'waiting'    → everything else (hidden in clinic list, shown in child page)
    """
    if given_date:
        return "given"

    t = (now or timezone.now()).date()
    if not due_date:
        return "waiting"

    if t < due_date:
        return "upcoming_24h" if due_date <= t + timedelta(days=1) else "future"

    # We're at or past due_date
    if (due_until_date is None and t >= due_date) or (due_until_date and due_date <= t <= due_until_date):
        return "due"

    # t is after the due window → figure out if it just turned overdue today
    overdue_since = (due_until_date or due_date) + timedelta(days=1)
    return "overdue_today" if overdue_since == t else "overdue"

def overdue_today_with_retention_q(t):
    """
    Django Q() filter for: items that are overdue 'today' with 3‑day retention window.
    i.e., became overdue today or in the last 2 days.
    """
    from django.db.models import Q
    from datetime import timedelta
    return (Q(due_until_date__lt=t, due_until_date__gte=t - timedelta(days=2)) |
            Q(due_until_date__isnull=True, due_date__lt=t, due_date__gte=t - timedelta(days=2)))
