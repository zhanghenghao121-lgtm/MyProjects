from django.urls import path
from .views import ChatAPIView, RagRebuildAPIView, RagStatsAPIView

urlpatterns = [
    path('chat/', ChatAPIView.as_view(), name='aichat-chat'),
    path('rag/stats/', RagStatsAPIView.as_view(), name='aichat-rag-stats'),
    path('rag/rebuild/', RagRebuildAPIView.as_view(), name='aichat-rag-rebuild'),
]
