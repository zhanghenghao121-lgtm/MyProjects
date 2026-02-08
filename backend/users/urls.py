from django.urls import path
from .views import (
    register,
    login_view,
    profile,
    update_profile,
    logout_view,
    captcha,
    csrf,
    send_register_email_code,
    send_reset_email_code,
    reset_password,
)

urlpatterns = [
    path('register/', register),
    path('login/', login_view),
    path('profile/', profile),
    path('profile/update/', update_profile),
    path('logout/', logout_view),
    path('captcha/', captcha),
    path('csrf/', csrf),
    path('register/send-email-code/', send_register_email_code),
    path('reset/send-email-code/', send_reset_email_code),
    path('reset-password/', reset_password),
]
