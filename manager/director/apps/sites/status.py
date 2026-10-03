"""Shared dashboard information for HTTP and websocket updates."""

from .models import Operation, Site


def normalize_dashboard_tab(tab: str | None) -> str:
    return "settings" if tab == "settings" else "overview"


def status_context(site: Site, user, *, dashboard_tab: str = "overview") -> dict:
    operation = Operation.objects.filter(site=site).prefetch_related("action_set").first()
    return {
        "site": site,
        "user": user,
        "operation": operation,
        "actions": list(operation.list_actions_in_order()) if operation else [],
        "can_edit": user.is_superuser or site.availability in {"enabled", "not-served"},
        "dashboard_tab": normalize_dashboard_tab(dashboard_tab),
    }
