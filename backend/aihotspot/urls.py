from django.urls import path
from .views import SkillsLeaderboardAPIView, HotspotListAPIView

urlpatterns = [
    path('skills/', SkillsLeaderboardAPIView.as_view(), name='aihotspot-skills'),
    path('list/', HotspotListAPIView.as_view(), name='aihotspot-list'),
]
