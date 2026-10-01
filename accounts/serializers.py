from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    has_pro = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "github_login", "is_staff", "plan", "has_pro"]
        read_only_fields = ["id", "email", "is_staff", "plan", "has_pro"]

    def validate_github_login(self, value):
        return value.strip().lstrip("@")


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "password"]
        read_only_fields = ["id"]

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value.lower()

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)
