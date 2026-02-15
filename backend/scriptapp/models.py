from django.conf import settings
from django.db import models


class Script(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="scripts",
    )
    title = models.CharField(max_length=200, default="未命名剧本")
    content = models.TextField(default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class ScriptScene(models.Model):
    script = models.ForeignKey(Script, on_delete=models.CASCADE, related_name="scenes")
    time_range = models.CharField(max_length=80, default="")
    characters = models.JSONField(default=list, blank=True)
    character_images = models.JSONField(default=dict, blank=True)
    scene_desc = models.TextField(default="", blank=True)
    prompt = models.TextField(default="", blank=True)
    scene_image_url = models.CharField(max_length=1200, default="", blank=True)
    user_remark = models.TextField(default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.script_id}-{self.time_range or 'scene'}"
