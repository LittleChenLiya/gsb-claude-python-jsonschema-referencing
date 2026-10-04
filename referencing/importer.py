"""
Offline reference importing.

This module provides functionality for importing a *closed* set of resources
reachable from a root URI, such that subsequent `Registry` resolution never
needs to retrieve resources (e.g. over the network).

Given a collection of `Resource` objects (and/or ``(URI, Resource)`` pairs)
along with a designated root URI, `import_offline` walks the root document,
following subresources and references along each resource's own dialect, and
produces a registry containing exactly the resources reachable from the root.
Cycles are allowed and are traversed only once.

If any reference cannot be resolved using only the given resources, an
`OfflineImportError` is raised aggregating *all* of the missing references,
each reported with its source URI and the location of the reference within
that resource. No partial registry is published.

Dynamic references (e.g. JSON Schema's ``$dynamicRef`` or ``$recursiveRef``)
cannot be fully resolved statically -- their resolution depends on the
dynamic scope at runtime. Such references are recorded as `DynamicReference`
objects describing the dynamic scope constraint, rather than being pretended
to be statically resolvable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Generic
from urllib.parse import urldefrag, urljoin

from referencing import Registry, Resource, exceptions
from referencing._attrs import frozen
from referencing.typing import URI, D

__all__ = [
    "DynamicReference",
    "MissingReference",
    "OfflineImport",
    "OfflineImportError",
    "ReferenceSite",
    "import_offline",
]


@frozen
class ReferenceSite:
    """
    A reference found at a specific location within a resource.

    Attributes:
        resource: The canonical URI of the resource containing the reference.
        path: The location of the reference within that resource, as a
            sequence of JSON-pointer-like segments (relative to the resource
            root).
        ref: The literal reference string, as written in the document.
        base: The base URI against which the reference is resolved.
        dynamic: Whether the reference is a dynamic reference.
        recursive: Whether the reference is a recursive reference.
    """

    resource: URI
    path: tuple[int | str, ...]
    ref: URI
    base: URI
    dynamic: bool = False
    recursive: bool = False


@frozen
class MissingReference:
    """
    A reference which could not be resolved using only the imported resources.

    Attributes:
        site: The location of the reference within its containing resource.
        target: The fully resolved URI which could not be found.
        detail: A human-readable explanation of why the reference is missing.
    """

    site: ReferenceSite
    target: URI
    detail: str


@frozen
class DynamicReference:
    """
    A dynamic reference whose resolution depends on runtime dynamic scope.

    Attributes:
        site: The location of the reference within its containing resource.
        target: The statically resolved target URI.
        description: A description of the dynamic scope constraint -- i.e.
            that the reference may resolve to a different resource at runtime
            depending on the chain of resources that led to this one.
    """

    site: ReferenceSite
    target: URI
    description: str


@frozen
class OfflineImport(Generic[D]):
    """
    The result of a successful offline import.

    Attributes:
        root: The designated root URI.
        registry: A fully crawled registry containing exactly the resources
            reachable from the root, with no retrieval function (so resolution
            can never accidentally access the network).
        resources: The closed set of resources, as
            ``(canonical URI, resource)`` pairs.
        dynamic_references: Dynamic references encountered while importing,
            each describing its dynamic scope constraint.
    """

    root: URI
    registry: Registry[D]
    resources: tuple[tuple[URI, Resource[D]], ...]
    dynamic_references: tuple[DynamicReference, ...]


def _format_path(path: tuple[int | str, ...]) -> str:
    if not path:
        return "#"
    return "#/" + "/".join(str(segment) for segment in path)


class OfflineImportError(Exception):
    """
    Raised when one or more references cannot be resolved offline.

    Aggregates *all* of the missing references so they can be reported in one
    go, each with its source URI and the location of the reference within that
    resource. No partial registry is published.
    """

    def __init__(self, missing: Iterable[MissingReference]):
        self.missing: tuple[MissingReference, ...] = tuple(missing)
        super().__init__(self._message())

    def _message(self) -> str:
        lines = [
            f"{len(self.missing)} reference(s) could not be resolved offline:",
        ]
        for each in self.missing:
            location = _format_path(each.site.path)
            lines.append(
                f"  - {each.site.resource}{location} -> {each.target} "
                f"({each.detail})",
            )
        return "\n".join(lines)


def _normalize(
    resources: Iterable[Resource[D] | tuple[URI, Resource[D]]],
) -> dict[URI, Resource[D]]:
    """
    Normalize the given resources into a URI-keyed map.

    Each resource is registered under both its given (external) URI and its
    internal (self-declared) identifier, if any.
    """
    by_uri: dict[URI, Resource[D]] = {}
    for item in resources:
        if isinstance(item, Resource):
            resource = item
            uri = resource.id()
            if uri is None:
                raise exceptions.NoInternalID(resource=resource)
        else:
            uri, resource = item
        id = resource.id()
        canonical = urljoin(uri, id) if id is not None else uri
        by_uri[uri] = resource
        by_uri[canonical] = resource
    return by_uri


def _walk_tree(
    resource: Resource[D],
    base_uri: URI,
) -> Iterable[tuple[Any, tuple[int | str, ...], URI, Resource[D], URI | None]]:
    """
    Iteratively walk a resource's document tree.

    Yields ``(node, path, base, resource, boundary_uri)`` tuples, where:

    * ``node`` is the document node,
    * ``path`` is its location relative to the containing resource,
    * ``base`` is the base URI to use for references at this node,
    * ``resource`` is the resource whose dialect governs this node, and
    * ``boundary_uri`` is the canonical URI if this node is a subresource with
      its own identifier, else `None`.

    Subresources are detected along the resource's own dialect (via
    `Specification.subresources_of`), and nested ``$id``s change the base
    URI for references within the subresource.
    """
    stack: list[
        tuple[Any, tuple[int | str, ...], URI, Resource[D], set[int]]
    ] = []
    root = resource.contents
    root_subs = {
        id(each) for each in resource._specification.subresources_of(root)
    }
    stack.append(
        (
            root,
            (),
            base_uri,
            resource,
            root_subs,
        ),
    )
    while stack:
        node, path, base, resource, subs = stack.pop()
        if id(node) in subs:
            # This node is a subresource of the enclosing resource.
            child = Resource.from_contents(
                node,
                default_specification=resource._specification,
            )
            child_id = child.id()
            child_subs = {
                id(each)
                for each in child._specification.subresources_of(
                    child.contents,
                )
            }
            if child_id is not None:
                # The subresource declares its own identifier, so it is a new
                # resource: reset the path and change the base URI.
                child_base = urljoin(base, child_id)
                yield node, (), child_base, child, child_base
                if isinstance(node, Mapping):
                    for key, value in node.items():
                        stack.append(
                            (value, (key,), child_base, child, child_subs),
                        )
                elif isinstance(node, list):
                    for index, item in enumerate(node):
                        stack.append(
                            (item, (index,), child_base, child, child_subs),
                        )
            else:
                # The subresource has no identifier of its own, so it is
                # transparent: references within it use the parent's base URI
                # and the path continues.
                yield node, path, base, child, None
                if isinstance(node, Mapping):
                    for key, value in node.items():
                        stack.append(
                            (value, (*path, key), base, child, child_subs),
                        )
                elif isinstance(node, list):
                    for index, item in enumerate(node):
                        stack.append(
                            (item, (*path, index), base, child, child_subs),
                        )
        else:
            yield node, path, base, resource, None
            if isinstance(node, Mapping):
                for key, value in node.items():
                    stack.append(
                        (value, (*path, key), base, resource, subs),
                    )
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    stack.append(
                        (item, (*path, index), base, resource, subs),
                    )


def _dynamic_description(site: ReferenceSite, target: URI) -> str:
    kind = "dynamic" if site.dynamic else "recursive"
    return (
        f"This is a {kind} reference ({site.ref!r}). Its resolution depends "
        f"on the dynamic scope at runtime -- the chain of resources that led "
        f"to this one -- and so may resolve to a different resource than the "
        f"static target {target!r}. The static target must exist, but the "
        f"offline registry does not retrieve external resources, so any "
        f"dynamic target outside the closed set cannot be verified and must "
        f"be supplied by the caller if it is needed."
    )


def import_offline(
    resources: Iterable[Resource[D] | tuple[URI, Resource[D]]],
    root: URI,
) -> OfflineImport[D]:
    """
    Import a closed set of resources reachable from the given root URI.

    Walks the root document, following subresources and references along each
    resource's own dialect, and produces a registry containing exactly the
    resources reachable from the root. Cycles are allowed and are traversed
    only once.

    All references are resolved using only the given resources -- no retrieval
    function is ever called. If any reference cannot be resolved, an
    `OfflineImportError` is raised aggregating *all* of the missing references
    (each with its source URI and the location of the reference within that
    resource), and no partial registry is published.

    Dynamic references (``$dynamicRef`` / ``$recursiveRef``) are recorded as
    `DynamicReference` objects describing their dynamic scope constraint
    rather than being pretended to be statically resolvable.

    Args:
        resources: The resources to import, either as `Resource` objects
            (which must have internal identifiers) or as ``(URI, Resource)``
            pairs.
        root: The designated root URI, which must identify one of the given
            resources.

    Returns:
        An `OfflineImport` containing the closed registry and the dynamic
        references encountered.

    Raises:
        `OfflineImportError`: if one or more references cannot be resolved
            using only the given resources.
    """
    by_uri = _normalize(resources)
    if root not in by_uri:
        raise OfflineImportError(
            [
                MissingReference(
                    site=ReferenceSite(
                        resource=root,
                        path=(),
                        ref="",
                        base=root,
                    ),
                    target=root,
                    detail="root resource not found",
                ),
            ],
        )

    root_resource = by_uri[root]
    root_id = root_resource.id()
    root_canonical = urljoin(root, root_id) if root_id is not None else root

    # Map every known URI of a reachable resource to the resource itself.
    closed: dict[URI, Resource[D]] = {}

    def remember(uri: URI, resource: Resource[D]) -> None:
        for alias, each in by_uri.items():
            if each is resource:
                closed[alias] = resource
        closed[uri] = resource

    def base_for(uri: URI, resource: Resource[D]) -> URI:
        id = resource.id()
        return urljoin(uri, id) if id is not None else uri

    # Iteratively close over reachable resources. Starting from the root,
    # walk each resource's tree (discovering subresources) and follow
    # references to other given resources, walking those too in turn. Cycles
    # are harmless: a resource is only ever walked once.
    remember(root_canonical, root_resource)
    queue: list[tuple[URI, Resource[D]]] = [(root_canonical, root_resource)]
    sites: list[ReferenceSite] = []
    while queue:
        base, resource = queue.pop()
        for node, path, node_base, node_resource, boundary_uri in _walk_tree(
            resource,
            base,
        ):
            if boundary_uri is not None and boundary_uri not in closed:
                remember(boundary_uri, node_resource)
            for ref in node_resource._specification.references_in(node):
                site = ReferenceSite(
                    resource=node_base,
                    path=path,
                    ref=ref.ref,
                    base=node_base,
                    dynamic=ref.dynamic,
                    recursive=ref.recursive,
                )
                sites.append(site)
                resolved = urljoin(site.base, site.ref)
                uri, _ = urldefrag(resolved)
                if uri not in closed and uri in by_uri:
                    target = by_uri[uri]
                    target_base = base_for(uri, target)
                    remember(target_base, target)
                    queue.append((target_base, target))

    # A fully crawled registry for validating fragments. It has no retrieval
    # function, so resolution can never accidentally access the network.
    registry = Registry().with_resources(closed.items()).crawl()

    missing: list[MissingReference] = []
    dynamic: list[DynamicReference] = []
    for site in sites:
        resolved = urljoin(site.base, site.ref)
        uri, fragment = urldefrag(resolved)
        target = closed.get(uri)
        if target is None:
            missing.append(
                MissingReference(
                    site=site,
                    target=resolved,
                    detail="no such resource",
                ),
            )
            continue

        if site.dynamic or site.recursive:
            dynamic.append(
                DynamicReference(
                    site=site,
                    target=resolved,
                    description=_dynamic_description(site, resolved),
                ),
            )

        if fragment.startswith("/"):
            try:
                target.pointer(fragment, registry.resolver(base_uri=uri))
            except exceptions.PointerToNowhere:
                missing.append(
                    MissingReference(
                        site=site,
                        target=resolved,
                        detail="JSON pointer points to nowhere",
                    ),
                )
        elif fragment:
            anchors = {each.name for each in target.anchors()}
            if fragment not in anchors:
                missing.append(
                    MissingReference(
                        site=site,
                        target=resolved,
                        detail=f"no such anchor {fragment!r}",
                    ),
                )

    if missing:
        raise OfflineImportError(missing)

    # Distinct resources under their canonical URIs, sorted for stable output.
    seen: set[int] = set()
    pairs: list[tuple[URI, Resource[D]]] = []
    for uri, resource in closed.items():
        rid = id(resource)
        if rid not in seen:
            seen.add(rid)
            resource_id = resource.id()
            canonical = urljoin(uri, resource_id) if resource_id is not None else uri
            pairs.append((canonical, resource))
    pairs.sort(key=lambda each: each[0])

    return OfflineImport(
        root=root,
        registry=registry,
        resources=tuple(pairs),
        dynamic_references=tuple(dynamic),
    )
