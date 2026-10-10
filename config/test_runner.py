from django.conf import settings
from django.test.runner import DiscoverRunner


class Runner(DiscoverRunner):
    """Tests run as flowly.net does, with plans on, so plan gating is covered; instance.tests checks plans off."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        settings.PLANS_ENABLED = True
