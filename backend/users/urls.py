from django.urls import path
from .views import register, login_view, profile

urlpatterns = [
    path('register/', register),
    path('login/', login_view),
    path('profile/', profile),
]
