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
#: Names of the built-in groups and their default tags, per language.
DEFAULT_NAMES = {
    "en": ("Status", "Priority", ["To do", "In progress", "Done"], ["Low", "Medium", "High"]),
    "it": ("Stato", "Priorità", ["Da fare", "In corso", "Fatto"], ["Bassa", "Media", "Alta"]),
    "es": ("Estado", "Prioridad", ["Por hacer", "En curso", "Hecho"], ["Baja", "Media", "Alta"]),
    "fr": ("Statut", "Priorité", ["À faire", "En cours", "Terminé"], ["Basse", "Moyenne", "Haute"]),
    "de": ("Status", "Priorität", ["Zu erledigen", "In Arbeit", "Erledigt"], ["Niedrig", "Mittel", "Hoch"]),
    "pt": ("Estado", "Prioridade", ["A fazer", "Em curso", "Feito"], ["Baixa", "Média", "Alta"]),
}


def _free_name(TagGroup, workspace, name):
    candidate, n = name, 1
    while TagGroup.objects.filter(workspace=workspace, name__iexact=candidate).exists():
        n += 1
        candidate = f"{name} {n}"
    return candidate


def create_system_groups(workspace, TagGroup=None, Tag=None, language="en"):
    """Create the STATUS and PRIORITY groups with default tags.

    Model classes can be passed in so data migrations can use historical models.
    Returns (status_tags, priority_tags) as lists in position order.
    """
    if TagGroup is None or Tag is None:
        from .models import Tag, TagGroup

    status_name, priority_name, status_names, priority_names = DEFAULT_NAMES.get(language, DEFAULT_NAMES["en"])
    status_group = TagGroup.objects.create(
        workspace=workspace, name=_free_name(TagGroup, workspace, status_name), kind="status", position=-2
    )
    priority_group = TagGroup.objects.create(
        workspace=workspace, name=_free_name(TagGroup, workspace, priority_name), kind="priority", position=-1
    )
    statuses = [
        Tag.objects.create(
            workspace=workspace, group=status_group, name=label, color=color, is_done=done, position=i
        )
        for i, ((_, color, done), label) in enumerate(zip(DEFAULT_STATUSES, status_names))
    ]
    priorities = [
        Tag.objects.create(workspace=workspace, group=priority_group, name=label, color=color, position=i)
        for i, ((_, color), label) in enumerate(zip(DEFAULT_PRIORITIES, priority_names))
    ]
    return statuses, priorities


def default_status(workspace):
    from .models import Tag

    tags = Tag.objects.filter(workspace=workspace, group__kind="status")
    return tags.filter(is_done=False).first() or tags.first()
