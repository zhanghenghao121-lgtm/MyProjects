from django.conf import settings
from django.db import models


class ChatMessage(models.Model):
    TYPE_TEXT = 'text'
    TYPE_IMAGE = 'image'
    TYPE_SYSTEM = 'system'
    TYPE_CHOICES = (
        (TYPE_TEXT, 'Text'),
        (TYPE_IMAGE, 'Image'),
        (TYPE_SYSTEM, 'System'),
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='chat_messages',
    )
    animerole = models.CharField(max_length=20, default='npc')
    message_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=TYPE_TEXT)
    content = models.TextField(blank=True, default='')
    image = models.ImageField(upload_to='chat/images/', blank=True, null=True)
    image_url = models.CharField(max_length=1000, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.animerole}: {self.message_type}"
