from django.urls import path

from .views import PublishHealthAPIView, auto_publish, hot_list

urlpatterns = [
    path('health/', PublishHealthAPIView.as_view()),
    path('hot-list/', hot_list),
    path('auto-publish/', auto_publish),
]
