from __future__ import annotations
from datetime import date, timedelta, datetime
from encodings.punycode import digits
from typing import List, Dict
from .utils_i18n import ui_lang_for_parent
from django.contrib import messages
from django.db.models import QuerySet, Prefetch
from django.db.models.functions import Length, Substr
from django.shortcuts import redirect, render, get_object_or_404
from django.views import View
from django.http import HttpResponseForbidden, HttpResponseRedirect, HttpResponseBadRequest
from django.utils.http import urlencode
from django.urls import reverse
from django.conf import settings
from django.utils import timezone
import base64, json, secrets, urllib.parse, urllib.request
from .utils_i18n import t
from .forms import AddChildForm, WhatsAppLookupForm, VerifyWhatsAppForm
from .crypto import hmac_sha256
from .models import Parent, Child, ChildDose, VaccineDose, OAuthState, Doctor, ChildShareLink, Vaccine, Clinic
from .utils import today, build_whatsapp_url, build_whatsapp_web_url, choose_two_languages_for_state, build_patient_message, classify_reminder_status, build_bilingual_vaccine_message, reminder_status, overdue_today_with_retention_q, reminder_status_for_cd
from .services import reanchor_dependents, current_schedule, get_patient_videos
from vaccinations.utils_schedule import build_series_prev_maps
from vaccinations.utils import today
from .utils_schedule import clinical_display_label
from .utils import last10_digits, phone_hash, normalize_msisdn
# -----------------------
# Helpers
# -----------------------

def _require_doctor_auth(request, token: str):
    auth = request.session.get("doctor_auth") or {}
    return auth.get("token") == token

# OAuth constants and helpers
OAUTH_STASH_KEY = "oauth_state"         # session key to keep state across redirect
DOC_AUTH_SESSION_KEY = "doctor_oauth"   # session key once logged in

def _urlencode(params: dict) -> str:
    return urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

def _build_google_auth_url(state: str, redirect_uri: str, force_choose: bool = False) -> str:
    scopes = " ".join(settings.GOOGLE_OAUTH["SCOPES"])
    q = {
        "client_id": settings.GOOGLE_OAUTH["CLIENT_ID"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "state": state,
        "access_type": "offline",
        "include_granted_scopes": "true",
    }
    # Force the account chooser if we want the user to switch accounts
    if force_choose:
        q["prompt"] = "select_account"
    return f'{settings.GOOGLE_OAUTH["AUTH_URL"]}?{_urlencode(q)}'

def _safe_state() -> str:
    # URL-safe (letters, digits, '-' and '_') – avoids odd chars that broke matching earlier
    return secrets.token_urlsafe(32)

def _stash_oauth(request, *, state: str, token: str, next_url: str, redirect_uri: str):
    request.session[OAUTH_STASH_KEY] = {
        "state": state,
        "doctor_token": token,
        "next": next_url,
        "redirect_uri": redirect_uri,
        "ts": timezone.now().isoformat(),
    }

def _pop_stash(request) -> dict | None:
    data = request.session.get(OAUTH_STASH_KEY)
    # do NOT delete here; only clear after we've used it successfully
    return data

def _clear_stash(request):
    request.session.pop(OAUTH_STASH_KEY, None)

def _exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    payload = _urlencode({
        "code": code,
        "client_id": settings.GOOGLE_OAUTH["CLIENT_ID"],
        "client_secret": settings.GOOGLE_OAUTH["CLIENT_SECRET"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(
        settings.GOOGLE_OAUTH["TOKEN_URL"],
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _fetch_userinfo(access_token: str) -> dict:
    req = urllib.request.Request(
        f'{settings.GOOGLE_OAUTH["USERINFO_URL"]}',
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

SESSION_PARENT_KEY = "parent_id"                 # primary parent id
SESSION_PARENT_IDS = "parent_ids"                # all equivalent parents' ids (same phone last-10)
SESSION_ANCHORED_ONCE = "anchored_child_dose_ids"

def require_parent_session(request):
    pid = request.session.get(SESSION_PARENT_KEY)
    if not pid:
        return None
    try:
        return Parent.objects.using("patients").get(pk=pid)
    except Parent.DoesNotExist:
        return None

def _digits_only(raw: str) -> str:
    """
    Return a string containing only the digits from the input string.
    """
    return "".join(ch for ch in (raw or "") if ch.isdigit())

def _equivalent_parents_by_input(wa_input: str) -> QuerySet:
    """
    Return ALL Parent rows that share the same last-10 digits with wa_input.
    Uses whatsapp_hash (SHA-256 hash of last-10). No DB substring ops on encrypted data.
    """
    if not wa_input:
        return Parent.objects.using("patients").none()
    
    return Parent.objects.using("patients").filter(whatsapp_hash=Parent.hash_for(wa_input)).order_by("id")

def _set_parent_session(request, primary_parent: Parent, equivalent_parents: QuerySet):
    ids = list(equivalent_parents.values_list("id", flat=True))
    if primary_parent.id not in ids:
        ids.insert(0, primary_parent.id)
    request.session[SESSION_PARENT_KEY] = primary_parent.id
    request.session[SESSION_PARENT_IDS] = ids

def _route_to_child_list(request, parents_qs: QuerySet):
    """
    Render the selection page for ALL children of the equivalent parents.
    """
    if not parents_qs.exists():
        messages.error(request, "No records found for that WhatsApp number. Please add a record.")
        return render(request, "vaccinations/update_lookup.html", {"form": WhatsAppLookupForm()})

    primary = parents_qs.first()  # pick a stable "primary" (oldest id)
    _set_parent_session(request, primary, parents_qs)

    child_qs = Child.objects.using("patients").filter(parent_id__in=list(parents_qs.values_list("id", flat=True)))
    children = list(child_qs.order_by("full_name", "date_of_birth"))
    if not children:
        messages.error(request, "No children found for this WhatsApp number. Please add a record.")
        return render(
            request,
            "vaccinations/update_lookup.html",
            {"form": WhatsAppLookupForm(initial={"parent_whatsapp": primary.whatsapp_e164})},
        )

    return render(
        request,
        "vaccinations/select_child.html",
        {"parent": primary, "children": children},
    )

def _compute_ui_state(child, cds, show_all: bool, newly_anchored_ids: set = None):
    t = today()
    newly_anchored_ids = newly_anchored_ids or set()
    
    # Create lookup for child doses by dose ID
    cd_by_dose_id = {cd.dose_id: cd for cd in cds}

    rows = []
    for cd in cds:
        # Use actual previous_dose relationship for IAP schedule logic
        prev_required = cd.dose.previous_dose_id is not None
        prev_cd = cd_by_dose_id.get(cd.dose.previous_dose_id) if prev_required else None
        prev_given = prev_cd.given_date if prev_cd else None

        # IAP Schedule Status Logic
        if cd.given_date:
            # Administered vaccines: Show "Given on <Date>" in green
            state = "given"
        elif prev_required and not prev_given:
            # Multi-dose/Booster vaccines waiting for previous dose
            state = "waiting-previous"
        elif cd.due_date is None:
            # No due date calculated yet (should not happen with proper IAP schedule)
            state = "waiting"
        elif cd.due_date <= t:
            # Due or overdue vaccines: Show "Due on <Date>" in red
            state = "due" if (cd.due_until_date is None or t <= cd.due_until_date) else "overdue"
        else:
            # Future vaccines: Show "Due on <Date>" (not red, but scheduled)
            state = "future"

        # Complete IAP Schedule Display: Show all vaccines always
        # Only filter for "Show Due Only" view (when show_all=False)
        if not show_all:
            # For "Show Due Only" view, show: given, due, overdue ONLY
            # Filter out: waiting, waiting-previous, and future vaccines
            if state in ("waiting", "waiting-previous"):
                continue
            elif state == "future" and cd.id not in newly_anchored_ids:
                continue

        # Editable only for due/overdue vaccines that are not given
        editable = (state in ("due", "overdue")) and (cd.given_date is None)

        # Determine display label for previous dose requirement
        prev_label = ""
        if prev_required and prev_cd:
            try:
                prev_label = f"{prev_cd.dose.vaccine.code} - {prev_cd.dose.dose_label}"
            except Exception:
                # Fallback if cross-database relationship fails
                prev_label = "previous dose"

        rows.append({
            "child_dose": cd,
            "vaccine_name": cd.dose.vaccine.name,
            "dose_label": cd.dose.dose_label,
            "status": state,
            "editable": editable,
            "due_display": cd.due_date,
            "prev_label": prev_label,
            "is_newly_anchored": cd.id in newly_anchored_ids,
            "vaccine_code": cd.dose.vaccine.code,  # Add for easier identification
        })

    # Sort by IAP schedule order, keeping vaccine series together
    def get_sort_key(row):
        cd = row["child_dose"]
        vaccine_code = cd.dose.vaccine.code.lower()
        min_offset = cd.dose.min_offset_days or 0
        
        # Special handling to keep vaccine series together
        if 'dtwp' in vaccine_code or 'dtap' in vaccine_code:
            # DTaP series: use base offset + small increment for sequence
            base_offset = 42  # DTaP1 starts at 6 weeks (42 days)
            if 'dtwp_dtap1' in vaccine_code:
                return (base_offset, 0)
            elif 'dtwp_dtap2' in vaccine_code:
                return (base_offset, 1)
            elif 'dtwp_dtap3' in vaccine_code:
                return (base_offset, 2)
            else:
                return (min_offset, 0)
        elif 'hep_b' in vaccine_code:
            # Hep B series
            if 'hep_b1' in vaccine_code:
                return (0, 1)  # Birth vaccine
            elif 'hep_b2' in vaccine_code:
                return (42, 1)  # 6 weeks
            elif 'hep_b3' in vaccine_code:
                return (70, 1)  # 10 weeks
            elif 'hep_b4' in vaccine_code:
                return (98, 1)  # 14 weeks
            else:
                return (min_offset, 1)
        elif 'hib' in vaccine_code:
            # Hib series: keep together
            return (42 if 'hib-1' in vaccine_code else min_offset, 2)
        elif 'ipv' in vaccine_code:
            # IPV series: keep together
            return (42 if 'ipv-1' in vaccine_code else min_offset, 3)
        elif 'pcv' in vaccine_code:
            # PCV series: keep together
            return (42 if 'pcv_1' in vaccine_code else min_offset, 4)
        elif 'rota' in vaccine_code:
            # Rota series: keep together
            return (42 if 'rota-1' in vaccine_code else min_offset, 5)
        elif 'influenza' in vaccine_code or 'annual' in vaccine_code:
            # Influenza series: keep together
            return (180 if 'influenza-1' in vaccine_code else min_offset, 6)
        elif 'bcg' in vaccine_code:
            # BCG: birth vaccine, should be first
            return (0, 0)
        else:
            # All other vaccines: use their natural offset
            return (min_offset, 10)
    
    rows.sort(key=get_sort_key)
    return rows
# -----------------------
# Pages (Add & Update entrypoints)
# -----------------------

class HomeView(View):
    def get(self, request):
        return render(request, "vaccinations/home.html")

class AddRecordView(View):
    def get(self, request):
        return render(request, "vaccinations/add_record.html",
                      {"form": AddChildForm(), "show_send_button": False})

    def post(self, request):
        form = AddChildForm(request.POST)
        if not form.is_valid():
            return render(request, "vaccinations/add_record.html",
                          {"form": form, "show_send_button": False})

        wa_input = form.cleaned_data["parent_whatsapp"]

        # Reuse existing parent with the same last-10 digits if found; otherwise create new
        parents_qs = _equivalent_parents_by_input(wa_input)
        if parents_qs.exists():
            parent = parents_qs.first()
        else:
            # Use hash-aware get/create with transaction to handle race conditions
            from django.db import IntegrityError, transaction
            try:
                with transaction.atomic(using="patients"):
                    parent = Parent.objects.using("patients").create()
                    parent.whatsapp_e164 = wa_input  # sets enc + hash via setter
                    parent.save(using="patients")
            except IntegrityError:
                # Handle rare race: someone else inserted the same number simultaneously
                # Re-query to find the parent that was created by another process
                parents_qs = _equivalent_parents_by_input(wa_input)
                parent = parents_qs.first()
                if not parent:
                    # If still not found, something else went wrong, re-raise
                    raise
            parents_qs = Parent.objects.using("patients").filter(pk=parent.pk)

        # Use the legacy field names that still back the live model.
        child, created = Child.objects.using("patients").get_or_create(
            parent=parent,
            full_name=form.cleaned_data["child_name"],
            date_of_birth=form.cleaned_data["date_of_birth"],
            defaults={
                "sex": form.cleaned_data["gender"],
                "state": form.cleaned_data["state"],
            }
        )

        _set_parent_session(request, parent, parents_qs)
        if created:
            messages.success(request, "Record added successfully.")
        else:
            messages.info(request, "Record already exists.")
        return redirect("vaccinations:card", child_id=child.id)

class UpdateLookupView(View):
    """
    GET:
      - If parent in session -> rebuild equivalence by last-10 and show the list.
      - Else -> show form (or accept ?wa= to resolve directly).
    POST:
      - Resolve by last-10 and show the list.
    """
    def get(self, request):
        parent = require_parent_session(request)
        if parent:
            parents_qs = _equivalent_parents_by_input(parent.whatsapp_e164)
            return _route_to_child_list(request, parents_qs)

        wa = request.GET.get("wa")
        if wa:
            parents_qs = _equivalent_parents_by_input(wa)
            return _route_to_child_list(request, parents_qs)

        return render(request, "vaccinations/update_lookup.html", {"form": WhatsAppLookupForm()})

    def post(self, request):
        form = WhatsAppLookupForm(request.POST)
        if not form.is_valid():
            return render(request, "vaccinations/update_lookup.html", {"form": form})
        wa = form.cleaned_data["parent_whatsapp"]
        parents_qs = _equivalent_parents_by_input(wa)
        return _route_to_child_list(request, parents_qs)

class ParentShareStartView(View):
    """
    GET: Show verify screen for a share token.
    POST: Verify last-10 digits and route to child's card (due-only view).
    """
    template_name = "vaccinations/parent_share_verify.html"

    def get(self, request, token: str):
        link = ChildShareLink.objects.using("patients").filter(token=token, is_active=True).first()
        if not link or not link.still_valid():
            messages.error(request, "This link is invalid or has expired.")
            return redirect("vaccinations:update")
        child = link.child
        return render(request, self.template_name, {
            "form": VerifyWhatsAppForm(),
            "child": child,
        })

    def post(self, request, token: str):
        link = ChildShareLink.objects.using("patients").filter(token=token, is_active=True).select_related("child__parent").first()
        if not link or not link.still_valid():
            messages.error(request, "This link is invalid or has expired.")
            return redirect("vaccinations:update")

        form = VerifyWhatsAppForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "child": link.child})

        if not link.matches(form.cleaned_data["whatsapp"]):
            messages.error(request, "Please enter the correct WhatsApp number")
            return render(request, self.template_name, {"form": form, "child": link.child})

        # Success -> authorize this parent session for this child's parent (and equivalents)
        parent = link.child.parent
        parents_qs = _equivalent_parents_by_input(parent.whatsapp_e164)
        _set_parent_session(request, parent, parents_qs)

        return redirect("vaccinations:card", child_id=link.child_id)

# -----------------------
# CARD VARIANT A: SHOW-ALL
# -----------------------
class ParentVaccineEducationView(View):
    """
    Shows all patient education videos for a vaccine (YouTube URLs),
    prioritizing the parent's preferred language but listing all.
    Requires a valid parent session (same gate you use for cards).
    """
    template_name = "vaccinations/parent_edu.html"

    def get(self, request, vaccine_id: int):
        parent = require_parent_session(request)
        if not parent:
            messages.error(request, "Please verify your WhatsApp number to access this page.")
            return redirect("vaccinations:update")

        # Mapping from dose-specific vaccines (default DB) to generic vaccines (masters DB)
        vaccine_mapping = {
            'bcg': 'bcg', 'hep_b1': 'hep-b', 'hep_b2': 'hep-b', 'hep_b3': 'hep-b', 'hep_b4': 'hep-b',
            'opv': 'opv', 'dtwp_dtap1': 'dtwp-dtap', 'dtwp_dtap2': 'dtwp-dtap', 'dtwp_dtap3': 'dtwp-dtap', 'dtwp_dtap': 'dtwp-dtap',
            'hib-1': 'hib', 'hib-2': 'hib', 'hib-3': 'hib', 'hib': 'hib',
            'ipv-1': 'ipv', 'ipv-2': 'ipv', 'ipv-3': 'ipv', 'ipv': 'ipv',
            'pcv_1': 'pcv', 'pcv_2': 'pcv', 'pcv_3': 'pcv', 'pcv_booster': 'pcv',
            'rota-1': 'rota', 'rota-2': 'rota', 'rota-3': 'rota-3',
            'influenza-1': 'influenza', 'influenza-2': 'influenza',
            'typhoid_conjugate_vaccine': 'typhoid-conjugate-vaccine',
            'mmr_1_measles_mumps_rubella': 'mmr', 'mmr_2': 'mmr', 'mmr_3': 'mmr',
            'hepatitis_a-1': 'hepatitis-a', 'hepatitis_a-2': 'hepatitis-a-2',
            'varicella': 'varicella', 'varicella_2': 'varicella',
            'hpv_2_doses': 'hpv', 'tdap_td': 'tdap-td', 'annual_influenza_vaccine': 'annual-influenza-vaccine',
        }
        
        # Try masters database first, then default database as fallback
        try:
            vaccine = Vaccine.objects.using("masters").select_related("schedule_version").get(pk=vaccine_id)
        except Vaccine.DoesNotExist:
            # If not found in masters, get from default and find equivalent in masters
            default_vaccine = get_object_or_404(Vaccine.objects.using("default").select_related("schedule_version"), pk=vaccine_id)
            
            # Try direct code match first
            try:
                vaccine = Vaccine.objects.using("masters").select_related("schedule_version").get(code=default_vaccine.code)
            except Vaccine.DoesNotExist:
                # Use mapping to find generic vaccine
                masters_code = vaccine_mapping.get(default_vaccine.code)
                if masters_code:
                    try:
                        vaccine = Vaccine.objects.using("masters").select_related("schedule_version").get(code=masters_code)
                    except Vaccine.DoesNotExist:
                        vaccine = default_vaccine
                else:
                    vaccine = default_vaccine
        # If link was clicked from "history", we know the child via ?child=<id>
        child_id = request.GET.get("child")
        if child_id:
            child = get_object_or_404(
                Child.objects.using("patients").select_related("parent"),
                pk=child_id,
            )
            if child.clinic_id:
                try:
                    child.clinic = Clinic.objects.using("masters").only("id", "name", "phone", "whatsapp_e164").get(pk=child.clinic_id)
                except Clinic.DoesNotExist:
                    child.clinic = None
        else:
            child = None
        # Build language preference
        pref = []
        if child:
            primary = ui_lang_for_parent(parent, child)
            pref = [primary, "hi", "en"] if primary != "en" else ["en", "hi"]
        videos = get_patient_videos(vaccine, pref)  # returns active videos ordered by preference

        lang = pref[0] if pref else "en"
        ctx = {
            "parent": parent,
            "child": child,
            "vaccine": vaccine,
            "videos": videos,
            "title": f"{vaccine.name} — " + t(lang, "edu.title", "Education"),
            "lead": t(lang, "edu.lead", "Watch the short video(s) below to learn why this vaccine is important."),
        }
        return render(request, self.template_name, ctx)

from .utils_schedule import clinical_display_label  # if you added this helper
from .utils import today

class VaccinationHistoryView(View):
    """
    After a parent edits dates on the card, they land here.
    Shows succinct history + actions. Fully localized with UiString translations.
    """
    template_name = "vaccinations/parent_history.html"

    def _get_child(self, request, child_id: int) -> Child | None:
        parent = require_parent_session(request)
        if not parent:
            messages.error(request, "Please verify your WhatsApp number to access this record.")
            return None
        child = get_object_or_404(
            Child.objects.using("patients")
            .select_related("parent")
            .prefetch_related(
                Prefetch("clinic", queryset=Clinic.objects.using("masters").only("id","name","state","phone"))
            ),

            pk=child_id
        )
        allowed_ids = request.session.get(SESSION_PARENT_IDS, [parent.id])
        if child.parent_id not in allowed_ids:
            messages.error(request, "This record doesn't belong to your WhatsApp number.")
            return None
        return child

    def get(self, request, child_id: int):
        child = self._get_child(request, child_id)
        if not child:
            return redirect("vaccinations:update")
        parent = child.parent
        lang = ui_lang_for_parent(parent, child)

        # Use direct ChildDose query instead of child.doses to avoid cross-database issues
        # Get child doses (without problematic cross-DB prefetch)
        cds = list(
            ChildDose.objects.using("patients")
            .filter(child=child)
        )
        
        # Load dose and vaccine data separately to avoid cross-DB issues
        for cd in cds:
            try:
                dose = VaccineDose.objects.using("default").select_related("previous_dose", "vaccine").get(pk=cd.dose_id)
                cd.dose = dose  # Attach the full dose with vaccine
            except Exception:
                pass  # Skip if dose/vaccine loading fails
        
        # Sort in Python after fetching to avoid cross-database order_by
        # Sort by min_offset_days (birth vaccines first), then by vaccine name for same day
        # Use 9999 only if min_offset_days is None, not if it's 0
        cds.sort(key=lambda cd: (cd.dose.min_offset_days if cd.dose.min_offset_days is not None else 9999, cd.dose.vaccine.name, cd.id))

        t0 = today()
        rows = []
        for cd in cds:
            # status
            if cd.given_date:
                status_key = "given"
                status_text = f"Given on : {cd.given_date:%d %b %Y}"
            elif cd.due_date is None or (cd.dose.previous_dose_id and
                                         not (cd.dose.previous_dose and
                                              child.doses.filter(dose=cd.dose.previous_dose, given_date__isnull=False).exists())):
                status_key = "waiting"
                status_text = "Previous dose pending"
            else:
                if t0 < cd.due_date:
                    status_key = "future"
                    status_text = f"Due on : {cd.due_date:%d %b %Y}"
                else:
                    status_key = "due"
                    status_text = f"Due on : {cd.due_date:%d %b %Y}"

            rows.append({
                "vaccine_id": cd.dose.vaccine_id,                      # <— used by {% url ... row.vaccine_id %}
                "vaccine_name": clinical_display_label(cd.dose),       # or cd.dose.vaccine.name
                "vaccine_subtitle": getattr(cd.dose, "dose_label", ""),
                "dose_label": cd.dose.dose_label,
                "status_key": status_key,
                "status": status_text,
                "cd": cd,
                "child_dose": cd,
                "due_display": cd.due_date,
                "can_call": bool(getattr(child.clinic, "phone", "")),
            })

        ctx = {
            "child": child, "parent": parent, "lang": lang, "rows": rows, "today": t0,
            "ui": {
                "title": t(lang, "history.title", "Vaccination History"),
                "title_history": t(lang, "history.heading", "Vaccine History"),
                "note": t(lang, "history.note",
                          'If this is your first visit, click "Update Due Date" to enter the doses already given. '
                          'Once done, only pending vaccines will remain.'),
                "update_due": t(lang, "btn.update_due", "Update Due Date"),
                "add_child": t(lang, "btn.add_child", "Add Another Child"),
                "back": t(lang, "btn.back", "Go Back"),
                "th_vaccine": t(lang, "th.vaccine", "Vaccine"),
                "th_status": t(lang, "th.status", "Status"),
                "th_action": t(lang, "th.action", "Action"),
                "call_clinic": t(lang, "btn.call_clinic", "Call Clinic"),
                "given_on": t(lang, "lbl.given_on", "Given on"),
                "due_on": t(lang, "lbl.due_on", "Due on"),
                "learn_more": t(lang, "lbl.learn_more", "Learn more"),
                "child_name_label": t(lang, "csv.child_name", "Child Name"),
                "child_dob_label": t(lang, "csv.date_of_birth", "Date of Birth"),
                "child_gender_label": t(lang, "csv.gender", "Gender"),
            },
            "t": {
                "vaccine_name": t(lang, "th.vaccine", "Vaccine"),
                "status": t(lang, "th.status", "Status"),
                "action": t(lang, "th.action", "Action"),
                "call_clinic": t(lang, "btn.call_clinic", "Call Clinic"),
            }
        }
        return render(request, self.template_name, ctx)
class VaccinationHistoryCSVExportView(View):
    """
    Export child vaccination history as CSV with child details (name, DOB, gender)
    """
    
    def get(self, request, child_id: int):
        child = self._get_child(request, child_id)
        if not child:
            return redirect("vaccinations:update")
        
        parent = child.parent
        lang = ui_lang_for_parent(parent, child)
        
        # Create CSV response
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="vaccination_history_{child.full_name}_{datetime.now().strftime("%Y%m%d")}.csv"'
        
        writer = csv.writer(response)
        
        # CSV Headers with child information
        writer.writerow([
            t(lang, "csv.child_name", "Child Name"),
            t(lang, "csv.date_of_birth", "Date of Birth"), 
            t(lang, "csv.gender", "Gender"),
            t(lang, "csv.vaccine", "Vaccine"),
            t(lang, "csv.dose", "Dose"),
            t(lang, "csv.status", "Status"),
            t(lang, "csv.date", "Date"),
            t(lang, "csv.clinic_phone", "Clinic Phone")
        ])
        
        # Get vaccination data
        # Get child doses (without problematic cross-DB prefetch)
        cds = list(
            child.doses.using("patients")
            .order_by("dose_id")  # Simple ordering by dose_id
        )
        
        # Load dose and vaccine data separately
        for cd in cds:
            try:
                dose = VaccineDose.objects.using("default").select_related("vaccine").get(pk=cd.dose_id)
                cd.dose = dose
            except Exception:
                pass
        
        for cd in cds:
            v = cd.dose.vaccine
            given = bool(cd.given_date)
            status_text = t(lang, "status.given", "Given") if given else t(lang,"status.pending","Pending")
            date_value = cd.given_date if given else cd.due_date
            
            writer.writerow([
                child.get_child_name(),
                child.get_date_of_birth_encrypted().strftime("%Y-%m-%d") if child.get_date_of_birth_encrypted() else "",
                child.get_gender_display(),
                v.name,
                cd.dose.dose_label,
                status_text,
                date_value.strftime("%Y-%m-%d") if date_value else "",
                getattr(child.clinic, "phone", "") or ""
            ])
        
        return response
    
    def _get_child(self, request, child_id: int) -> Child | None:
        parent = require_parent_session(request)
        if not parent:
            messages.error(request, "Please verify your WhatsApp number to access this record.")
            return None
        child = get_object_or_404(
            Child.objects.using("patients")
            .select_related("parent")
            .prefetch_related(
                Prefetch("clinic", queryset=Clinic.objects.using("masters").only("id","name","state","phone"))
            ),
            pk=child_id
        )
        allowed_ids = request.session.get(SESSION_PARENT_IDS, [parent.id])
        if child.parent_id not in allowed_ids:
            messages.error(request, "This record doesn't belong to your WhatsApp number.")
            return None
        return child
    
class VaccinationCardAllView(View):
    """
    Shows ALL doses (past, today, future).
    Editable only for due/overdue (not future / waiting-previous / given).
    """
    def get_child_and_check(self, request, child_id: int) -> Child:
        parent = require_parent_session(request)
        child = get_object_or_404(Child.objects.using("patients"), pk=child_id)
        if not parent:
            messages.error(request, "Please verify your WhatsApp number to access this record.")
            return None
        allowed_ids = request.session.get(SESSION_PARENT_IDS, [parent.id])
        if child.parent_id not in allowed_ids:
            messages.error(request, "This record doesn't belong to your WhatsApp number.")
            return None
        return child

    def get(self, request, child_id: int):
        child = self.get_child_and_check(request, child_id)
        if not child:
            return redirect("vaccinations:update")

        # Get child doses (without select_related which breaks cross-DB queries)
        cds = list(
            ChildDose.objects.using("patients")
            .filter(child=child)
        )
        
        # Load dose and vaccine data separately to avoid cross-DB issues
        for cd in cds:
            try:
                # Load dose from default database
                dose = VaccineDose.objects.using("default").select_related("vaccine").get(pk=cd.dose_id)
                cd.dose = dose  # Attach the full dose with vaccine
            except Exception:
                pass  # Skip if dose/vaccine loading fails
                
        rows = _compute_ui_state(child, cds, show_all=True)
        return render(request, "vaccinations/card.html", {"child": child, "rows": rows, "today": today(), "show_all": True})

    def post(self, request, child_id: int):
        child = self.get_child_and_check(request, child_id)
        if not child:
            return redirect("vaccinations:update")

        t = today()
        # Get editable doses (without select_related which breaks cross-DB queries)
        editable_qs = (
            child.doses.using("patients")
            .filter(due_date__isnull=False, due_date__lte=t, given_date__isnull=True)
        )

        changed = 0
        changed_bases: List[ChildDose] = []
        for cd in editable_qs:
            raw = (request.POST.get(f"dose_{cd.id}", "") or "").strip()
            if not raw:
                continue
            try:
                given = date.fromisoformat(raw)
            except Exception:
                messages.error(request, f"Invalid date for {cd.dose.dose_label}.")
                continue
            if given > t:
                messages.error(request, f"{cd.dose.dose_label} cannot be a future date.")
                continue

            cd.given_date = given
            cd.save(using="patients", update_fields=["given_date", "updated_at"])
            changed += 1
            changed_bases.append(cd)

        newly_anchored = reanchor_dependents(child, changed_bases)

        if newly_anchored:
            txt = "; ".join([f"{x.dose.vaccine.name} — {x.dose.dose_label}: {x.due_date}" for x in newly_anchored])
            messages.success(request, f"Updated. Newly anchored next doses: {txt}")
        elif changed:
            messages.success(request, "Vaccination record updated.")
        else:
            messages.info(request, "No changes were made.")

        if changed > 0:
            return redirect("vaccinations:history", child_id=child.id)

        if request.GET.get("next") == "history" or request.POST.get("next") == "history":
            return redirect("vaccinations:history", child_id=child.id)

        return redirect("vaccinations:card-all", child_id=child.id)

# -----------------------
# CARD VARIANT B: DUE-ONLY (past + today)
# -----------------------

class VaccinationCardDueView(View):
    """
    Shows only doses with due_date <= today (and given ones with such due dates).
    Plus newly anchored doses even if they are future.
    Editable only for due/overdue (not future / waiting-previous / given).
    """
    def get_child_and_check(self, request, child_id: int) -> Child:
        parent = require_parent_session(request)
        child = get_object_or_404(Child.objects.using("patients"), pk=child_id)
        if not parent:
            messages.error(request, "Please verify your WhatsApp number to access this record.")
            return None
        allowed_ids = request.session.get(SESSION_PARENT_IDS, [parent.id])
        if child.parent_id not in allowed_ids:
            messages.error(request, "This record doesn't belong to your WhatsApp number.")
            return None
        return child

    def get(self, request, child_id: int):
        child = self.get_child_and_check(request, child_id)
        if not child:
            return redirect("vaccinations:update")

        t = today()
        # Get child doses (without select_related which breaks cross-DB queries)
        cds_all = list(
            ChildDose.objects.using("patients")
            .filter(child=child)
        )
        
        # Load dose and vaccine data separately to avoid cross-DB issues
        for cd in cds_all:
            try:
                # Load dose from default database
                dose = VaccineDose.objects.using("default").select_related("vaccine").get(pk=cd.dose_id)
                cd.dose = dose  # Attach the full dose with vaccine
            except Exception:
                pass  # Skip if dose/vaccine loading fails

        anchored_ids = set(request.session.pop(SESSION_ANCHORED_ONCE, []) or [])
        rows = _compute_ui_state(child, cds_all, show_all=True, newly_anchored_ids=anchored_ids)
        
        # Filter to show only vaccines that are actually due or given (no waiting vaccines)
        today_date = t
        filtered_rows = []
        for row in rows:
            cd = row["child_dose"]
            state = row["status"]
            
            # Always show given vaccines
            if state == "given":
                filtered_rows.append(row)
            # Show due/overdue vaccines (actual dates that have arrived)
            elif state in ("due", "overdue"):
                filtered_rows.append(row)
            # DO NOT show waiting or waiting-previous vaccines
            # They will appear automatically when their primary dose is given and date is calculated
        
        rows = filtered_rows

        if anchored_ids:
            newly_anchored_rows = [r for r in rows if r.get("is_newly_anchored", False)]
            if newly_anchored_rows:
                txt = "; ".join([f"{r['vaccine_name']} — {r['dose_label']}: {r['due_display']}" for r in newly_anchored_rows])
                messages.success(request, f"Next dose(s) automatically scheduled: {txt}")

        return render(request, "vaccinations/card.html", {"child": child, "rows": rows, "today": t, "show_all": False})

    def post(self, request, child_id: int):
        child = self.get_child_and_check(request, child_id)
        if not child:
            return redirect("vaccinations:update")

        t = today()
        # Get editable doses (without select_related which breaks cross-DB queries)
        editable_qs = (ChildDose.objects.using("patients")
                       .filter(child=child, due_date__isnull=False, due_date__lte=t, given_date__isnull=True))

        changed = 0
        changed_bases: List[ChildDose] = []
        for cd in editable_qs:
            raw = (request.POST.get(f"dose_{cd.id}", "") or "").strip()
            if not raw:
                continue
            try:
                given = date.fromisoformat(raw)
            except Exception:
                messages.error(request, f"Invalid date for {cd.dose.dose_label}.")
                continue
            if given > t:
                messages.error(request, f"{cd.dose.dose_label} cannot be a future date.")
                continue

            cd.given_date = given
            cd.save(using="patients", update_fields=["given_date", "updated_at"])
            changed += 1
            changed_bases.append(cd)

        newly_anchored = reanchor_dependents(child, changed_bases)
        if newly_anchored:
            request.session[SESSION_ANCHORED_ONCE] = [cd.id for cd in newly_anchored]

        if changed and not newly_anchored:
            messages.success(request, "Vaccination record updated.")
        elif not changed:
            messages.info(request, "No changes were made.")

        if changed > 0:
            return redirect("vaccinations:history", child_id=child.id)

        if request.GET.get("next") == "history" or request.POST.get("next") == "history":
            return redirect("vaccinations:history", child_id=child.id)

        return redirect("vaccinations:card", child_id=child.id)
# -----------------------
# Optional JSON endpoint (unchanged)
# -----------------------

from rest_framework.generics import RetrieveAPIView
from rest_framework.permissions import AllowAny
from .serializers import ChildCardSerializer

class ChildCardAPI(RetrieveAPIView):
    queryset = (Child.objects.select_related("parent")
                .prefetch_related("doses__dose__vaccine"))
    permission_classes = [AllowAny]
    serializer_class = ChildCardSerializer
    lookup_field = "pk"

# --- Phase 2 views ---
from django.contrib.auth.decorators import user_passes_test
from django.utils.decorators import method_decorator
from django.http import HttpResponseForbidden
from django.core.files.uploadedfile import UploadedFile
import csv

from .models import Partner, FieldRepresentative, Doctor, Clinic
from .forms import (
    DoctorRegistrationSelfForm, DoctorRegistrationPartnerForm, DoctorClinicProfileForm,
    AddChildForm, WhatsAppLookupForm
)
from .services import send_doctor_portal_link

# ---------- Partner publishing (admin/staff only) ----------
def staff_required(u): return u.is_active and u.is_staff

class PartnerCreateUploadView(View):
    template_name = "vaccinations/partners_create_upload.html"

    def dispatch(self, request, *args, **kwargs):
        is_staff = getattr(request.user, "is_active", False) and getattr(request.user, "is_staff", False)
        if not (is_staff or request.session.get("admin_ok")):
            messages.error(request, "Admin access required.")
            return redirect("vaccinations:admin-access")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return render(request, self.template_name, {"created": False})

    def post(self, request):
        name = (request.POST.get("partner_name", "") or "").strip()
        if not name:
            messages.error(request, "Partner name is required.")
            return render(request, self.template_name, {"created": False})

        partner = Partner.new(name)
        partner.save(using="masters")
        created_reps = 0

        f: UploadedFile | None = request.FILES.get("csv_file")
        if f and f.size:
            try:
                decoded = f.read().decode("utf-8-sig").splitlines()
                reader = csv.DictReader(decoded)
                for row in reader:
                    code = (row.get("rep_code") or "").strip()
                    full_name = (row.get("full_name") or "").strip()
                    if not code or not full_name:
                        continue
                    FieldRepresentative.objects.using("masters").update_or_create(
                        partner=partner, rep_code=code,
                        defaults={"full_name": full_name, "is_active": True}
                    )
                    created_reps += 1
            except Exception as ex:
                messages.error(request, f"Could not parse CSV: {ex}")
                return render(request, self.template_name, {"created": False})

        reg_link = request.build_absolute_uri(partner.doctor_registration_link)
        messages.success(request, f"Partner '{partner.name}' created with {created_reps} field reps.")
        return render(request, self.template_name, {
            "created": True,
            "partner": partner,
            "registration_link": reg_link,
        })

class AdminAccessView(View):
    template_name = "vaccinations/admin_access.html"

    def get(self, request):
        return render(request, self.template_name, {"error": ""})

    def post(self, request):
        pwd = (request.POST.get("password") or "").strip()
        expected = getattr(settings, "ADMIN_QUICK_PASSWORD", "")
        if not expected:
            expected = (getattr(settings, "SECRET_KEY", "") or "")[0:12]
        if pwd and expected and pwd == expected:
            request.session["admin_ok"] = True
            return redirect("vaccinations:partner-create")
        messages.error(request, "Invalid admin password.")
        return render(request, self.template_name, {"error": "Invalid password"})

# ---------- Doctor registration (self) ----------
class DoctorRegisterSelfView(View):
    template_name = "vaccinations/doctor_register.html"

    def get(self, request):
        return render(request, self.template_name, {"form": DoctorRegistrationSelfForm(), "mode": "self"})

    def post(self, request):
        form = DoctorRegistrationSelfForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "mode": "self"})

        clinic = Clinic.objects.create(
            name=form.cleaned_data["doctor_name"],
            address=form.cleaned_data.get("clinic_address",""),
            phone="",  # optional
            state=form.cleaned_data["state"],
            headquarters=form.cleaned_data.get("head_quarters",""),
            pincode=form.cleaned_data.get("pincode",""),
            whatsapp_e164=form.cleaned_data.get("clinic_whatsapp",""),
            receptionist_email=form.cleaned_data.get("receptionist_email",""),
        )
        clinic.set_languages(form.cleaned_data.get("preferred_languages", []))
        clinic.save()

        doctor = Doctor.objects.create(
            clinic=clinic,
            full_name=form.cleaned_data["doctor_name"],
            whatsapp_e164=form.cleaned_data["doctor_whatsapp"],
            email=form.cleaned_data["doctor_email"],
            imc_number=form.cleaned_data["imc_number"],
            photo=form.cleaned_data.get("doctor_photo"),
        )
        send_doctor_portal_link(doctor, request)
        
        # For self registration, also redirect to WhatsApp
        import re
        import urllib.parse
        from django.urls import reverse
        
        # Clean and format phone number
        doctor_whatsapp = form.cleaned_data["doctor_whatsapp"]
        phone_number = re.sub(r'[^\d+]', '', doctor_whatsapp)
        if not phone_number.startswith('+'):
            if phone_number.startswith('91'):
                phone_number = '+' + phone_number
            elif phone_number.startswith('0'):
                phone_number = '+91' + phone_number[1:]
            else:
                phone_number = '+91' + phone_number
        
        # Remove + for WhatsApp URL
        clean_number = phone_number.replace('+', '')
        
        # Create WhatsApp URL with pre-filled message including portal link
        portal_link = request.build_absolute_uri(reverse("vaccinations:doc-home", args=[doctor.portal_token]))
        message = f"""Hello Dr. {doctor.full_name},

Please find below the link for your personalized vaccination system under the aegis of South Asia Pediatric Association (SAPA).

This system is supported for your clinic by the Serum Institute of India.

You can now send timely vaccination reminders to your patients. You can also keep track of your patient's vaccinations.

Link: {portal_link}"""
        encoded_message = urllib.parse.quote(message)
        whatsapp_url = f"https://wa.me/{clean_number}?text={encoded_message}"
        
        # Redirect to WhatsApp
        return redirect(whatsapp_url)

# ---------- Doctor registration (partner link, with field rep) ----------
class DoctorRegisterPartnerView(View):
    template_name = "vaccinations/doctor_register.html"

    def dispatch(self, request, token: str, *args, **kwargs):
        self.partner = Partner.objects.filter(registration_token=token).first()
        if not self.partner:
            messages.error(request, "Invalid or expired partner link.")
            return redirect("vaccinations:home")
        return super().dispatch(request, token, *args, **kwargs)

    def get(self, request, token: str):
        form = DoctorRegistrationPartnerForm(partner=self.partner)
        return render(request, self.template_name, {"form": form, "mode": "partner", "partner": self.partner})

    def post(self, request, token: str):
        form = DoctorRegistrationPartnerForm(request.POST, request.FILES, partner=self.partner)
        if not form.is_valid():
            print(f"Form validation failed: {form.errors}")
            return render(request, self.template_name, {"form": form, "mode": "partner", "partner": self.partner})

        print("Form is valid, creating doctor...")
        clinic = Clinic.objects.create(
            name=form.cleaned_data["doctor_name"],
            address=form.cleaned_data.get("clinic_address",""),
            phone="",
            state=form.cleaned_data["state"],
            headquarters=form.cleaned_data.get("head_quarters",""),
            pincode=form.cleaned_data.get("pincode",""),
            whatsapp_e164=form.cleaned_data.get("clinic_whatsapp",""),
            receptionist_email=form.cleaned_data.get("receptionist_email",""),
        )
        clinic.set_languages(form.cleaned_data.get("preferred_languages", []))
        clinic.save()

        doctor = Doctor.objects.create(
            clinic=clinic,
            full_name=form.cleaned_data["doctor_name"],
            whatsapp_e164=form.cleaned_data["doctor_whatsapp"],
            email=form.cleaned_data["doctor_email"],
            imc_number=form.cleaned_data["imc_number"],
            photo=form.cleaned_data.get("doctor_photo"),
            partner=self.partner,
            field_rep=form.cleaned_data["__field_rep_obj"],
        )
        send_doctor_portal_link(doctor, request)
        
        # Store the doctor's WhatsApp number before creating new form
        doctor_whatsapp = form.cleaned_data["doctor_whatsapp"]
        print(f"Doctor created successfully. WhatsApp: {doctor_whatsapp}")
        
        # For partner registration, redirect to WhatsApp immediately
        import re
        import urllib.parse
        from django.urls import reverse
        
        # Clean and format phone number
        phone_number = re.sub(r'[^\d+]', '', doctor_whatsapp)
        if not phone_number.startswith('+'):
            if phone_number.startswith('91'):
                phone_number = '+' + phone_number
            elif phone_number.startswith('0'):
                phone_number = '+91' + phone_number[1:]
            else:
                phone_number = '+91' + phone_number
        
        # Remove + for WhatsApp URL
        clean_number = phone_number.replace('+', '')
        
        # Create WhatsApp URL with pre-filled message including portal link
        portal_link = request.build_absolute_uri(reverse("vaccinations:doc-home", args=[doctor.portal_token]))
        message = f"""Hello Dr. {doctor.full_name},

Please find below the link for your personalized vaccination system under the aegis of South Asia Pediatric Association (SAPA).

This system is supported for your clinic by the Serum Institute of India.

You can now send timely vaccination reminders to your patients. You can also keep track of your patient's vaccinations.

Link: {portal_link}"""
        encoded_message = urllib.parse.quote(message)
        whatsapp_url = f"https://wa.me/{clean_number}?text={encoded_message}"
        
        # Redirect to WhatsApp
        return redirect(whatsapp_url)

# --- Google OAuth + Doctor session gating ---
from django.conf import settings
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.http import HttpResponse, HttpResponseForbidden
import secrets, urllib.parse, requests
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from .models import Doctor, DoctorSessionToken

OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

def _next_or_default(next_url: str, token: str) -> str:
    # Default to doctor profile if next is missing
    if not next_url:
        return reverse("vaccinations:doc-profile", args=[token])
    # Only allow same-host redirects
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts=None):
        return next_url
    return reverse("vaccinations:doc-profile", args=[token])

def _set_doctor_cookie(response, token_value: str, max_age_seconds: int):
    response.set_cookie(
        settings.DOCTOR_AUTH_COOKIE_NAME,
        token_value,
        max_age=max_age_seconds,
        httponly=True,
        secure=settings.DOCTOR_AUTH_COOKIE_SECURE,
        samesite=settings.DOCTOR_AUTH_COOKIE_SAMESITE,
    )
    return response

class DoctorAuthRequiredMixin:
    """
    To use: subclass this mixin before View in every doctor-portal view.
    Requires the URL kwarg 'token' and a Doctor whose portal_token==token.
    """
    doctor: Doctor = None

    def dispatch(self, request, *args, **kwargs):
        token = kwargs.get("token")
        self.doctor = Doctor.objects.filter(portal_token=token).first()
        if not self.doctor:
            return HttpResponseForbidden("Invalid portal link.")
        # Check active session cookie
        raw = request.COOKIES.get(settings.DOCTOR_AUTH_COOKIE_NAME)
        if raw:
            sess = (DoctorSessionToken.objects
                    .filter(token=raw, doctor=self.doctor, revoked=False, expires_at__gt=timezone.now())
                    .first())
            if sess and sess.matches_request(request):
                # OK – extend sliding expiry
                sess.expires_at = timezone.now() + timezone.timedelta(minutes=settings.DOCTOR_SESSION_TTL_MINUTES)
                sess.save(update_fields=["expires_at"])
                return super().dispatch(request, *args, **kwargs)
        # Not logged in: start OAuth
        next_url = request.get_full_path()
        start = reverse("vaccinations:oauth-google-start")
        q = urllib.parse.urlencode({"token": token, "next": next_url})
        return redirect(f"{start}?{q}")

# ---------- Doctor portal (token-based) ----------

class DoctorPortalHomeView(View):
    template_name = "vaccinations/doctor_portal_home.html"

    def dispatch(self, request, token: str, *args, **kwargs):
        self.doctor = get_object_or_404(Doctor, portal_token=token)
        if not _require_doctor_auth(request, token):
            start = reverse("vaccinations:oauth-google-start")
            nxt = reverse("vaccinations:doc-home", args=[token])
            return redirect(f"{start}?{urlencode({'token': token, 'next': nxt})}")
        return super().dispatch(request, token, *args, **kwargs)

    def get(self, request, token):
        return render(request, self.template_name, {"doctor": self.doctor})

# Reuse AddChildForm & logic, but force clinic to current doctor's clinic
class DoctorPortalAddRecordView(View):
    template_name = "vaccinations/add_record.html"

    def dispatch(self, request, token: str, *args, **kwargs):
        self.doctor = get_object_or_404(Doctor, portal_token=token)
        if not _require_doctor_auth(request, token):
            start = reverse("vaccinations:oauth-google-start")
            nxt = request.get_full_path()
            return redirect(f"{start}?{urlencode({'token': token, 'next': nxt})}")
        return super().dispatch(request, token, *args, **kwargs)

    def get(self, request, token: str):
        return render(
            request,
            self.template_name,
            {
                "form": AddChildForm(),
                "header_home": self.doctor.portal_path,
                "show_send_button": True,
            },
        )

    def post(self, request, token: str):
        form = AddChildForm(request.POST)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {
                    "form": form,
                    "header_home": self.doctor.portal_path,
                    "show_send_button": True,
                },
            )

        wa_input = form.cleaned_data["parent_whatsapp"]
        parents_qs = _equivalent_parents_by_input(wa_input)
        if parents_qs.exists():
            parent = parents_qs.first()
        else:
            # Use hash-aware get/create with transaction to handle race conditions
            from django.db import IntegrityError, transaction
            try:
                with transaction.atomic(using="patients"):
                    parent = Parent.objects.using("patients").create()
                    parent.whatsapp_e164 = wa_input  # sets enc + hash via setter
                    parent.save(using="patients")
            except IntegrityError:
                # Handle rare race: someone else inserted the same number simultaneously
                # Re-query to find the parent that was created by another process
                parents_qs = _equivalent_parents_by_input(wa_input)
                parent = parents_qs.first()
                if not parent:
                    # If still not found, something else went wrong, re-raise
                    raise
            parents_qs = Parent.objects.using("patients").filter(pk=parent.pk)

        # Match the public add flow and write against the patients DB explicitly.
        # The live MySQL schema still requires the legacy columns such as `full_name`.
        child, created = Child.objects.using("patients").get_or_create(
            parent=parent,
            full_name=form.cleaned_data["child_name"],
            date_of_birth=form.cleaned_data["date_of_birth"],
            defaults={
                "clinic": self.doctor.clinic,
                "sex": form.cleaned_data["gender"],
                "state": form.cleaned_data["state"] or self.doctor.clinic.state,
            },
        )

        if created:
            messages.success(request, "Record added successfully.")
        else:
            messages.info(request, "Record already exists.")

        # WhatsApp sending logic
        if request.POST.get("send_to_patient") == "1":
            share = ChildShareLink.issue_for(child, created_by=self.doctor)
            primary, secondary = choose_two_languages_for_state(
                child.state or self.doctor.clinic.state
            )
            link = request.build_absolute_uri(
                reverse("vaccinations:parent-share", args=[share.token])
            )
            msg_primary = build_patient_message(primary, self.doctor.full_name, link)
            msg_secondary = build_patient_message(secondary, self.doctor.full_name, link)
            message = f"{msg_primary}\n\n——————————\n\n{msg_secondary}"

            import re, urllib.parse
            parent_whatsapp = parent.whatsapp_e164
            phone_number = re.sub(r"[^\d+]", "", parent_whatsapp)
            if not phone_number.startswith("+"):
                if phone_number.startswith("91"):
                    phone_number = "+" + phone_number
                elif phone_number.startswith("0"):
                    phone_number = "+91" + phone_number[1:]
                else:
                    phone_number = "+91" + phone_number
            clean_number = phone_number.replace("+", "")
            encoded_message = urllib.parse.quote(message)
            whatsapp_url = f"https://web.whatsapp.com/send?phone={clean_number}&text={encoded_message}"
            return redirect(whatsapp_url)

        # Default redirect if WhatsApp not sent
        return redirect(
            "vaccinations:doc-card", token=self.doctor.portal_token, child_id=child.id
        )


# Doctor lookup -> list children in THIS clinic only
class DoctorPortalUpdateLookupView(View):
    template_name = "vaccinations/doctor_update_lookup.html"

    def dispatch(self, request, token: str, *args, **kwargs):
        self.doctor = get_object_or_404(Doctor, portal_token=token)
        if not _require_doctor_auth(request, token):
            start = reverse("vaccinations:oauth-google-start")
            nxt = request.get_full_path()
            return redirect(f"{start}?{urlencode({'token': token, 'next': nxt})}")
        return super().dispatch(request, token, *args, **kwargs)

    def get(self, request, token: str):
        return render(request, self.template_name, {"form": WhatsAppLookupForm(), "doctor": self.doctor})

    def post(self, request, token: str):
        form = WhatsAppLookupForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "doctor": self.doctor})
        wa = form.cleaned_data["parent_whatsapp"]
        parents_qs = _equivalent_parents_by_input(wa)
        child_qs = Child.objects.filter(
            parent_id__in=list(parents_qs.values_list("id", flat=True)),
            clinic=self.doctor.clinic,
        )
        children = list(child_qs.order_by("full_name", "date_of_birth"))
        return render(request, "vaccinations/doctor_select_child.html", {"doctor": self.doctor, "children": children})

# Card views (doctor-scoped authorization)
class DoctorPortalCardDueView(View):
    def dispatch(self, request, token: str, *args, **kwargs):
        self.doctor = get_object_or_404(Doctor, portal_token=token)
        if not _require_doctor_auth(request, token):
            start = reverse("vaccinations:oauth-google-start")
            nxt = request.get_full_path()
            return redirect(f"{start}?{urlencode({'token': token, 'next': nxt})}")
        return super().dispatch(request, token, *args, **kwargs)

    def get_child(self, child_id: int) -> Child | None:
        child = get_object_or_404(Child, pk=child_id)
        if child.clinic_id != self.doctor.clinic_id:
            messages.error(self.request, "This record is not part of your clinic.")
            return None
        return child

    def get(self, request, token: str, child_id: int):
        child = self.get_child(child_id)
        if not child: return redirect("vaccinations:doc-update", token=self.doctor.portal_token)
        t = today()
        # Get child doses (without problematic cross-DB prefetch)
        cds_all = list(
            ChildDose.objects.using("patients")
            .filter(child=child)
        )
        
        # Load dose and vaccine data separately to avoid cross-DB issues
        for cd in cds_all:
            try:
                dose = VaccineDose.objects.using("default").select_related("vaccine").get(pk=cd.dose_id)
                cd.dose = dose  # Attach the full dose with vaccine
            except Exception:
                pass  # Skip if dose/vaccine loading fails
        
        anchored_ids = set(request.session.pop(SESSION_ANCHORED_ONCE, []) or [])
        rows = _compute_ui_state(child, cds_all, show_all=False, newly_anchored_ids=anchored_ids)
        return render(request, "vaccinations/card.html", {"child": child, "rows": rows, "today": t, "show_all": False, "doctor": self.doctor})

    def post(self, request, token: str, child_id: int):
        child = self.get_child(child_id)
        if not child: return redirect("vaccinations:doc-update", token=self.doctor.portal_token)
        t = today()
        # Get editable doses (without problematic cross-DB prefetch)
        editable_qs = (
            ChildDose.objects.using("patients")
            .filter(child=child, due_date__isnull=False, due_date__lte=t, given_date__isnull=True)
        )
        changed = 0
        changed_bases: List[ChildDose] = []
        for cd in editable_qs:
            raw = (request.POST.get(f"dose_{cd.id}", "") or "").strip()
            if not raw: continue
            try:
                given = date.fromisoformat(raw)
            except Exception:
                messages.error(request, f"Invalid date for {cd.dose.dose_label}.")
                continue
            if given > t:
                messages.error(request, f"{cd.dose.dose_label} cannot be a future date.")
                continue
            cd.given_date = given
            cd.save(using="patients", update_fields=["given_date", "updated_at"])
            changed += 1
            changed_bases.append(cd)
        newly_anchored = reanchor_dependents(child, changed_bases)
        if newly_anchored:
            # Store newly anchored IDs in session for next page load
            request.session[SESSION_ANCHORED_ONCE] = [cd.id for cd in newly_anchored]
            txt = "; ".join([f"{x.dose.vaccine.name} — {x.dose.dose_label}: {x.due_date}" for x in newly_anchored])
            messages.success(request, f"Updated. Newly anchored next doses: {txt}")
        elif changed:
            messages.success(request, "Vaccination record updated.")
        else:
            messages.info(request, "No changes were made.")
        return redirect("vaccinations:doc-card", token=self.doctor.portal_token, child_id=child.id)

class DoctorPortalCardAllView(View):
    def dispatch(self, request, token: str, *args, **kwargs):
        self.doctor = get_object_or_404(Doctor, portal_token=token)
        if not _require_doctor_auth(request, token):
            start = reverse("vaccinations:oauth-google-start")
            nxt = request.get_full_path()
            return redirect(f"{start}?{urlencode({'token': token, 'next': nxt})}")
        return super().dispatch(request, token, *args, **kwargs)

    def get_child(self, child_id: int) -> Child | None:
        child = get_object_or_404(Child, pk=child_id)
        if child.clinic_id != self.doctor.clinic_id:
            messages.error(self.request, "This record is not part of your clinic.")
            return None
        return child

    def get(self, request, token: str, child_id: int):
        child = self.get_child(child_id)
        if not child: return redirect("vaccinations:doc-update", token=self.doctor.portal_token)
        # Get child doses (without problematic cross-DB prefetch)
        cds = list(
            ChildDose.objects.using("patients")
            .filter(child=child)
        )
        
        # Load dose and vaccine data separately to avoid cross-DB issues
        for cd in cds:
            try:
                dose = VaccineDose.objects.using("default").select_related("vaccine").get(pk=cd.dose_id)
                cd.dose = dose  # Attach the full dose with vaccine
            except Exception:
                pass  # Skip if dose/vaccine loading fails
        rows = _compute_ui_state(child, cds, show_all=True)
        return render(request, "vaccinations/card.html", {"child": child, "rows": rows, "today": today(), "show_all": True, "doctor": self.doctor})

    def post(self, request, token: str, child_id: int):
        # identical to DoctorPortalCardDueView.post but redirect back to card-all
        child = self.get_child(child_id)
        if not child: return redirect("vaccinations:doc-update", token=self.doctor.portal_token)
        t = today()
        # Get editable doses (without problematic cross-DB prefetch)
        editable_qs = (
            ChildDose.objects.using("patients")
            .filter(child=child, due_date__isnull=False, due_date__lte=t, given_date__isnull=True)
        )
        changed = 0
        changed_bases: List[ChildDose] = []
        for cd in editable_qs:
            raw = (request.POST.get(f"dose_{cd.id}", "") or "").strip()
            if not raw: continue
            try:
                given = date.fromisoformat(raw)
            except Exception:
                messages.error(request, f"Invalid date for {cd.dose.dose_label}.")
                continue
            if given > t:
                messages.error(request, f"{cd.dose.dose_label} cannot be a future date.")
                continue
            cd.given_date = given
            cd.save(using="patients", update_fields=["given_date", "updated_at"])
            changed += 1
            changed_bases.append(cd)
        newly_anchored = reanchor_dependents(child, changed_bases)
        if newly_anchored:
            # Store newly anchored IDs in session for next page load
            request.session[SESSION_ANCHORED_ONCE] = [cd.id for cd in newly_anchored]
            txt = "; ".join([f"{x.dose.vaccine.name} — {x.dose.dose_label}: {x.due_date}" for x in newly_anchored])
            messages.success(request, f"Updated. Newly anchored next doses: {txt}")
        elif changed:
            messages.success(request, "Vaccination record updated.")
        else:
            messages.info(request, "No changes were made.")
        return redirect("vaccinations:doc-card-all", token=self.doctor.portal_token, child_id=child.id)

# Profile edit
class DoctorPortalEditProfileView(View):
    template_name = "vaccinations/doctor_profile.html"

    def dispatch(self, request, token: str, *args, **kwargs):
        self.doctor = get_object_or_404(Doctor, portal_token=token)
        if not _require_doctor_auth(request, token):
            start = reverse("vaccinations:oauth-google-start")
            nxt = request.get_full_path()
            return redirect(f"{start}?{urlencode({'token': token, 'next': nxt})}")
        return super().dispatch(request, token, *args, **kwargs)

    def get(self, request, token: str):
        c = self.doctor.clinic
        initial = {
            "doctor_name": self.doctor.full_name,
            "doctor_whatsapp": self.doctor.whatsapp_e164,
            "clinic_whatsapp": c.whatsapp_e164,
            "state": c.state,
            "head_quarters": c.headquarters,
            "clinic_address": c.address,
            "preferred_languages": c.get_languages(),
            "pincode": c.pincode,
            "doctor_email": self.doctor.email,
            "receptionist_email": c.receptionist_email,
            "imc_number": self.doctor.imc_number,
        }
        return render(request, self.template_name, {
            "form": DoctorClinicProfileForm(initial=initial),
            "doctor": self.doctor
        })

    def post(self, request, token: str):
        form = DoctorClinicProfileForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "doctor": self.doctor})
        # update clinic
        c = self.doctor.clinic
        c.state = form.cleaned_data["state"]
        c.headquarters = form.cleaned_data.get("head_quarters","")
        c.address = form.cleaned_data.get("clinic_address","")
        c.set_languages(form.cleaned_data.get("preferred_languages", []))
        c.pincode = form.cleaned_data.get("pincode","")
        c.whatsapp_e164 = form.cleaned_data.get("clinic_whatsapp","")
        c.receptionist_email = form.cleaned_data.get("receptionist_email","")
        c.save()
        # update doctor
        self.doctor.full_name = form.cleaned_data["doctor_name"]
        self.doctor.whatsapp_e164 = form.cleaned_data["doctor_whatsapp"]
        self.doctor.email = form.cleaned_data["doctor_email"]
        self.doctor.imc_number = form.cleaned_data["imc_number"]
        if form.cleaned_data.get("doctor_photo"):
            self.doctor.photo = form.cleaned_data["doctor_photo"]
        self.doctor.save()
        messages.success(request, "Profile updated.")
        return redirect("vaccinations:doc-home", token=self.doctor.portal_token)

# ---------- OAuth endpoints ----------

class GoogleAuthStartView(View):
    """
    Entry: /auth/google/start/?token=<doctor_portal_token>&next=/d/<token>[/...]
    Saves state in session, redirects to Google.
    """
    def get(self, request):
        token = (request.GET.get("token") or "").strip()
        next_url = request.GET.get("next") or f"/d/{token}/profile/"
        force = request.GET.get("force") == "1"
        # Validate token exists
        doctor = get_object_or_404(Doctor, portal_token=token)

        state = _safe_state()
        redirect_uri = settings.GOOGLE_OAUTH["REDIRECT_URI"]
        _stash_oauth(request, state=state, token=token, next_url=next_url, redirect_uri=redirect_uri)
        request.session.save()  # ensure sessionid cookie is written before redirect

        auth_url = _build_google_auth_url(state, redirect_uri, force_choose=force)
        return HttpResponseRedirect(auth_url)

class GoogleAuthCallbackView(View):
    """
    Handles Google's redirect. On success, verifies email matches the Doctor record.
    If mismatch: redirect to friendly error page with a "Use another account" button.
    """
    def get(self, request):
        code = request.GET.get("code") or ""
        state = request.GET.get("state") or ""
        stash = _pop_stash(request)
        

        if not stash or not state or stash.get("state") != state:
            # Session expired - redirect back to OAuth start without error message
            token = stash.get("doctor_token") if stash else ""
            _clear_stash(request)
            if token:
                retry = f'{reverse("vaccinations:oauth-google-start")}?token={urllib.parse.quote(token)}&next={urllib.parse.quote(stash.get("next","/"))}&force=1'
                return redirect(retry)
            # No token available - redirect to home
            return redirect("vaccinations:home")

        token = stash["doctor_token"]
        doctor = get_object_or_404(Doctor, portal_token=token)

        try:
            tokens = _exchange_code_for_tokens(code, stash["redirect_uri"])
            userinfo = _fetch_userinfo(tokens["access_token"])
            email = (userinfo.get("email") or "").strip().lower()
            email_verified = bool(userinfo.get("email_verified"))
        except Exception:
            _clear_stash(request)
            # Network error - redirect back to OAuth start without error message
            retry = f'{reverse("vaccinations:oauth-google-start")}?token={urllib.parse.quote(token)}&next={urllib.parse.quote(stash.get("next","/"))}&force=1'
            return redirect(retry)

        expected = (doctor.email or "").strip().lower()
        
        # If doctor has no email registered, allow any verified email (first-time login)
        if not expected:
            if not email_verified:
                # Unverified email - redirect back to OAuth start without error message
                _clear_stash(request)
                retry = f'{reverse("vaccinations:oauth-google-start")}?token={urllib.parse.quote(token)}&next={urllib.parse.quote(stash.get("next","/"))}&force=1'
                return redirect(retry)
        else:
            # Doctor has email registered, it must match
            if not email_verified or (email != expected):
                # Wrong email - redirect back to OAuth start with force account chooser
                _clear_stash(request)
                retry = f'{reverse("vaccinations:oauth-google-start")}?token={urllib.parse.quote(token)}&next={urllib.parse.quote(stash.get("next","/"))}&force=1'
                return redirect(retry)

        # Success: mark session as logged-in for this doctor+portal
        request.session[DOC_AUTH_SESSION_KEY] = {
            "doctor_id": doctor.id,
            "portal_token": doctor.portal_token,
            "email": email,
            "ts": timezone.now().isoformat(),
        }
        _clear_stash(request)

        # Go to requested page (defaults to doctor profile)
        next_url = stash.get("next") or f"/d/{token}/profile/"
        return redirect(next_url)


# ---------------------------
# Doctor Education (per vaccine)
# ---------------------------
class DoctorVaccineEducationView(View):
    template_name = "vaccinations/vaccine_doctor_education.html"

    def dispatch(self, request, token: str, *args, **kwargs):
        self.doctor = get_object_or_404(Doctor, portal_token=token)
        if not _require_doctor_auth(request, token):
            start = reverse("vaccinations:oauth-google-start")
            nxt = request.get_full_path()
            return redirect(f"{start}?{urlencode({'token': token, 'next': nxt})}")
        return super().dispatch(request, token, *args, **kwargs)

    def get(self, request, token: str, vaccine_id: int):
        # Mapping from dose-specific vaccines (default DB) to generic vaccines (masters DB)
        vaccine_mapping = {
            'bcg': 'bcg', 'hep_b1': 'hep-b', 'hep_b2': 'hep-b', 'hep_b3': 'hep-b', 'hep_b4': 'hep-b',
            'opv': 'opv', 'dtwp_dtap1': 'dtwp-dtap', 'dtwp_dtap2': 'dtwp-dtap', 'dtwp_dtap3': 'dtwp-dtap', 'dtwp_dtap': 'dtwp-dtap',
            'hib-1': 'hib', 'hib-2': 'hib', 'hib-3': 'hib', 'hib': 'hib',
            'ipv-1': 'ipv', 'ipv-2': 'ipv', 'ipv-3': 'ipv', 'ipv': 'ipv',
            'pcv_1': 'pcv', 'pcv_2': 'pcv', 'pcv_3': 'pcv', 'pcv_booster': 'pcv',
            'rota-1': 'rota', 'rota-2': 'rota', 'rota-3': 'rota-3',
            'influenza-1': 'influenza', 'influenza-2': 'influenza',
            'typhoid_conjugate_vaccine': 'typhoid-conjugate-vaccine',
            'mmr_1_measles_mumps_rubella': 'mmr', 'mmr_2': 'mmr', 'mmr_3': 'mmr',
            'hepatitis_a-1': 'hepatitis-a', 'hepatitis_a-2': 'hepatitis-a-2',
            'varicella': 'varicella', 'varicella_2': 'varicella',
            'hpv_2_doses': 'hpv', 'tdap_td': 'tdap-td', 'annual_influenza_vaccine': 'annual-influenza-vaccine',
        }
        
        # Try masters database first, then default database as fallback
        try:
            vaccine = Vaccine.objects.using("masters").select_related("schedule_version").prefetch_related("doctor_education").filter(is_active=True).get(pk=vaccine_id)
        except Vaccine.DoesNotExist:
            # If not found in masters, get from default and find equivalent in masters
            default_vaccine = get_object_or_404(Vaccine.objects.using("default").select_related("schedule_version"), pk=vaccine_id)
            
            # Try direct code match first
            try:
                vaccine = Vaccine.objects.using("masters").select_related("schedule_version").prefetch_related("doctor_education").filter(is_active=True).get(code=default_vaccine.code)
            except Vaccine.DoesNotExist:
                # Use mapping to find generic vaccine
                masters_code = vaccine_mapping.get(default_vaccine.code)
                if masters_code:
                    try:
                        vaccine = Vaccine.objects.using("masters").select_related("schedule_version").prefetch_related("doctor_education").filter(is_active=True).get(code=masters_code)
                    except Vaccine.DoesNotExist:
                        vaccine = default_vaccine
                else:
                    vaccine = default_vaccine
        return render(request, self.template_name, {"doctor": self.doctor, "vaccine": vaccine})

# ---------------------------
# Child-level Reminder Schedule
# ---------------------------
class DoctorChildRemindersView(View):
    template_name = "vaccinations/doctor_child_reminders.html"

    def dispatch(self, request, token: str, *args, **kwargs):
        self.doctor = get_object_or_404(Doctor, portal_token=token)
        if not _require_doctor_auth(request, token):
            start = reverse("vaccinations:oauth-google-start")
            nxt = request.get_full_path()
            return redirect(f"{start}?{urlencode({'token': token, 'next': nxt})}")
        return super().dispatch(request, token, *args, **kwargs)

    def get_child(self, child_id: int):
        child = get_object_or_404(Child, pk=child_id)
        if child.clinic_id != self.doctor.clinic_id:
            messages.error(self.request, "This record is not part of your clinic.")
            return None
        return child

    def get(self, request, token: str, child_id: int):
        child = self.get_child(child_id)
        if not child:
            return redirect("vaccinations:doc-update", token=self.doctor.portal_token)

        t = today()
        rows = []
        # Get child doses (without problematic cross-DB prefetch)
        child_doses = list(
            ChildDose.objects.using("patients")
            .filter(child=child)
        )
        
        # Load dose and vaccine data separately to avoid cross-DB issues
        for cd in child_doses:
            try:
                dose = VaccineDose.objects.using("default").select_related("vaccine").get(pk=cd.dose_id)
                cd.dose = dose  # Attach the full dose with vaccine
            except Exception:
                pass  # Skip if dose/vaccine loading fails
        for cd in child_doses:
            status, eligible = reminder_status_for_cd(cd, t)
            rows.append({
                "cd": cd,
                "vaccine": cd.dose.vaccine,
                "due_date": cd.due_date,
                "status": status,
                "eligible": eligible,
                # add your own reminder log/timestamp here if you store it:
                "sent_at": getattr(cd, "last_reminder_at", None),
            })

        return render(
            request,
            self.template_name,
            {"doctor": self.doctor, "child": child, "rows": rows},
        )

# ---------------------------
# Send Reminder (opens WhatsApp)
# ---------------------------
class DoctorPortalSendReminderView(View):
    def dispatch(self, request, token: str, *args, **kwargs):
        self.doctor = get_object_or_404(Doctor, portal_token=token)
        if not _require_doctor_auth(request, token):
            start = reverse("vaccinations:oauth-google-start")
            nxt = request.get_full_path()
            return redirect(f"{start}?{urlencode({'token': token, 'next': nxt})}")
        return super().dispatch(request, token, *args, **kwargs)

    def get(self, request, token: str, child_dose_id: int):
        # Get the ChildDose first, then load related data separately to avoid complex query issues
        cd = get_object_or_404(ChildDose.objects.using("patients"), pk=child_dose_id)
        
        # Load related data with proper database routing
        try:
            # Load dose and vaccine separately to avoid cross-database issues
            from vaccinations.models import VaccineDose
            dose = VaccineDose.objects.using("default").select_related("vaccine").get(pk=cd.dose_id)
            vaccine = dose.vaccine
        except Exception as e:
            # If there are issues with vaccine loading, continue without it
            vaccine = None
            print(f"Error loading vaccine: {e}")
        if cd.child.clinic_id != self.doctor.clinic_id:
            messages.error(request, "This record is not part of your clinic.")
            return redirect("vaccinations:doc-update", token=self.doctor.portal_token)

        status, eligible = reminder_status_for_cd(cd, today())
        if not eligible:
            messages.error(request, "This dose is not eligible for a reminder right now.")
            return redirect("vaccinations:doc-child-reminders", token=token, child_id=cd.child_id)

        child = cd.child
        # Use the vaccine we loaded earlier, or fallback to cd.dose.vaccine
        if vaccine is None:
            try:
                vaccine = cd.dose.vaccine
            except Exception:
                messages.error(request, "Unable to load vaccine information.")
                return redirect("vaccinations:doc-child-reminders", token=token, child_id=cd.child_id)

        # Build Patient Education link for this vaccine (shows all 8 languages)
        education_link = request.build_absolute_uri(
            reverse("vaccinations:patient-edu", args=[vaccine.id])
        )
        message = build_bilingual_vaccine_message(child, vaccine, cd.due_date, self.doctor, education_link)
        to = child.parent.whatsapp_e164

        # Mark as sent (so the button turns green on return)
        now = timezone.now()
        ChildDose.objects.using("patients").filter(pk=cd.pk).update(
            last_reminder_at=now, last_reminder_for_date=cd.due_date, last_reminder_by=self.doctor
        )

        url = build_whatsapp_url(to, message, request)
        return redirect(url)

# ---------------------------
# Clinic-wide Reminders dashboard
# ---------------------------
from django.db.models import Q, Prefetch

class DoctorPortalRemindersView(View):
    template_name = "vaccinations/doctor_reminders.html"

    def dispatch(self, request, token: str, *args, **kwargs):
        self.doctor = get_object_or_404(Doctor, portal_token=token)
        if not _require_doctor_auth(request, token):
            start = reverse("vaccinations:oauth-google-start")
            nxt = request.get_full_path()
            return redirect(f"{start}?{urlencode({'token': token, 'next': nxt})}")
        return super().dispatch(request, token, *args, **kwargs)

    def get(self, request, token: str):
        # --- always populate vaccine dropdown
        sv = current_schedule()
        vqs = Vaccine.objects.using("default").filter(is_active=True)
        if sv:
            vqs = vqs.filter(schedule_version=sv)
        vaccines = list(vqs.only("id", "name").order_by("name"))

        # --- read filters
        t = today()
        status_filter = (request.GET.get("status") or "overdue_today").strip()
        vaccine_id = (request.GET.get("vaccine") or "").strip()
        query = (request.GET.get("q") or "").strip()

        # --- base queryset: this clinic, not yet given (simplified to avoid cross-DB issues)
        base = (ChildDose.objects.using("patients")
                .select_related("child__parent", "child")
                .filter(child__clinic_id=self.doctor.clinic_id, given_date__isnull=True))

        # --- apply status filter
        if status_filter == "upcoming_24h":
            qs = base.filter(due_date__gt=t, due_date__lte=t + timedelta(days=1))
        else:
            # Default view: include doses that are due today (inside window)
            # plus ALL overdue vaccines (including old overdue for health reasons)
            due_window_q = (
                Q(due_date__lte=t) & (Q(due_until_date__isnull=True) | Q(due_until_date__gte=t))
            )
            # Include all overdue vaccines (not just recent ones)
            all_overdue_q = (
                Q(due_until_date__lt=t) |  # Past due_until_date
                Q(due_until_date__isnull=True, due_date__lt=t)  # Past due_date when no until_date
            )
            qs = base.filter(due_window_q | all_overdue_q)

        # --- optional vaccine filter
        if vaccine_id:
            qs = qs.filter(dose__vaccine_id=vaccine_id)

        # --- optional text search filter
        if query:
            # Try name search first
            name_results = qs.filter(Q(child__full_name__icontains=query))
            
            # Try phone search if query contains digits
            digits = last10_digits(query)
            if digits:
                phone_results = qs.filter(child__parent__whatsapp_hash=phone_hash(query))
                # Combine name and phone results
                qs = name_results | phone_results
            else:
                # Only name search
                qs = name_results

        # --- build rows for the table
        rows = []
        for cd in qs.order_by("due_date"):  # Simplified ordering
            try:
                status, eligible = reminder_status_for_cd(cd, t)
                
                # Load vaccine data separately to avoid cross-DB issues
                try:
                    from vaccinations.models import VaccineDose
                    dose = VaccineDose.objects.using("default").select_related("vaccine").get(pk=cd.dose_id)
                    vaccine = dose.vaccine
                except Exception as e:
                    # Log the error for debugging
                    print(f"Error loading vaccine for dose {cd.dose_id}: {e}")
                    vaccine = None
                
                rows.append({
                    "cd": cd,
                    "child": cd.child,
                    "vaccine": vaccine,
                    "due_date": cd.due_date,
                    "status": status,
                    "eligible": eligible,
                    "sent_at": getattr(cd, "last_reminder_at", None),
                })
            except Exception as e:
                # Skip problematic rows but continue processing
                print(f"Error processing ChildDose {cd.id}: {e}")
                continue

        return render(
            request,
            self.template_name,
            {
                "doctor": self.doctor,
                "vaccines": vaccines,
                "status_filter": status_filter,
                "vaccine_id": vaccine_id,
                "query": query,
                "rows": rows,
            },
        )


class DoctorLogoutView(View):
    def get(self, request, token: str):
        # Handle GET requests (direct URL access) the same as POST
        return self.post(request, token)
    
    def post(self, request, token: str):
        # Clear the doctor authentication session
        if "doctor_auth" in request.session:
            del request.session["doctor_auth"]
        
        # Logout from Django's authentication system
        from django.contrib.auth import logout
        logout(request)
        
        # Redirect to home page with success message
        messages.success(request, "You have been successfully logged out.")
        return redirect("vaccinations:home")


# Simple Patient Education view for reminder links
class PatientEducationView(View):
    """
    Shows Patient Education videos for a vaccine in all available languages.
    Used by reminder messages to provide comprehensive education content.
    """
    template_name = "vaccinations/parent_education_simple.html"

    def get(self, request, vaccine_id: int):
        vaccine_mapping = {
            'bcg': 'bcg', 'hep_b1': 'hep-b', 'hep_b2': 'hep-b', 'hep_b3': 'hep-b', 'hep_b4': 'hep-b',
            'opv': 'opv', 'dtwp_dtap1': 'dtwp-dtap', 'dtwp_dtap2': 'dtwp-dtap', 'dtwp_dtap3': 'dtwp-dtap', 'dtwp_dtap': 'dtwp-dtap',
            'hib-1': 'hib', 'hib-2': 'hib', 'hib-3': 'hib', 'hib': 'hib',
            'ipv-1': 'ipv', 'ipv-2': 'ipv', 'ipv-3': 'ipv', 'ipv': 'ipv',
            'pcv_1': 'pcv', 'pcv_2': 'pcv', 'pcv_3': 'pcv', 'pcv_booster': 'pcv',
            'rota-1': 'rota', 'rota-2': 'rota', 'rota-3': 'rota-3',
            'influenza-1': 'influenza', 'influenza-2': 'influenza',
            'typhoid_conjugate_vaccine': 'typhoid-conjugate-vaccine',
            'mmr_1_measles_mumps_rubella': 'mmr', 'mmr_2': 'mmr', 'mmr_3': 'mmr',
            'hepatitis_a-1': 'hepatitis-a', 'hepatitis_a-2': 'hepatitis-a-2',
            'varicella': 'varicella', 'varicella_2': 'varicella',
            'hpv_2_doses': 'hpv', 'tdap_td': 'tdap-td', 'annual_influenza_vaccine': 'annual-influenza-vaccine',
        }

        try:
            # Try masters database first, fallback to default
            try:
                vaccine = Vaccine.objects.using("masters").select_related("schedule_version").get(pk=vaccine_id)
            except Vaccine.DoesNotExist:
                default_vaccine = get_object_or_404(
                    Vaccine.objects.using("default").select_related("schedule_version"), pk=vaccine_id
                )
                masters_code = vaccine_mapping.get(default_vaccine.code) or default_vaccine.code
                vaccine = (
                    Vaccine.objects.using("masters").select_related("schedule_version")
                    .filter(code=masters_code).first()
                    or default_vaccine
                )

            # 🟩 STEP 1: Get preferred language (persistent across refresh)
            lang = (
                request.GET.get("lang")
                or request.session.get("lang")
                or request.COOKIES.get("lang")
                or "en"
            )

            # 🟩 STEP 2: Fetch all available videos for the vaccine
            videos = get_patient_videos(vaccine, [])

            # Group videos by language
            videos_by_lang = {}
            for video in videos:
                if video.video_url:
                    vlang = video.language or "en"
                    videos_by_lang.setdefault(vlang, []).append(video)

            # 🟩 STEP 3: Select videos for current language, fallback to English
            selected_videos = videos_by_lang.get(lang)
            if not selected_videos:
                selected_videos = videos_by_lang.get("en", [])

            # 🟩 STEP 4: Prepare context
            ctx = {
                "title": f"Education Videos - {vaccine.name}",
                "vaccine": vaccine,
                "videos_by_lang": videos_by_lang,
                "selected_videos": selected_videos,
                "lang": lang,
            }

            # 🟩 STEP 5: Set language in session & cookie (to persist)
            response = render(request, self.template_name, ctx)
            response.set_cookie("lang", lang)
            request.session["lang"] = lang
            return response

        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error in PatientEducationView: {str(e)}")

            return render(request, self.template_name, {
                "title": "Error Loading Videos",
                "vaccine": None,
                "videos_by_lang": {},
                "selected_videos": [],
                "lang": "en",
                "error": str(e)
            })
