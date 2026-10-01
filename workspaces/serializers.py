from django.contrib.auth import get_user_model
from rest_framework import serializers

from accounts.serializers import UserSerializer

from .models import Invitation, Membership, Workspace

User = get_user_model()


class WorkspaceSerializer(serializers.ModelSerializer):
    my_role = serializers.SerializerMethodField()
    member_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Workspace
        fields = ["id", "name", "color", "created_at", "my_role", "member_count"]
        read_only_fields = ["id", "created_at"]

    def get_my_role(self, obj):
        user = self.context["request"].user
        role = getattr(obj, "my_role", None)
        if role is None:
            membership = obj.memberships.filter(user=user).first()
            role = membership.role if membership else None
        return role


class MembershipSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "user", "role", "joined_at"]
        read_only_fields = ["id", "user", "joined_at"]

    def validate_role(self, value):
        if value == Membership.Role.OWNER:
            raise serializers.ValidationError("Ownership can't be assigned through this endpoint.")
        return value


class InvitationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invitation
        fields = ["id", "email", "role", "created_at"]
        read_only_fields = ["id", "created_at"]


class AddMemberSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(
        choices=[Membership.Role.ADMIN, Membership.Role.MEMBER], default=Membership.Role.MEMBER
    )
