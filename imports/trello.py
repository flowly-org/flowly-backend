"""Trello board export (Board menu → Print, export and share → Export as JSON) → import items."""

from .engine import parse_date


def read(data: dict) -> tuple[str, str, list[dict]]:
    """→ (board id, board name, items)."""
    if not isinstance(data, dict) or "cards" not in data or "lists" not in data:
        raise ValueError("That isn't a Trello board export (JSON).")
    lists = {l["id"]: l for l in data.get("lists", [])}
    members = {m["id"]: {"name": m.get("fullName") or m.get("username"), "email": ""} for m in data.get("members", [])}
    checklists = {}
    for cl in data.get("checklists", []):
        items = sorted(cl.get("checkItems", []), key=lambda i: i.get("pos", 0))
        checklists.setdefault(cl["idCard"], []).extend({"text": i.get("name", ""), "done": i.get("state") == "complete"} for i in items)
    comments = {}
    for action in data.get("actions", []):
        if action.get("type") == "commentCard":
            card = ((action.get("data") or {}).get("card") or {}).get("id")
            author = (action.get("memberCreator") or {}).get("fullName") or members.get(action.get("idMemberCreator"), {}).get("name")
            comments.setdefault(card, []).append({"author": author, "text": (action.get("data") or {}).get("text", ""), "date": action.get("date")})
    items = []
    for card in data.get("cards", []):
        lst = lists.get(card.get("idList")) or {}
        if card.get("closed") or lst.get("closed"):
            continue  # archived
        assignee = members.get((card.get("idMembers") or [None])[0]) if card.get("idMembers") else None
        items.append({
            "id": card["id"], "title": card.get("name", ""), "notes": card.get("desc", ""),
            "done": bool(card.get("dueComplete")) or bool(card.get("closed")),
            "due": parse_date(card.get("due")), "start": parse_date(card.get("start")),
            "list": lst.get("name", ""), "labels": [{"name": l.get("name") or l.get("color", ""), "color": l.get("color", "")} for l in card.get("labels", [])],
            "checklist": checklists.get(card["id"], []), "assignee": assignee,
            "comments": sorted(comments.get(card["id"], []), key=lambda c: c.get("date") or ""),
        })
    return data.get("id", ""), data.get("name", "Trello board"), items
