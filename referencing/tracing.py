"""
Explicit resolution tracing for `referencing`.

A `ResolutionTrace` records, *as resolution happens*, each reference a
`Resolver` follows: the base URI in effect, the fragment resolved, and the
identity of the `Resource` ultimately found -- never the document values
themselves, and never by re-performing ("guessing") the lookup a second
time.

Traces are opt-in: pass one to `Resolver.lookup` via its ``trace``
argument. Resolution results are completely unaffected, whether or not a
trace is provided.

A short example, resolving a three-hop chain (root schema, to a
``$defs`` subschema, to another resource, to an anchor within it)::

    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
    from referencing.tracing import ResolutionTrace

    root = Resource.from_contents(
        {
            "$id": "https://example.com/root",
            "$defs": {"foo": {"type": "integer"}},
        },
        default_specification=DRAFT202012,
    )
    other = Resource.from_contents(
        {
            "$id": "https://example.com/other.json",
            "$anchor": "thing",
        },
        default_specification=DRAFT202012,
    )
    registry = Registry().with_resource(
        "https://example.com/other.json",
        other,
    )
    resolver = registry.resolver_with_root(root)

    trace = ResolutionTrace()
    first = resolver.lookup("#/$defs/foo", trace=trace)
    second = first.resolver.lookup("other.json", trace=trace)
    third = second.resolver.lookup("#thing", trace=trace)

    for hop in trace:
        print(hop.base_uri, "->", hop.ref, "=>", hop.target)

The tests for this module can be run locally with::

    python -m pytest referencing/tests/test_tracing.py -q
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from attrs import field

from referencing._attrs import frozen

if TYPE_CHECKING:
    from collections.abc import Iterator

    from referencing.typing import URI

__all__ = ["ResolutionTrace", "TraceHop"]


@frozen
class TraceHop:
    """
    One explicitly recorded step of reference resolution.

    A hop records *identities only* -- the base URI a reference was
    resolved against, the reference and its fragment, the identity of the
    resource found, and the resolver's dynamic scope at the time. It never
    contains document contents, so traces are cheap to keep and safe to
    log.
    """

    #: The base URI in effect when the reference was resolved. For
    #: references resolved from within nested resources (e.g. beneath a
    #: JSON Schema :kw:`$id`), this is the *real* base URI of the
    #: resolver's scope, not necessarily the root resource's URI.
    base_uri: URI

    #: The reference exactly as it was passed to `Resolver.lookup`.
    ref: URI

    #: The fragment portion of the reference (empty if there was none).
    fragment: str

    #: The identity of the resource the reference ultimately resolved to
    #: -- concretely, the base URI of the resolver which would continue
    #: resolution from the result. This reflects nested identifiers (a
    #: nested :kw:`$id` shows up here as a changed base) as well as
    #: dynamic anchors (the dynamic scope's identity is preserved here).
    #:
    #: It is `None` if the target was missing (an unresolvable reference,
    #: a pointer to nowhere, or a nonexistent anchor), in which case all
    #: *earlier* hops in the trace remain recorded.
    target: URI | None

    #: The identities (URIs) in the resolver's dynamic scope when the
    #: reference was resolved, innermost first.
    dynamic_scope: tuple[URI, ...] = field(factory=tuple, converter=tuple)

    def __repr__(self) -> str:
        return (
            f"<TraceHop {self.ref!r} from {self.base_uri!r} "
            f"to {self.target!r}>"
        )


class ResolutionTrace:
    """
    An explicit record of the references a resolver has resolved.

    Pass an instance to each `Resolver.lookup` call via its ``trace``
    argument; every lookup appends one `TraceHop` describing the base URI,
    fragment, and resolved resource identity, recorded inline during the
    lookup itself.

    Circular reference chains cannot grow the trace without bound: a hop
    which exactly repeats an already-recorded resolution (same base URI,
    reference, and target) is not appended again, and is instead counted
    under `ResolutionTrace.cycles`.

    If a reference cannot be resolved, the failed hop is recorded with a
    `None <TraceHop.target>` target and all previously completed hops are
    retained.
    """

    def __init__(self) -> None:
        self._hops: list[TraceHop] = []
        self._seen: set[tuple[URI, URI, URI | None]] = set()
        self._cycles = 0

    def __repr__(self) -> str:
        hops = len(self._hops)
        pluralized = "hop" if hops == 1 else "hops"
        cycles = self._cycles
        return f"<ResolutionTrace ({hops} {pluralized}, {cycles} cycles)>"

    def __iter__(self) -> Iterator[TraceHop]:
        """
        Iterate over the recorded hops, in resolution order.
        """
        return iter(self._hops)

    def __len__(self) -> int:
        """
        Count the number of recorded hops.
        """
        return len(self._hops)

    @property
    def hops(self) -> tuple[TraceHop, ...]:
        """
        The recorded hops, in resolution order.
        """
        return tuple(self._hops)

    @property
    def cycles(self) -> int:
        """
        The number of repeated resolutions suppressed from the trace.

        Re-resolving an identical reference from an identical base URI to
        an identical target (as happens with circular references) is
        counted here rather than appended, keeping the trace finite.
        """
        return self._cycles

    @property
    def missing(self) -> tuple[TraceHop, ...]:
        """
        The recorded hops whose target resource was missing.
        """
        return tuple(hop for hop in self._hops if hop.target is None)

    def record(
        self,
        base_uri: URI,
        ref: URI,
        fragment: str,
        target: URI | None,
        dynamic_scope: tuple[URI, ...] = (),
    ) -> TraceHop | None:
        """
        Record one resolution hop, returning it.

        If an identical hop (same base URI, reference, and target) was
        already recorded, do not append it again; instead count it as a
        cycle and return `None`.
        """
        hop = TraceHop(
            base_uri=base_uri,
            ref=ref,
            fragment=fragment,
            target=target,
            dynamic_scope=dynamic_scope,
        )
        key = (hop.base_uri, hop.ref, hop.target)
        if key in self._seen:
            self._cycles += 1
            return None
        self._seen.add(key)
        self._hops.append(hop)
        return hop
