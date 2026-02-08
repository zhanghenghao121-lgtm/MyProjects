from django.db import models
from django.contrib.auth.models import AbstractUser
import uuid


class User(AbstractUser):
    signature = models.CharField(max_length=20, blank=True, default='')
    animerole = models.CharField(max_length=20, default='npc')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    username_changed_at = models.DateTimeField(blank=True, null=True)


class AuthToken(models.Model):
    """Simple token model to pair users with login tokens we can revoke on logout."""
    key = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(User, related_name='auth_tokens', on_delete=models.CASCADE)
    created = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def generate_token():
        return uuid.uuid4().hex

    def __str__(self):
        return f"Token for {self.user.username}"
