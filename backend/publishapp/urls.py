from django.urls import path

from .views import PublishHealthAPIView

urlpatterns = [
    path('health/', PublishHealthAPIView.as_view()),
]
