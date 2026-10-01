DEFAULT_STATUSES = [
    # name, color, is_done
    ("To do", "#64748b", False),
    ("In progress", "#d97706", False),
    ("Done", "#16a34a", True),
]
DEFAULT_PRIORITIES = [
    ("Low", "#0ea5e9"),
    ("Medium", "#d97706"),
    ("High", "#dc2626"),
]


def _free_name(TagGroup, workspace, name):
    candidate, n = name, 1
    while TagGroup.objects.filter(workspace=workspace, name__iexact=candidate).exists():
        n += 1
        candidate = f"{name} {n}"
    return candidate


def create_system_groups(workspace, TagGroup=None, Tag=None):
    """Create the STATUS and PRIORITY groups with default tags.

    Model classes can be passed in so data migrations can use historical models.
    Returns (status_tags, priority_tags) as lists in position order.
    """
    if TagGroup is None or Tag is None:
        from .models import Tag, TagGroup

    status_group = TagGroup.objects.create(
        workspace=workspace, name=_free_name(TagGroup, workspace, "Status"), kind="status", position=-2
    )
    priority_group = TagGroup.objects.create(
        workspace=workspace, name=_free_name(TagGroup, workspace, "Priority"), kind="priority", position=-1
    )
    statuses = [
        Tag.objects.create(
            workspace=workspace, group=status_group, name=name, color=color, is_done=done, position=i
        )
        for i, (name, color, done) in enumerate(DEFAULT_STATUSES)
    ]
    priorities = [
        Tag.objects.create(workspace=workspace, group=priority_group, name=name, color=color, position=i)
        for i, (name, color) in enumerate(DEFAULT_PRIORITIES)
    ]
    return statuses, priorities


def default_status(workspace):
    from .models import Tag

    tags = Tag.objects.filter(workspace=workspace, group__kind="status")
    return tags.filter(is_done=False).first() or tags.first()
