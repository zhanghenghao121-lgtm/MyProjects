from django.urls import path
from .views import list_resources, download_resource

urlpatterns = [
    path("list/", list_resources),
    path("download/", download_resource),
]
