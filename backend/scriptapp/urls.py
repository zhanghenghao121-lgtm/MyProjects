from django.urls import path
from .views import (
    ScriptHealthAPIView,
    extract_entities,
    list_scenes,
    parse_script,
    update_scene,
    upload_image,
    upload_script,
)


urlpatterns = [
    path("health/", ScriptHealthAPIView.as_view(), name="scriptapp-health"),
    path("upload/", upload_script, name="scriptapp-upload"),
    path("entities/<int:script_id>/", extract_entities, name="scriptapp-entities"),
    path("parse/<int:script_id>/", parse_script, name="scriptapp-parse"),
    path("scenes/<int:script_id>/", list_scenes, name="scriptapp-scenes"),
    path("scene/<int:scene_id>/", update_scene, name="scriptapp-scene-update"),
    path("upload/image/", upload_image, name="scriptapp-upload-image"),
]
