from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from posixpath import normpath
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET
from markdown_it import MarkdownIt

from .governance import guidelines_exempt

MAX_DOCUMENTS = 200
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_QUERY_CHARACTERS = 200
TOCTREE = re.compile(r"^```\{toctree\}[^\n]*\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


def _docs_root() -> Path:
    default = Path(__file__).resolve().parents[4] / "docs" / "source"
    return Path(getattr(settings, "DIRECTOR_DOCS_ROOT", default)).resolve()


def _document_path(page: str) -> Path:
    if not page:
        page = "index"
    if "\\" in page or "\x00" in page:
        raise Http404
    parts = PurePosixPath(page).parts
    if any(part in {"..", ".", "/"} or part.startswith((".", "_")) for part in parts):
        raise Http404
    relative = PurePosixPath(page)
    if relative.is_absolute() or relative.suffix not in {"", ".md"}:
        raise Http404
    root = _docs_root()
    candidate = root.joinpath(*relative.parts)
    if not relative.suffix:
        candidate = candidate.with_suffix(".md")
        if not candidate.is_file():
            candidate = root.joinpath(*relative.parts, "index.md")
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
        if not resolved.is_file() or resolved.stat().st_size > MAX_DOCUMENT_BYTES:
            raise Http404
    except (OSError, ValueError):
        raise Http404 from None
    return resolved


def _read_document(path: Path) -> tuple[str, str]:
    try:
        with path.open(encoding="utf-8") as file:
            text = file.read(MAX_DOCUMENT_BYTES + 1)
        if len(text) > MAX_DOCUMENT_BYTES:
            raise Http404
    except (OSError, UnicodeError):
        raise Http404 from None
    match = re.search(r"^#\s+(.+?)\s*#*\s*$", text, re.MULTILINE)
    title = match.group(1) if match else path.stem.replace("-", " ").title()
    if match:
        text = text[: match.start()] + text[match.end() :]
    return title, text


def _page_name(path: Path) -> str:
    relative = path.relative_to(_docs_root()).with_suffix("").as_posix()
    if relative.endswith("/index"):
        relative = relative[: -len("/index")]
    return relative


def _page_url(page: str) -> str:
    return (
        reverse("sites:docs-index") if page == "index" else reverse("sites:docs-page", args=[page])
    )


def _documents():
    paths = []
    root = _docs_root()
    for candidate in root.rglob("*.md"):
        try:
            relative = candidate.relative_to(root)
            path = _document_path(relative.as_posix())
        except (Http404, ValueError):
            continue
        paths.append(path)
        if len(paths) >= MAX_DOCUMENTS:
            break
    for path in sorted(set(paths)):
        try:
            title, content = _read_document(path)
        except Http404:
            continue
        page = _page_name(path)
        yield {"page": page, "title": title, "text": content, "url": _page_url(page)}


def _local_doc_page(href: str, current_page: str) -> str | None:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
        return None
    relative = normpath(PurePosixPath(current_page).parent.joinpath(parsed.path).as_posix())
    try:
        path = _document_path(relative)
    except Http404:
        return None
    target = _page_url(_page_name(path))
    return target + (f"#{parsed.fragment}" if parsed.fragment else "")


def _render_document(content: str, page: str) -> str:
    def toctree_links(match):
        entries = []
        in_metadata = False
        for raw_line in match.group(1).splitlines():
            line = raw_line.strip()
            if line == "---":
                in_metadata = not in_metadata
                continue
            if not line or in_metadata or line.startswith(":"):
                continue
            target = _local_doc_page(line, page)
            if target:
                label = line.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()
                entries.append(f"- [{label}]({target})")
        return "\n" + "\n".join(entries) + "\n"

    markdown = MarkdownIt("commonmark", {"html": False, "linkify": False})
    tokens = markdown.parse(TOCTREE.sub(toctree_links, content))
    for block in tokens:
        for token in block.children or []:
            if token.type == "link_open":
                target = _local_doc_page(token.attrGet("href") or "", page)
                if target:
                    token.attrSet("href", target)
    return markdown.renderer.render(tokens, markdown.options, {})


def _page_context(page: str):
    path = _document_path(page)
    title, content = _read_document(path)
    canonical_page = _page_name(path)
    return {
        "doc_title": title,
        "doc_html": _render_document(content, canonical_page),
        "docs": [{key: item[key] for key in ("title", "url")} for item in _documents()],
    }


@guidelines_exempt
@login_required
@require_GET
def docs_index(request):
    return render(request, "sites/docs/page.html", _page_context("index"))


@guidelines_exempt
@login_required
@require_GET
def docs_page(request, page):
    if page.endswith(".md"):
        return redirect(_page_url(_page_name(_document_path(page))))
    return render(request, "sites/docs/page.html", _page_context(page))


@guidelines_exempt
@login_required
@require_GET
def docs_search(request):
    query = request.GET.get("q", "").strip()
    context = {"q": query, "results": []}
    if len(query) > MAX_QUERY_CHARACTERS:
        context["error"] = "Search documentation with at most 200 characters."
        return render(request, "sites/docs/search.html", context, status=400)
    if query:
        words = query.casefold().split()[:20]
        results = []
        for item in _documents():
            title = item["title"].casefold()
            content = item["text"].casefold()
            rank = sum(title.count(word) * 2 + content.count(word) for word in words)
            if rank:
                results.append({"title": item["title"], "url": item["url"], "rank": rank})
        context["results"] = sorted(results, key=lambda item: (-item["rank"], item["title"]))[:50]
    return render(request, "sites/docs/search.html", context)
