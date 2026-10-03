import shlex

from django.db.models import Q


def search_sites(sites, query):
    try:
        terms = shlex.split(query)
    except ValueError:
        terms = query.split()
    for term in terms:
        field, separator, value = term.partition(":")
        if separator and field == "id" and value.isdecimal():
            if len(value) > 19 or not 0 < int(value) <= 2**63 - 1:
                return sites.none()
            sites = sites.filter(pk=int(value))
        elif separator and field in {"name", "desc", "description", "user"}:
            lookup = {
                "name": "name__icontains",
                "desc": "description__icontains",
                "description": "description__icontains",
                "user": "users__username__iexact",
            }[field]
            sites = sites.filter(**{lookup: value})
        else:
            sites = sites.filter(
                Q(name__icontains=term)
                | Q(description__icontains=term)
                | Q(users__username__iexact=term)
            )
    return sites.distinct()
