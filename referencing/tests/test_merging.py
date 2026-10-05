"""
Tests for `Registry.merge`, the explicit (strict) registry merger.
"""

import attrs
import pytest

from referencing import Registry, Resource, exceptions
from referencing.jsonschema import DRAFT202012
from referencing.tests.test_core import ID_AND_CHILDREN


def counting_retrieve():
    """
    A retrieve function which records how many times it was called.
    """
    calls = []

    def retrieve(uri):
        calls.append(uri)
        raise exceptions.NoSuchResource(ref=uri)

    return retrieve, calls


class TestMerge:
    def test_identical_resources_under_same_uri_merge(self):
        """
        Two registries with the same URI and the same resource merge.
        """
        contents = {"$id": "http://example.com/a", "type": "integer"}
        one = Registry().with_resource(
            "http://example.com/a",
            DRAFT202012.create_resource(contents),
        )
        two = Registry().with_resource(
            "http://example.com/a",
            DRAFT202012.create_resource(dict(contents)),
        )

        merged = one.merge(two)
        assert len(merged) == 1
        assert merged["http://example.com/a"].contents == contents

    def test_disjoint_registries_merge(self):
        one = Registry().with_resource("urn:one", Resource.opaque(contents=1))
        two = Registry().with_resource("urn:two", Resource.opaque(contents=2))

        merged = one.merge(two)
        assert set(merged) == {"urn:one", "urn:two"}
        assert merged["urn:one"].contents == 1
        assert merged["urn:two"].contents == 2

    def test_merging_multiple_registries(self):
        registries = [
            Registry().with_resource(f"urn:{i}", Resource.opaque(contents=i))
            for i in range(3)
        ]
        merged = registries[0].merge(*registries[1:])
        assert set(merged) == {"urn:0", "urn:1", "urn:2"}

    def test_merging_with_oneself_is_a_no_op(self):
        registry = Registry().with_resource(
            "urn:one",
            Resource.opaque(contents=1),
        )
        assert registry.merge(registry) is registry

    def test_differing_contents_conflict(self):
        one = Registry().with_resource("urn:x", Resource.opaque(contents=1))
        two = Registry().with_resource("urn:x", Resource.opaque(contents=2))
        with pytest.raises(exceptions.MergeConflict) as excinfo:
            one.merge(two)
        assert excinfo.value.uri == "urn:x"
        assert excinfo.value.reason == "differing contents"

    def test_changing_an_anchor_conflicts(self):
        """
        Same URI, same dialect, but a changed anchor: conflict.
        """
        uri = "http://example.com/a"
        one = Registry().with_resource(
            uri,
            DRAFT202012.create_resource({"$anchor": "foo"}),
        )
        two = Registry().with_resource(
            uri,
            DRAFT202012.create_resource({"$anchor": "bar"}),
        )
        with pytest.raises(exceptions.MergeConflict) as excinfo:
            one.merge(two)
        assert excinfo.value.uri == uri
        assert excinfo.value.reason == "differing anchors"

    def test_differing_dialects_conflict(self):
        """
        Same URI and same contents, but different dialects: conflict.
        """
        contents = {}
        one = Registry().with_resource("urn:x", Resource.opaque(contents))
        two = Registry().with_resource(
            "urn:x",
            DRAFT202012.create_resource(contents),
        )
        with pytest.raises(exceptions.MergeConflict) as excinfo:
            one.merge(two)
        assert excinfo.value.reason == "differing specifications"

    def test_conflict_report_does_not_copy_resource_values(self):
        """
        The conflict report carries a URI and a reason, never contents.
        """
        secret = {"secret": "swordfish", "with": ["nested", "values"]}
        one = Registry().with_resource("urn:x", Resource.opaque(secret))
        two = Registry().with_resource(
            "urn:x",
            Resource.opaque({"other": "contents"}),
        )
        with pytest.raises(exceptions.MergeConflict) as excinfo:
            one.merge(two)
        error = excinfo.value
        assert "swordfish" not in str(error)
        assert "swordfish" not in repr(error)
        assert "nested" not in repr(error)
        assert {
            field.name for field in attrs.fields(type(error))
        } == {"uri", "reason"}

    def test_empty_fragment_is_the_same_uri(self):
        """
        The URI normalization rule is fixed: ``urn:x#`` is ``urn:x``.
        """
        resource = Resource.opaque(contents=1)
        one = Registry().with_resource("urn:x", resource)
        two = Registry().with_resource("urn:x#", resource)
        assert len(one.merge(two)) == 1

        three = Registry().with_resource(
            "urn:x#",
            Resource.opaque(contents=2),
        )
        with pytest.raises(exceptions.MergeConflict) as excinfo:
            one.merge(three)
        assert excinfo.value.uri == "urn:x"

    def test_merge_does_not_retrieve(self):
        """
        Merging never calls either registry's retrieve function.
        """
        retrieve, calls = counting_retrieve()
        one = Registry(retrieve=retrieve).with_resource(
            "urn:one",
            Resource.opaque(contents=1),
        )
        two = Registry(retrieve=retrieve).with_resource(
            "urn:two",
            Resource.opaque(contents=2),
        )

        merged = one.merge(two)
        assert calls == []
        assert set(merged) == {"urn:one", "urn:two"}

    def test_conflicting_retrieve_functions_are_an_error(self):
        one = Registry(retrieve=lambda uri: Resource.opaque(contents=1))
        two = Registry(retrieve=lambda uri: Resource.opaque(contents=2))
        with pytest.raises(ValueError):  # noqa: PT011
            one.merge(two)

    def test_original_registries_are_unmodified(self):
        """
        Merging -- successfully or not -- leaves both originals alone.
        """
        one_resource = Resource.opaque(contents={"from": "one"})
        two_resource = Resource.opaque(contents={"from": "two"})
        one = Registry().with_resource("urn:shared", one_resource)
        two = Registry().with_resource("urn:shared", two_resource)

        with pytest.raises(exceptions.MergeConflict):
            one.merge(two)
        assert one["urn:shared"] is one_resource
        assert two["urn:shared"] is two_resource
        assert len(one) == len(two) == 1

        three = Registry().with_resource(
            "urn:other",
            Resource.opaque(contents=3),
        )
        merged = one.merge(three)
        assert len(one) == 1
        assert len(three) == 1
        assert len(merged) == 2

    def test_original_registries_still_resolve_independently(self):
        one = Registry().with_resource(
            "urn:one",
            Resource.opaque(contents={"from": "one"}),
        )
        two = Registry().with_resource(
            "urn:two",
            Resource.opaque(contents={"from": "two"}),
        )
        one.merge(two)

        assert one.resolver().lookup("urn:one").contents == {"from": "one"}
        assert two.resolver().lookup("urn:two").contents == {"from": "two"}
        with pytest.raises(exceptions.Unresolvable):
            one.resolver().lookup("urn:two")
        with pytest.raises(exceptions.Unresolvable):
            two.resolver().lookup("urn:one")

    def test_dynamic_scope_survives_merging(self):
        """
        Resources keep their dynamic-scope behavior after being merged.
        """
        one = ID_AND_CHILDREN.create_resource(
            {
                "ID": "http://example.com/",
                "children": [
                    {
                        "ID": "child/",
                        "children": [{"ID": "grandchild"}],
                    },
                ],
            },
        )
        two = ID_AND_CHILDREN.create_resource(
            {
                "ID": "http://example.com/two",
                "children": [{"ID": "two-child/"}],
            },
        )
        first = one @ Registry()
        second = two @ Registry()
        merged = first.merge(second)

        resolver = merged.resolver()
        root = resolver.lookup("http://example.com/")
        child = root.resolver.lookup("#/children/0")
        grandchild = child.resolver.lookup("grandchild")
        assert grandchild.contents == {"ID": "grandchild"}

        other = grandchild.resolver.lookup("http://example.com/two")
        assert list(other.resolver.dynamic_scope()) == [
            ("http://example.com/child/grandchild", other.resolver._registry),
            ("http://example.com/child/", other.resolver._registry),
            ("http://example.com/", other.resolver._registry),
        ]
