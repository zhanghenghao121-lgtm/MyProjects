from django.db import models
from django.contrib.auth.models import AbstractUser
import uuid


class User(AbstractUser):
    """Custom user model (currently no extra fields)."""
    pass


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
