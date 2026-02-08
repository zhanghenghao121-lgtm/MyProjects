from django.urls import path
from .views import room_meta

urlpatterns = [
    path('meta/', room_meta),
]
