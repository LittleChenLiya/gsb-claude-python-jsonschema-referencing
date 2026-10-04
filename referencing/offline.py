"""
Offline construction of closed registries.

This module implements an *offline importer*: given a root URI and a fixed
set of candidate resources, it walks every reference reachable from the root
-- following each resource's own specification (dialect) to find
subresources, anchors and (relative) references -- and produces a `Closure`:
a manifest of exactly the resources reachable from the root, bundled into a
`Registry` which is guaranteed to never touch the network (or any other
retrieval mechanism) afterwards.

References may form cycles; each distinct resource is scanned exactly once,
so traversal always terminates.

The import is all-or-nothing: if any reference points at a resource which
was not provided, or at an anchor or JSON pointer which does not exist
within a reachable resource, `OfflineImportError` is raised describing *all*
such problems at once (along with the referring resource and the location of
each reference within it), and no partial registry is published.

Dynamic references (``$dynamicRef`` and ``$recursiveRef``) are not pretended
to be statically resolved: their static targets are validated like any other
reference, but the closure additionally records a `DynamicReference` note
for each of them, describing the dynamic-scope constraint which still
applies when the reference is resolved at evaluation time.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any, Generic
from urllib.parse import unquote, urldefrag, urljoin

from attrs import field

from referencing import Registry, Resource, Specification
from referencing._attrs import frozen
from referencing.typing import URI, D

__all__ = [
    "Closure",
    "DynamicReference",
    "MissingReference",
    "OfflineImportError",
    "close_over",
]

#: Kinds of references which may be encountered while scanning.
_STATIC = "static"
_DYNAMIC = "dynamic"
_RECURSIVE = "recursive"

#: Reasons a `MissingReference` may report.
_NO_SUCH_RESOURCE = "no such resource"
_NO_SUCH_ANCHOR = "no such anchor"
_POINTER_TO_NOWHERE = "pointer to nowhere"


def _reference_keywords(
    specification: Specification[Any],
) -> dict[str, str]:
    """
    Determine the reference keywords of a specification, mapped to kind.

    JSON Schema dialects always use ``$ref``; draft 2020-12 adds the dynamic
    ``$dynamicRef`` and draft 2019-09 the (dynamic) ``$recursiveRef``. Opaque
    resources contain no references at all.
    """
    if specification is Specification.OPAQUE:
        return {}
    keywords = {"$ref": _STATIC}
    if specification.name == "draft2020-12":
        keywords["$dynamicRef"] = _DYNAMIC
    elif specification.name == "draft2019-09":
        keywords["$recursiveRef"] = _RECURSIVE
    return keywords


def _escape(segment: str) -> str:
    """
    Escape a single JSON pointer segment.
    """
    return segment.replace("~", "~0").replace("/", "~1")


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str)


@frozen
class MissingReference:
    """
    A single reference which could not be satisfied offline.

    .. attribute:: referrer

        The URI of the resource containing the unsatisfiable reference
        (`None` only when the root resource itself was not provided).

    .. attribute:: location

        A JSON pointer to the reference keyword within the referrer
        (e.g. ``/properties/foo/$ref``), or the empty string when the
        missing resource is the root itself.

    .. attribute:: ref

        The reference, exactly as written.

    .. attribute:: resolved

        The absolute URI (without fragment) the reference resolved to
        against its base URI.

    .. attribute:: reason

        One of ``"no such resource"``, ``"no such anchor"`` or
        ``"pointer to nowhere"``.
    """

    referrer: URI | None
    location: str
    ref: str
    resolved: URI
    reason: str = _NO_SUCH_RESOURCE

    def __str__(self) -> str:
        referrer = self.referrer or "<root>"
        location = self.location or "<root>"
        return (
            f"{referrer} ({location}): "
            f"{self.ref!r} -> {self.resolved!r} ({self.reason})"
        )


@frozen
class OfflineImportError(Exception):
    """
    A root-reachable closure could not be constructed offline.

    All unsatisfiable references are reported at once via
    `OfflineImportError.missing`; no partial registry is published.
    """

    missing: tuple[MissingReference, ...]

    def __str__(self) -> str:
        count = len(self.missing)
        lines = [f"{count} reference(s) could not be satisfied offline:"]
        lines.extend(f"  - {each}" for each in self.missing)
        return "\n".join(lines)


@frozen
class DynamicReference:
    """
    A dynamic reference found while closing over a set of resources.

    Dynamic references are *not* fully resolved statically: the closure
    guarantees only their static target, and records the dynamic-scope
    constraint which still applies at evaluation time.

    .. attribute:: kind

        Either ``"dynamic"`` (``$dynamicRef``) or ``"recursive"``
        (``$recursiveRef``).
    """

    referrer: URI
    location: str
    ref: str
    kind: str
    constraint: str


@frozen
class _Reference:
    """
    A reference encountered while scanning a resource.
    """

    referrer: URI
    location: str
    ref: str
    kind: str
    base: URI


@frozen
class _Scan:
    """
    The result of scanning a single resource.
    """

    references: tuple
    anchors: dict
    provided: dict
    root_base: URI


def _scan(uri: URI, resource: Resource[D]) -> _Scan:
    """
    Walk a resource, following its own dialect.

    Collect the references it contains, the anchors it defines (keyed by the
    base URI they belong to, accounting for nested identifiers), and the
    URIs it provides (its own URI plus any nested identifiers).
    """
    specification = resource._specification
    references = []
    anchors = {}
    provided = {uri: resource.contents}
    root_base = uri

    def visit_schema(contents, specification, base, pointer):
        nonlocal root_base
        if not isinstance(contents, Mapping):
            return

        node_specification = specification.detect(contents)

        id = node_specification.id_of(contents)
        if id is not None:
            base = urldefrag(urljoin(base, id))[0]
            provided[base] = contents
            if pointer == "":
                root_base = base

        for each in node_specification.anchors_in(contents):
            anchors.setdefault(base, set()).add(each.name)

        for keyword, kind in _reference_keywords(node_specification).items():
            ref = contents.get(keyword)
            if isinstance(ref, str):
                references.append(
                    _Reference(
                        referrer=uri,
                        location=f"{pointer}/{keyword}",
                        ref=ref,
                        kind=kind,
                        base=base,
                    ),
                )

        subresources = list(node_specification.subresources_of(contents))

        def is_subresource(value):
            return any(value is each for each in subresources)

        for key, value in contents.items():
            child = f"{pointer}/{_escape(key)}"
            if isinstance(value, Mapping):
                if is_subresource(value):
                    visit_schema(value, node_specification, base, child)
                else:  # perhaps a mapping whose *values* are subresources
                    for inner_key, inner in value.items():
                        if is_subresource(inner):
                            visit_schema(
                                inner,
                                node_specification,
                                base,
                                f"{child}/{_escape(inner_key)}",
                            )
            elif _is_sequence(value):
                for index, inner in enumerate(value):
                    if is_subresource(inner):
                        visit_schema(
                            inner,
                            node_specification,
                            base,
                            f"{child}/{index}",
                        )

    visit_schema(resource.contents, specification, uri, "")
    return _Scan(
        references=tuple(references),
        anchors=anchors,
        provided=provided,
        root_base=root_base,
    )


def _pointer_exists(contents: Any, pointer: str) -> bool:
    """
    Check whether a JSON pointer fragment resolves within given contents.
    """
    for segment in unquote(pointer[1:]).split("/"):
        if isinstance(contents, Mapping):
            segment = segment.replace("~1", "/").replace("~0", "~")
        elif _is_sequence(contents):
            try:
                segment = int(segment)
            except ValueError:
                return False
        else:
            return False
        try:
            contents = contents[segment]
        except LookupError:
            return False
    return True


@frozen
class Closure(Generic[D]):
    """
    A closed, offline manifest of the resources reachable from a root URI.

    .. attribute:: root

        The root URI the closure was built from.

    .. attribute:: registry

        A `Registry` containing exactly the validated, root-reachable
        resources. It has no retrieval function configured, so resolution
        against it can never access the network.

    .. attribute:: dynamic

        Notes about each dynamic reference encountered. These references are
        guaranteed only as far as their static targets; their final
        resolution still depends on the dynamic scope at evaluation time.

    The closure itself also behaves like a read-only manifest: iterating it
    yields the URIs of the resources it contains, and ``in`` / ``len`` work
    as expected.
    """

    root: URI
    registry: Registry[D]
    dynamic: tuple[DynamicReference, ...] = field(factory=tuple)

    def __iter__(self) -> Iterator[URI]:
        """
        Iterate over the URIs of resources in this closure.
        """
        return iter(self.registry)

    def __len__(self) -> int:
        """
        Count the resources in this closure.
        """
        return len(self.registry)

    def __contains__(self, uri: URI) -> bool:
        """
        Check whether the given URI is part of this closure.
        """
        return uri.rstrip("#") in self.registry._resources


def close_over(
    root: URI,
    resources: Iterable[tuple[URI, Resource[D]]] | Mapping[URI, Resource[D]],
) -> Closure[D]:
    """
    Close over the resources reachable from ``root``, fully offline.

    Arguments:

        root:

            the URI of the root resource to start from

        resources:

            the candidate resources (an iterable of ``(uri, resource)``
            pairs, or a mapping from URIs to resources -- a `Registry`
            works too). No others will be used or retrieved.

    Returns:

        a `Closure` containing exactly the resources reachable from the
        root, usable as a fully offline `Registry`

    Raises:

        `OfflineImportError`

            if the root or any reference reachable from it cannot be
            satisfied by the given resources; *all* such problems are
            reported at once, and no partial result is produced

    """
    if isinstance(resources, Registry):
        resources = resources.crawl()
    if isinstance(resources, Mapping):
        resources = resources.items()
    known = {uri.rstrip("#"): resource for uri, resource in resources}

    root = root.rstrip("#")
    if root not in known:
        raise OfflineImportError(
            missing=(
                MissingReference(
                    referrer=None,
                    location="",
                    ref=root,
                    resolved=root,
                ),
            ),
        )

    # First pass: discover every reachable resource, scanning each exactly
    # once (references may form cycles, which the `reachable` set absorbs).
    reachable: dict[URI, Resource[D]] = {}
    references: list[_Reference] = []
    anchors: dict[URI, set[str]] = {}
    provided: dict[URI, Any] = {}
    queue = [root]
    while queue:
        uri = queue.pop()
        if uri in reachable:
            continue
        resource = reachable[uri] = known[uri]

        scan = _scan(uri, resource)
        references.extend(scan.references)
        provided.update(scan.provided)
        for base, names in scan.anchors.items():
            anchors.setdefault(base, set()).update(names)
        # Aliasing: a resource stored under one URI but identifying as
        # another still exposes its anchors under both (as `Registry` does).
        if scan.root_base != uri:
            anchors.setdefault(uri, set()).update(
                scan.anchors.get(scan.root_base, set()),
            )

        for reference in scan.references:
            target, _ = urldefrag(urljoin(reference.base, reference.ref))
            if target in known and target not in reachable:
                queue.append(target)

    # Second pass: every reference must now be satisfiable from within the
    # reachable set (and only from it -- nothing may be retrieved).
    missing: list[MissingReference] = []
    dynamic: list[DynamicReference] = []
    for reference in references:
        absolute = urljoin(reference.base, reference.ref)
        target, fragment = urldefrag(absolute)

        reason = None
        if target not in provided:
            reason = _NO_SUCH_RESOURCE
        elif fragment.startswith("/"):
            if not _pointer_exists(provided[target], fragment):
                reason = _POINTER_TO_NOWHERE
        elif fragment and fragment not in anchors.get(target, set()):
            reason = _NO_SUCH_ANCHOR
        if reason is not None:
            missing.append(
                MissingReference(
                    referrer=reference.referrer,
                    location=reference.location,
                    ref=reference.ref,
                    resolved=target,
                    reason=reason,
                ),
            )

        if reference.kind != _STATIC:
            dynamic.append(
                DynamicReference(
                    referrer=reference.referrer,
                    location=reference.location,
                    ref=reference.ref,
                    kind=reference.kind,
                    constraint=(
                        f"{reference.kind} reference: only the static target "
                        f"{absolute!r} is guaranteed by this closure; final "
                        "resolution still depends on the runtime dynamic "
                        "scope, whose outer scopes may provide an overriding "
                        "anchor of the same name"
                    ),
                ),
            )

    if missing:
        raise OfflineImportError(missing=tuple(missing))

    registry = (
        Registry()
        .with_resources((uri, reachable[uri]) for uri in reachable)
        .crawl()
    )
    return Closure(root=root, registry=registry, dynamic=tuple(dynamic))
