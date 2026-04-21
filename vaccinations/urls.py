# vaccinations/urls.py
from django.urls import path
from .views import (
    HomeView, AddRecordView, UpdateLookupView,
    VaccinationCardDueView, VaccinationCardAllView,
    ChildCardAPI,
    PartnerCreateUploadView, AdminAccessView,
    DoctorRegisterSelfView, DoctorRegisterPartnerView,
    DoctorPortalHomeView, DoctorPortalAddRecordView, DoctorPortalUpdateLookupView,
    DoctorPortalCardDueView, DoctorPortalCardAllView, DoctorPortalEditProfileView,
    DoctorChildRemindersView, DoctorPortalRemindersView,
    DoctorPortalSendReminderView, DoctorVaccineEducationView,
    DoctorLogoutView, ParentShareStartView, PatientEducationView, VaccinationHistoryView, ParentVaccineEducationView, VaccinationHistoryCSVExportView
)
from .views_auth import google_oauth_start, google_oauth_callback  # NEW
from . import views
app_name = "vaccinations"

urlpatterns = [
    path("", HomeView.as_view(), name="home"),
    path("add/", AddRecordView.as_view(), name="add"),
    path("update/", UpdateLookupView.as_view(), name="update"),

    # Existing due-only card (unchanged behavior)
    path("card/<int:child_id>/", VaccinationCardDueView.as_view(), name="card"),

    # NEW: Full schedule card (past + today + future)
    path("card-all/<int:child_id>/", VaccinationCardAllView.as_view(), name="card-all"),

    # Optional API
    path("api/children/<int:pk>/card/", ChildCardAPI.as_view(), name="card-api"),

    # Admin publishing
    path("partners/new/", PartnerCreateUploadView.as_view(), name="partner-create"),
    path("admin/access/", AdminAccessView.as_view(), name="admin-access"),
    # Doctor registration
    path("doctor/register/", DoctorRegisterSelfView.as_view(), name="doctor-register-self"),
    path("doctor/register/<str:token>/", DoctorRegisterPartnerView.as_view(), name="doctor-register-partner"),
    # Doctor portal (token)
    path("d/<str:token>/", DoctorPortalHomeView.as_view(), name="doc-home"),
    path("d/<str:token>/add/", DoctorPortalAddRecordView.as_view(), name="doc-add"),
    path("d/<str:token>/update/", DoctorPortalUpdateLookupView.as_view(), name="doc-update"),
    path("d/<str:token>/card/<int:child_id>/", DoctorPortalCardDueView.as_view(), name="doc-card"),
    path("d/<str:token>/card-all/<int:child_id>/", DoctorPortalCardAllView.as_view(), name="doc-card-all"),
    # NEW: reminders (child-level and clinic-wide) + send action + doctor education
    path("d/<str:token>/child/<int:child_id>/reminders/", DoctorChildRemindersView.as_view(), name="doc-child-reminders"),
    path("d/<str:token>/reminders/", DoctorPortalRemindersView.as_view(), name="doc-reminders"),
    path("d/<str:token>/send-reminder/<int:child_dose_id>/", DoctorPortalSendReminderView.as_view(), name="doc-send-reminder"),
    path("d/<str:token>/vaccine/<int:vaccine_id>/", DoctorVaccineEducationView.as_view(), name="doc-vaccine-edu"),
    path("d/<str:token>/profile/", DoctorPortalEditProfileView.as_view(), name="doc-profile"),
    # OAuth endpoints
    path("auth/google/start/", google_oauth_start, name="oauth-google-start"),
    path("auth/google/callback/", google_oauth_callback, name="oauth-google-callback"),
    path("accounts/google/callback/", google_oauth_callback, name="oauth-google-callback-short-legacy"),
    path("accounts/google/login/callback/", google_oauth_callback, name="oauth-google-callback-legacy"),
    path("d/<str:token>/logout/", DoctorLogoutView.as_view(), name="doc-logout"),
    # Parent share link
    path("p/<str:token>/", ParentShareStartView.as_view(), name="parent-share"),
    # Patient Education page (for reminder links)
    path("edu/patient/v/<int:vaccine_id>/", PatientEducationView.as_view(), name="patient-edu"),
     path("history/<int:child_id>/", VaccinationHistoryView.as_view(), name="history"),
    path("edu/vaccine/<int:vaccine_id>/", ParentVaccineEducationView.as_view(), name="parent-edu"),
    path("history/<int:child_id>/export/", VaccinationHistoryCSVExportView.as_view(), name="history-csv-export"),
    path(
        "edu/patient/<int:vaccine_id>/",
        views.PatientEducationView.as_view(),
        name="patient-edu",
    ),
]
