"""
Template context shared by every authenticated page.

docs/08_UI_UX.md: the sidebar is filtered by role server-side, so a Bursar's
rendered HTML contains no User Management markup at all. That means the entries
have to be absent from the context, not wrapped in `{% if %}` — a template
condition still ships the field names to anyone reading the source.
"""

from django.urls import NoReverseMatch, reverse

from core.permissions import visible_modules


def _built(module):
    """Whether this module's view exists yet.

    The permission matrix is complete — every module from
    docs/04_Permission_Matrix.md is in MODULE_ACCESS from the start, because a
    half-filled permission table is worse than none. But the views arrive over
    phases 2-8, and reversing a URL name that has no pattern yet raises
    NoReverseMatch and takes down every authenticated page.

    So the nav renders what is built. This check disappears once phase 8 lands;
    it does not gate access, only whether a link is drawn.
    """
    try:
        reverse(module.url_name)
    except NoReverseMatch:
        return False
    return True


def navigation(request):
    """`nav_modules` and `institution` for base.html.

    Returns nothing for anonymous requests and for PlatformUsers. A Super Admin
    renders the platform templates, which extend a different base with no
    sidebar at all — giving them an empty nav here is the correct answer rather
    than an oversight.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}

    institution = getattr(user, "institution", None)
    return {
        "nav_modules": [m for m in visible_modules(user) if _built(m)],
        "institution": institution,
        "current_role": getattr(user, "role", None),
    }
