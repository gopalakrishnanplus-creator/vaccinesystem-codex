from __future__ import annotations
import secrets
import urllib.parse
import hmac
import requests
from django.conf import settings
from django.contrib import messages, auth
from django.contrib.auth import get_user_model, login
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone

from .models import Doctor

_OAUTH_STASH = "oauth_state"
_DOCTOR_AUTH = "doctor_auth"

def _redirect_uri(request):
    configured = settings.GOOGLE_OAUTH.get("REDIRECT_URI")
    if configured:
        return configured
    # Fallback for local settings where only REDIRECT_PATH is defined.
    return request.build_absolute_uri(settings.GOOGLE_OAUTH["REDIRECT_PATH"])

def _safe_next(request, raw_next: str | None) -> str:
    # Prevent open redirects; only allow our own host.
    nxt = raw_next or "/"
    if url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return "/"

def google_oauth_start(request):
    token = request.GET.get("token", "").strip()   # doctor portal token
    nxt = request.GET.get("next")                  # where to return after login
    if not token:
        messages.error(request, "Missing doctor link.")
        return redirect("vaccinations:home")

    # Generate URL-safe state.  No punctuation, so no '*' mismatch again.
    state = secrets.token_urlsafe(24)

    stash = {
        "state": state,
        "doctor_token": token,
        "next": _safe_next(request, nxt) or f"/d/{token}/",
        "redirect_uri": _redirect_uri(request),
        "ts": timezone.now().isoformat(),
    }
    request.session[_OAUTH_STASH] = stash
    request.session.modified = True

    params = {
        "response_type": "code",
        "client_id": settings.GOOGLE_OAUTH["CLIENT_ID"],
        "redirect_uri": stash["redirect_uri"],
        "scope": " ".join(settings.GOOGLE_OAUTH["SCOPES"]),
        "state": state,
        "access_type": "online",
        "include_granted_scopes": "true",
        "prompt": "select_account",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return redirect(url)

def google_oauth_callback(request):
    code = request.GET.get("code")
    state = request.GET.get("state") or ""
    stash = request.session.get(_OAUTH_STASH)

    if not code or not stash:
        messages.error(request, "Login session expired. Please try again.")
        return redirect("vaccinations:home")

    if not hmac.compare_digest(state, stash.get("state", "")):
        # State mismatch -> restart start() for the same doctor token.
        messages.warning(request, "Security check failed. Starting sign-in again.")
        return redirect(f'{reverse("vaccinations:oauth-google-start")}'
                        f'?token={stash.get("doctor_token","")}'
                        f'&next={urllib.parse.quote(stash.get("next","/"))}')

    redirect_uri = stash["redirect_uri"]
    data = {
        "code": code,
        "client_id": settings.GOOGLE_OAUTH["CLIENT_ID"],
        "client_secret": settings.GOOGLE_OAUTH["CLIENT_SECRET"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    # Exchange code -> tokens
    try:
        r = requests.post("https://oauth2.googleapis.com/token", data=data, timeout=10)
    except requests.RequestException:
        messages.error(request, "Network error while signing in with Google.")
        return redirect(f'{reverse("vaccinations:oauth-google-start")}'
                        f'?token={stash.get("doctor_token","")}')

    if r.status_code != 200:
        messages.error(request, "Failed to sign in with Google. Please try again.")
        return redirect(f'{reverse("vaccinations:oauth-google-start")}'
                        f'?token={stash.get("doctor_token","")}')

    tokens = r.json()
    access_token = tokens.get("access_token")
    if not access_token:
        messages.error(request, "No access token from Google.")
        return redirect(f'{reverse("vaccinations:oauth-google-start")}'
                        f'?token={stash.get("doctor_token","")}')

    # Get profile (email, sub, name, picture)
    u = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if u.status_code != 200:
        messages.error(request, "Could not read Google profile.")
        return redirect(f'{reverse("vaccinations:oauth-google-start")}'
                        f'?token={stash.get("doctor_token","")}')

    prof = u.json()
    email = (prof.get("email") or "").lower()
    sub = prof.get("id") or prof.get("sub")
    if not email:
        return HttpResponseBadRequest("Google account has no email.")

    # Check the doctor the link belongs to
    doc_token = stash.get("doctor_token") or ""
    doctor = get_object_or_404(Doctor.objects.using("masters"), portal_token=doc_token)

    # Enforce the Gmail used at registration (case-insensitive).
    # If you prefer to "bind" an empty email to the Google one on first login:
    if doctor.email and doctor.email.lower() != email:
        messages.error(request, f"This link belongs to {doctor.email}. "
                                f"Please switch Google account in the popup.")
        # Clear stash to avoid loop and restart sign-in:
        request.session.pop(_OAUTH_STASH, None)
        return redirect(f'{reverse("vaccinations:oauth-google-start")}?token={doc_token}')

    # Persist Google subject for permanent binding
    doctor.google_sub = sub
    if not doctor.email:
        doctor.email = email  # first bind
    doctor.save(update_fields=["google_sub", "email"])

    # Log the browser into Django; use a service user per doctor.
    User = get_user_model()
    user, _ = User.objects.get_or_create(
        username=f"doc:{doctor.id}",
        defaults={"email": email},
    )
    login(request, user)

    # Mark this session as authorized for THIS doctor token only.
    request.session[_DOCTOR_AUTH] = {"token": doc_token, "doctor_id": doctor.id, "email": email}
    request.session.pop(_OAUTH_STASH, None)

    return redirect(stash.get("next") or reverse("vaccinations:doc-home", args=[doc_token]))
