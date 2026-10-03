import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models


class UserManager(DjangoUserManager):
    pass


class User(AbstractBaseUser, PermissionsMixin):
    objects = UserManager()

    USERNAME_FIELD = "username"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name", "email", "is_teacher"]

    username = models.CharField(unique=True, max_length=32, null=False, blank=False)
    first_name = models.CharField(max_length=35, null=False, blank=False)
    last_name = models.CharField(max_length=70, null=False, blank=False)
    email = models.EmailField(max_length=50, null=False, blank=False)

    graduation_year = models.PositiveSmallIntegerField(null=True, default=None, blank=True)

    is_active = models.BooleanField(default=True, null=False)
    is_student = models.BooleanField(default=False, null=False)
    is_teacher = models.BooleanField(default=False, null=False)
    is_superuser = models.BooleanField(default=False, null=False)
    is_staff = models.BooleanField(default=False, null=False)

    date_joined = models.DateTimeField(auto_now_add=True)

    accepted_guidelines = models.BooleanField(default=False, null=False)
    is_service = models.BooleanField(default=False)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def get_social_auth(self):
        return self.social_auth.get(provider="ion")

    def __str__(self) -> str:
        return self.username

    def __repr__(self) -> str:
        return f"<User: {self.username} ({self.id})>"


class MassEmail(models.Model):
    limit_users = models.ManyToManyField(User, blank=True, related_name="+")
    subject = models.CharField(max_length=200)
    text_html = models.TextField()
    text_plain = models.TextField()
    sender = models.ForeignKey(
        User, null=True, on_delete=models.SET_NULL, related_name="sent_emails"
    )
    created_time = models.DateTimeField(auto_now_add=True)
    sent_time = models.DateTimeField(null=True, default=None)
    status = models.CharField(
        max_length=16,
        default="queued",
        choices=[("queued", "Queued"), ("sent", "Sent"), ("failed", "Failed")],
    )
    request_id = models.UUIDField(default=uuid.uuid4, unique=True)
    recipient_emails = models.JSONField(default=list, blank=True)

    def __str__(self):
        return self.subject
