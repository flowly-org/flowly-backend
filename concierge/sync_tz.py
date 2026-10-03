from zoneinfo import ZoneInfo


def tz_for(user) -> ZoneInfo:
    from .models import ConciergeProfile

    profile = ConciergeProfile.objects.filter(user=user).first()
    try:
        return ZoneInfo(profile.timezone if profile else "Europe/Rome")
    except Exception:
        return ZoneInfo("UTC")
