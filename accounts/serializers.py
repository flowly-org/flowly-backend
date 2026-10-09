from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework.exceptions import APIException
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    has_pro = serializers.BooleanField(read_only=True)
    has_pro_plus = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "github_login", "language", "home_location", "is_staff", "plan", "has_pro",
                  "has_pro_plus", "is_bot", "adhd_mode", "focus", "plan_source", "plan_expires_at"]
        read_only_fields = ["id", "email", "is_staff", "plan", "has_pro", "has_pro_plus", "is_bot", "plan_source", "plan_expires_at"]

    def validate_focus(self, value):
        from .focus import clean

        try:
            return clean(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def to_representation(self, obj):
        from .focus import options

        data = super().to_representation(obj)
        data["focus"] = options(obj)
        return data

    def update(self, instance, validated_data):
        if "focus" in validated_data:
            validated_data["focus"] = {**(instance.focus or {}), **validated_data["focus"]}
        home_changed = "home_location" in validated_data and validated_data["home_location"] != instance.home_location
        user = super().update(instance, validated_data)
        if home_changed:
            from calendars.sync import reclassify

            reclassify(user)
        return user

    def validate_github_login(self, value):
        return value.strip().lstrip("@")

    def validate_language(self, value):
        from tasks.services import DEFAULT_NAMES

        return value if value in DEFAULT_NAMES else "en"


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    code = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=60,
                                 help_text="Invite code (optional): gives the plan it was made for.")

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "password", "language", "code"]
        read_only_fields = ["id"]
        extra_kwargs = {"language": {"required": False}}

    def validate_language(self, value):
        from tasks.services import DEFAULT_NAMES

        return value if value in DEFAULT_NAMES else "en"

    def validate_email(self, value):
        from sso.services import requires_sso

        if requires_sso(value):
            raise serializers.ValidationError("Your organization uses single sign-on: continue with SSO instead.")
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value.lower()

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate_code(self, value):
        from .codes import CodeError, find

        if not value.strip():
            return ""
        try:
            find(value)
        except CodeError as exc:
            raise serializers.ValidationError(str(exc)) from None
        return value

    def create(self, validated_data):
        from django.db import transaction

        from .codes import CodeError, redeem

        code = validated_data.pop("code", "")
        with transaction.atomic():
            user = User.objects.create_user(**validated_data)
            if code:
                try:
                    redeem(user, code)
                except CodeError as exc:  # e.g. its last use was taken a moment ago
                    raise serializers.ValidationError({"code": str(exc)}) from None
        return user


class SsoRequired(APIException):
    status_code = 400
    default_code = "sso_required"


class LoginSerializer(TokenObtainPairSerializer):
    """Email + password, except for people whose organization requires single sign-on."""

    def validate(self, attrs):
        from sso.services import requires_sso

        email = attrs.get(self.username_field, "")
        user = User.objects.filter(email__iexact=email).first()
        conn = requires_sso(email, user)
        if conn is not None:
            raise SsoRequired({"detail": f"{conn.workspace.name} requires single sign-on: continue with SSO.", "sso": "required"})
        return super().validate(attrs)
