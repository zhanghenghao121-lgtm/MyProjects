from django.urls import path
from .views import SkillsLeaderboardAPIView, HotspotListAPIView, GithubHotProjectsAPIView, DouyinHotAPIView

urlpatterns = [
    path('skills/', SkillsLeaderboardAPIView.as_view(), name='aihotspot-skills'),
    path('list/', HotspotListAPIView.as_view(), name='aihotspot-list'),
    path('github/hot/', GithubHotProjectsAPIView.as_view(), name='aihotspot-github-hot'),
    path('douyin/hot/', DouyinHotAPIView.as_view(), name='aihotspot-douyin-hot'),
]
