from django.urls import path
from .views import ScriptHealthAPIView


urlpatterns = [
    path("health/", ScriptHealthAPIView.as_view(), name="scriptapp-health"),
]
