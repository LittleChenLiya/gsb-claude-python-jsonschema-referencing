"""
Tests for `referencing.tracing` -- explicit resolution traces.

Run locally with:

    python -m pytest referencing/tests/test_tracing.py -q
"""

import pytest

from referencing import Registry, Resource, exceptions
from referencing.jsonschema import DRAFT202012
from referencing.tracing import ResolutionTrace, TraceHop


def root_resource(**defs):
    return Resource.from_contents(
        {"$id": "https://example.com/root", "$defs": defs},
        default_specification=DRAFT202012,
    )


class TestTraceHop:
    def test_records_identities_not_document_values(self):
        """
        A trace hop contains only URIs and fragments, never contents.
        """
        root = root_resource(foo={"type": "integer"})
        resolver = Registry().resolver_with_root(root)

        trace = ResolutionTrace()
        resolver.lookup("#/$defs/foo", trace=trace)

        (hop,) = trace.hops
        assert isinstance(hop, TraceHop)
        assert isinstance(hop.base_uri, str)
        assert isinstance(hop.ref, str)
        assert isinstance(hop.fragment, str)
        assert hop.target is None or isinstance(hop.target, str)
        assert all(isinstance(each, str) for each in hop.dynamic_scope)

    def test_repr(self):
        hop = TraceHop(
            base_uri="https://example.com/root",
            ref="#/$defs/foo",
            fragment="/$defs/foo",
            target="https://example.com/root",
        )
        assert repr(hop) == (
            "<TraceHop '#/$defs/foo' from 'https://example.com/root' "
            "to 'https://example.com/root'>"
        )


class TestResolutionTrace:
    def test_empty(self):
        trace = ResolutionTrace()
        assert len(trace) == 0
        assert trace.hops == ()
        assert trace.cycles == 0
        assert trace.missing == ()
        assert list(trace) == []
        assert repr(trace) == "<ResolutionTrace (0 hops, 0 cycles)>"

    def test_two_hops_root_to_defs_to_anchor(self):
        """
        Root to a $defs subschema to an anchor records two hops, each
        with the accurate base URI in effect.
        """
        root = root_resource(foo={"$anchor": "bar", "type": "integer"})
        resolver = Registry().resolver_with_root(root)

        trace = ResolutionTrace()
        first = resolver.lookup("#/$defs/foo", trace=trace)
        second = first.resolver.lookup("#bar", trace=trace)

        assert first.contents == {"$anchor": "bar", "type": "integer"}
        assert second.contents == {"$anchor": "bar", "type": "integer"}

        assert len(trace.hops) == 2
        hop1, hop2 = trace.hops

        assert hop1.base_uri == "https://example.com/root"
        assert hop1.ref == "#/$defs/foo"
        assert hop1.fragment == "/$defs/foo"
        assert hop1.target == "https://example.com/root"
        assert hop1.dynamic_scope == ()

        assert hop2.base_uri == "https://example.com/root"
        assert hop2.ref == "#bar"
        assert hop2.fragment == "bar"
        assert hop2.target == "https://example.com/root"

        assert trace.cycles == 0
        assert trace.missing == ()

    def test_nested_id_changes_base_visible(self):
        """
        A nested $id changes the base URI, and the change is visible in
        the trace, both as the hop's target and as the next hop's base.
        """
        root = root_resource(
            nested={
                "$id": "nested/",
                "$defs": {"inner": {"$anchor": "innerAnchor"}},
            },
        )
        resolver = Registry().resolver_with_root(root)

        trace = ResolutionTrace()
        first = resolver.lookup("#/$defs/nested", trace=trace)
        second = first.resolver.lookup("#innerAnchor", trace=trace)

        assert second.contents == {"$anchor": "innerAnchor"}

        hop1, hop2 = trace.hops
        assert hop1.base_uri == "https://example.com/root"
        assert hop1.target == "https://example.com/nested/"

        assert hop2.base_uri == "https://example.com/nested/"
        assert hop2.ref == "#innerAnchor"
        assert hop2.fragment == "innerAnchor"
        assert hop2.target == "https://example.com/nested/"

    def test_missing_target_keeps_completed_chain(self):
        """
        A reference to a missing resource records a targetless hop, and
        the hops completed before it are retained.
        """
        root = root_resource(foo={"type": "integer"})
        resolver = Registry().resolver_with_root(root)

        trace = ResolutionTrace()
        first = resolver.lookup("#/$defs/foo", trace=trace)
        with pytest.raises(exceptions.Unresolvable) as excinfo:
            first.resolver.lookup(
                "https://example.com/missing.json",
                trace=trace,
            )

        assert excinfo.value.ref == "https://example.com/missing.json"

        assert len(trace.hops) == 2
        completed, failed = trace.hops
        assert completed.target == "https://example.com/root"

        assert failed.base_uri == "https://example.com/root"
        assert failed.ref == "https://example.com/missing.json"
        assert failed.fragment == ""
        assert failed.target is None

        assert trace.missing == (failed,)

    def test_missing_pointer_and_anchor_recorded(self):
        """
        Pointers to nowhere and missing anchors are recorded as
        targetless hops too, keeping the earlier chain intact.
        """
        root = root_resource(foo={"type": "integer"})
        resolver = Registry().resolver_with_root(root)

        trace = ResolutionTrace()
        resolver.lookup("#/$defs/foo", trace=trace)
        with pytest.raises(exceptions.PointerToNowhere):
            resolver.lookup("#/$defs/nonexistent", trace=trace)
        with pytest.raises(exceptions.NoSuchAnchor):
            resolver.lookup("#no-such-anchor", trace=trace)

        assert len(trace.hops) == 3
        assert [hop.target for hop in trace.hops] == [
            "https://example.com/root",
            None,
            None,
        ]
        assert trace.missing == trace.hops[1:]

    def test_circular_references_do_not_grow_trace(self):
        """
        Circularly resolving the same references records each distinct
        resolution once and counts the repetitions as cycles.
        """
        a = Resource.opaque(contents={"link": "b"})
        b = Resource.opaque(contents={"link": "a"})
        registry = Registry().with_resources(
            [
                ("https://example.com/a.json", a),
                ("https://example.com/b.json", b),
            ],
        )

        trace = ResolutionTrace()
        resolver = registry.resolver("https://example.com/a.json")
        for _ in range(4):
            resolver = resolver.lookup("b.json", trace=trace).resolver
            resolver = resolver.lookup("a.json", trace=trace).resolver

        assert len(trace.hops) == 2
        assert trace.cycles == 6

        hop1, hop2 = trace.hops
        assert hop1.base_uri == "https://example.com/a.json"
        assert hop1.target == "https://example.com/b.json"
        assert hop2.base_uri == "https://example.com/b.json"
        assert hop2.target == "https://example.com/a.json"

    def test_dynamic_scope_identity_preserved(self):
        """
        Resolving a dynamic anchor records the real dynamic scope of the
        resolver, and the identity of the resource the scope selects.
        """
        one = Resource.from_contents(
            {
                "$id": "https://example.com/one",
                "$dynamicAnchor": "dyn",
                "name": "one",
            },
            default_specification=DRAFT202012,
        )
        two = Resource.from_contents(
            {
                "$id": "https://example.com/two",
                "$dynamicAnchor": "dyn",
                "name": "two",
            },
            default_specification=DRAFT202012,
        )
        registry = Registry().with_resources(
            [
                ("https://example.com/one", one),
                ("https://example.com/two", two),
            ],
        )

        trace = ResolutionTrace()
        resolver = registry.resolver()
        first = resolver.lookup("https://example.com/one", trace=trace)
        second = first.resolver.lookup("two", trace=trace)
        third = second.resolver.lookup("#dyn", trace=trace)

        # The dynamic anchor resolves to the resource from the scope.
        assert third.contents == one.contents

        hop1, hop2, hop3 = trace.hops
        assert hop1.dynamic_scope == ()
        assert hop2.dynamic_scope == ()

        assert hop3.base_uri == "https://example.com/two"
        assert hop3.fragment == "dyn"
        assert hop3.dynamic_scope == ("https://example.com/one",)
        assert hop3.target == "https://example.com/one"

    def test_three_hop_chain(self):
        """
        A three-hop chain: root to a $defs subschema, to another
        resource, to an anchor within it.
        """
        root = root_resource(foo={"type": "integer"})
        other = Resource.from_contents(
            {
                "$id": "https://example.com/other.json",
                "$anchor": "thing",
                "type": "string",
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

        assert first.contents == {"type": "integer"}
        assert third.contents == other.contents

        assert len(trace.hops) == 3
        assert [hop.base_uri for hop in trace] == [
            "https://example.com/root",
            "https://example.com/root",
            "https://example.com/other.json",
        ]
        assert [hop.ref for hop in trace] == [
            "#/$defs/foo",
            "other.json",
            "#thing",
        ]
        assert [hop.fragment for hop in trace] == [
            "/$defs/foo",
            "",
            "thing",
        ]
        assert [hop.target for hop in trace] == [
            "https://example.com/root",
            "https://example.com/other.json",
            "https://example.com/other.json",
        ]

    def test_resolution_results_unchanged_by_tracing(self):
        """
        Tracing does not change what resolution returns, and ``None``
        (the default) behaves as no tracing at all.
        """
        root = root_resource(foo={"$anchor": "bar", "type": "integer"})
        resolver = Registry().resolver_with_root(root)

        traced = resolver.lookup("#/$defs/foo", trace=ResolutionTrace())
        untraced = resolver.lookup("#/$defs/foo")
        explicitly_untraced = resolver.lookup("#/$defs/foo", trace=None)

        assert traced.contents == untraced.contents
        assert traced.resolver == untraced.resolver
        assert untraced.contents == explicitly_untraced.contents
        assert untraced.resolver == explicitly_untraced.resolver
