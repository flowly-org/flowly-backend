from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    has_pro = serializers.BooleanField(read_only=True)
    has_pro_plus = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "github_login", "language", "home_location", "is_staff", "plan", "has_pro",
                  "has_pro_plus", "is_bot", "adhd_mode", "focus"]
        read_only_fields = ["id", "email", "is_staff", "plan", "has_pro", "has_pro_plus", "is_bot"]

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

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "password", "language"]
        read_only_fields = ["id"]
        extra_kwargs = {"language": {"required": False}}

    def validate_language(self, value):
        from tasks.services import DEFAULT_NAMES

        return value if value in DEFAULT_NAMES else "en"

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value.lower()

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)
