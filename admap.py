"""The AD-group to RADIUS-group mapping.

Kept in a JSON file rather than the database because CLAUDE.md forbids adding
tables or columns to the FreeRADIUS schema, and this is application
configuration rather than RADIUS data. FreeRADIUS reads the same file through
deploy/ad-policy during a request, so both sides agree without a second
credential or a second source of truth.

The order matters: a user is frequently in several mapped groups, and the first
match wins. That makes the outcome predictable and lets an operator put
"Contractors" above "Domain Users" to mean "the narrower rule applies".

Nothing here is secret -- AD group names and RADIUS group names -- so the file
is world-readable, which is what lets the unprivileged FreeRADIUS process read
it without a special group.
"""
import json
import os
import tempfile

MAP_FILE = os.environ.get(
    "AD_GROUP_MAP",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ad_group_map.json"),
)


class MappingError(Exception):
    pass


def load():
    """Return the mapping as an ordered list of {ad_group, radius_group}.

    A missing or unreadable file means "no mappings", never an exception: a
    broken file must not take the users page down.
    """
    try:
        with open(MAP_FILE) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []

    out = []
    for row in data.get("mappings", []):
        ad = (row.get("ad_group") or "").strip()
        radius = (row.get("radius_group") or "").strip()
        if ad and radius:
            out.append({"ad_group": ad, "radius_group": radius})
    return out


def save(mappings):
    """Write atomically, so a crash mid-write cannot leave a truncated file
    that FreeRADIUS then reads during an authentication."""
    payload = {
        "_comment": "Managed by the Directory groups page. First match wins.",
        "mappings": [
            {"ad_group": m["ad_group"], "radius_group": m["radius_group"]}
            for m in mappings
        ],
    }
    directory = os.path.dirname(MAP_FILE) or "."
    handle = tempfile.NamedTemporaryFile(
        "w", dir=directory, prefix=".ad_group_map.", delete=False
    )
    try:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(handle.name, 0o644)  # FreeRADIUS reads this as its own user
        os.replace(handle.name, MAP_FILE)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def add(ad_group, radius_group):
    ad_group = (ad_group or "").strip()
    radius_group = (radius_group or "").strip()
    if not ad_group:
        raise MappingError("Pick a directory group")
    if not radius_group:
        raise MappingError("Pick a RADIUS group to map it to")

    mappings = load()
    if any(m["ad_group"].lower() == ad_group.lower() for m in mappings):
        raise MappingError("'%s' is already mapped" % ad_group)
    mappings.append({"ad_group": ad_group, "radius_group": radius_group})
    save(mappings)


def remove(ad_group):
    mappings = [m for m in load() if m["ad_group"].lower() != ad_group.lower()]
    save(mappings)


def move(ad_group, direction):
    """Shift one mapping up or down, changing which rule wins."""
    mappings = load()
    index = next(
        (i for i, m in enumerate(mappings)
         if m["ad_group"].lower() == ad_group.lower()),
        None,
    )
    if index is None:
        raise MappingError("'%s' is not mapped" % ad_group)
    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(mappings):
        mappings[index], mappings[target] = mappings[target], mappings[index]
        save(mappings)


def resolve(user_ad_groups):
    """First mapped RADIUS group for a user's AD group list, or None.

    Comparison is case-insensitive because winbind reports group names
    lowercased while AD displays them in mixed case.
    """
    have = {g.strip().lower() for g in user_ad_groups if g.strip()}
    for mapping in load():
        if mapping["ad_group"].lower() in have:
            return mapping["radius_group"]
    return None
