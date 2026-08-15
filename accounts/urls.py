"""
Institution-scoped auth and user management URLs.

Mounted at the root: /login/, /register/, /password-reset/, and /settings/users/ (docs/03_Views_and_Endpoints.md).
"""

from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    # Auth & Registration
    path("register/", views.InstitutionRegisterView.as_view(), name="register"),
    path(
        "register/pending/",
        views.RegistrationPendingView.as_view(),
        name="register_pending",
    ),
    path("login/", views.InstitutionLoginView.as_view(), name="login"),
    path("logout/", views.InstitutionLogoutView.as_view(), name="logout"),
    path(
        "password-reset/",
        views.InstitutionPasswordResetView.as_view(),
        name="password_reset",
    ),
    path(
        "password-reset/sent/",
        views.InstitutionPasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "password-reset/confirm/<uidb64>/<token>/",
        views.InstitutionPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        views.InstitutionPasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    # Staff User Management (Owner only, CR-003)
    path("settings/users/", views.UserListView.as_view(), name="user_list"),
    path("settings/users/invite/", views.UserInviteView.as_view(), name="user_invite"),
    path(
        "settings/users/<int:pk>/edit/",
        views.UserUpdateView.as_view(),
        name="user_edit",
    ),
    path(
        "settings/users/accept/<uidb64>/<token>/",
        views.UserAcceptInviteView.as_view(),
        name="user_accept_invite",
    ),
]
