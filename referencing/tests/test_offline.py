import pytest

from referencing import Registry, Resource, exceptions
from referencing.jsonschema import DRAFT201909, DRAFT202012
from referencing.offline import (
    DynamicReference,
    MissingReference,
    OfflineImportError,
    close_over,
)

ROOT = "https://example.com/root.json"
DEFS = "https://example.com/defs.json"


def blow_up(uri):  # pragma: no cover
    """
    A retriever suitable for use in tests which expect it never to be used.
    """
    raise RuntimeError("This retrieve function expects to never be called!")


def counting_retrieve():
    """
    A retriever which counts how often it was (not) called.
    """
    calls = []

    def retrieve(uri):
        calls.append(uri)
        raise exceptions.NoSuchResource(ref=uri)

    return calls, retrieve


def schema2020(contents):
    return DRAFT202012.create_resource(contents)


class TestClosure:
    def test_mutual_relative_references_close(self):
        """
        A root referring relatively to ``defs`` which refers back closes.
        """
        root = schema2020({"$ref": "defs.json"})
        defs = schema2020({"$ref": "root.json"})
        closure = close_over(ROOT, {ROOT: root, DEFS: defs})

        assert closure.root == ROOT
        assert set(closure) == {ROOT, DEFS}
        assert len(closure) == 2
        assert ROOT in closure
        assert DEFS in closure
        assert closure.dynamic == ()

        registry = closure.registry
        assert registry.resolver(ROOT).lookup("defs.json").contents == {
            "$ref": "root.json",
        }
        assert registry.resolver(DEFS).lookup("root.json").contents == {
            "$ref": "defs.json",
        }

    def test_self_references_close(self):
        root = schema2020(
            {
                "$defs": {"self": {"$ref": "#"}},
                "$ref": "#/$defs/self",
            },
        )
        closure = close_over(ROOT, {ROOT: root})
        assert set(closure) == {ROOT}

    def test_closure_never_retrieves(self):
        """
        Resolution against a closed registry stays offline (0 retrievals).
        """
        root = schema2020({"$ref": "defs.json"})
        defs = schema2020({"$ref": "root.json"})
        closure = close_over(ROOT, {ROOT: root, DEFS: defs})

        calls, retrieve = counting_retrieve()
        registry = Registry(retrieve=retrieve).combine(closure.registry)
        registry.resolver(ROOT).lookup("defs.json")
        registry.resolver(DEFS).lookup("root.json")
        assert calls == []

        # The closure's own registry cannot retrieve at all.
        with pytest.raises(exceptions.NoSuchResource):
            closure.registry.get_or_retrieve("https://example.com/elsewhere")

    def test_unrelated_resources_are_not_included(self):
        root = schema2020({"$ref": "defs.json"})
        defs = schema2020({})
        unrelated = schema2020({})
        closure = close_over(
            ROOT,
            {ROOT: root, DEFS: defs, "https://example.com/other": unrelated},
        )
        assert set(closure) == {ROOT, DEFS}

    def test_resources_may_be_a_registry(self):
        root = schema2020({"$ref": "defs.json"})
        defs = schema2020({"$ref": "root.json"})
        registry = Registry().with_resources([(ROOT, root), (DEFS, defs)])
        closure = close_over(ROOT, registry)
        assert set(closure) == {ROOT, DEFS}

    def test_opaque_resources_close_trivially(self):
        root = Resource.opaque({"no": "references here"})
        closure = close_over("urn:example:root", {"urn:example:root": root})
        assert set(closure) == {"urn:example:root"}


class TestMissing:
    def test_missing_resource_reports_location(self):
        """
        A missing resource is reported with referrer and reference location.
        """
        root = schema2020({"properties": {"foo": {"$ref": "defs.json"}}})
        with pytest.raises(OfflineImportError) as excinfo:
            close_over(ROOT, {ROOT: root})

        (missing,) = excinfo.value.missing
        assert missing == MissingReference(
            referrer=ROOT,
            location="/properties/foo/$ref",
            ref="defs.json",
            resolved=DEFS,
        )
        assert "no such resource" in str(excinfo.value)
        assert DEFS in str(excinfo.value)

    def test_all_missing_are_reported_at_once(self):
        root = schema2020(
            {
                "properties": {
                    "one": {"$ref": "one.json"},
                    "two": {"$ref": "sub/two.json"},
                },
            },
        )
        with pytest.raises(OfflineImportError) as excinfo:
            close_over(ROOT, {ROOT: root})

        assert {
            (each.location, each.resolved) for each in excinfo.value.missing
        } == {
            ("/properties/one/$ref", "https://example.com/one.json"),
            ("/properties/two/$ref", "https://example.com/sub/two.json"),
        }

    def test_missing_transitively(self):
        root = schema2020({"$ref": "defs.json"})
        defs = schema2020({"$ref": "missing.json"})
        with pytest.raises(OfflineImportError) as excinfo:
            close_over(ROOT, {ROOT: root, DEFS: defs})

        (missing,) = excinfo.value.missing
        assert missing.referrer == DEFS
        assert missing.resolved == "https://example.com/missing.json"

    def test_missing_root(self):
        with pytest.raises(OfflineImportError) as excinfo:
            close_over(ROOT, {})
        (missing,) = excinfo.value.missing
        assert missing.referrer is None
        assert missing.resolved == ROOT

    def test_missing_import_never_retrieves(self):
        """
        A failed import publishes nothing and performs no retrieval.
        """
        root = schema2020({"$ref": "defs.json"})
        calls, retrieve = counting_retrieve()
        registry = Registry(retrieve=retrieve)
        with pytest.raises(OfflineImportError):
            close_over(ROOT, registry.with_resource(ROOT, root))
        assert calls == []


class TestAnchorsAndFragments:
    def test_anchor_fragment_correspondence(self):
        root = schema2020({"$ref": "defs.json#positive"})
        defs = schema2020(
            {
                "$defs": {
                    "int": {
                        "$anchor": "positive",
                        "type": "integer",
                        "minimum": 0,
                    },
                },
            },
        )
        closure = close_over(ROOT, {ROOT: root, DEFS: defs})
        resolved = closure.registry.resolver(ROOT).lookup("defs.json#positive")
        assert resolved.contents["type"] == "integer"

    def test_unknown_anchor_is_reported(self):
        root = schema2020({"$ref": "defs.json#noSuchAnchor"})
        defs = schema2020({"$defs": {"int": {"$anchor": "positive"}}})
        with pytest.raises(OfflineImportError) as excinfo:
            close_over(ROOT, {ROOT: root, DEFS: defs})

        (missing,) = excinfo.value.missing
        assert missing.referrer == ROOT
        assert missing.location == "/$ref"
        assert missing.resolved == DEFS
        assert missing.reason == "no such anchor"

    def test_pointer_fragments_are_checked(self):
        root = schema2020(
            {
                "$defs": {"foo": {"type": "integer"}},
                "properties": {
                    "ok": {"$ref": "#/$defs/foo"},
                    "bad": {"$ref": "#/$defs/bar"},
                },
            },
        )
        with pytest.raises(OfflineImportError) as excinfo:
            close_over(ROOT, {ROOT: root})

        (missing,) = excinfo.value.missing
        assert missing.location == "/properties/bad/$ref"
        assert missing.reason == "pointer to nowhere"

    def test_pointer_into_other_resource(self):
        root = schema2020({"$ref": "defs.json#/$defs/foo"})
        defs = schema2020({"$defs": {"foo": {"type": "integer"}}})
        closure = close_over(ROOT, {ROOT: root, DEFS: defs})
        assert set(closure) == {ROOT, DEFS}


class TestNestedIdentifiers:
    def test_nested_id_satisfies_references(self):
        """
        A nested ``$id`` provides a URI which references may target.
        """
        root = schema2020(
            {
                "$id": ROOT,
                "$defs": {
                    "inner": {
                        "$id": "inner.json",
                        "$anchor": "innerAnchor",
                        "type": "integer",
                    },
                },
                "$ref": "inner.json",
            },
        )
        closure = close_over(ROOT, {ROOT: root})

        inner = "https://example.com/inner.json"
        assert inner in closure
        resolved = closure.registry.resolver(ROOT).lookup("inner.json")
        assert resolved.contents["type"] == "integer"

    def test_nested_id_referring_outwards(self):
        root = schema2020(
            {
                "$id": ROOT,
                "$defs": {
                    "inner": {
                        "$id": "inner.json",
                        "properties": {
                            "back": {"$ref": "root.json"},
                            "anchor": {"$ref": "root.json#top"},
                        },
                    },
                },
                "$anchor": "top",
                "$ref": "inner.json",
            },
        )
        closure = close_over(ROOT, {ROOT: root})
        assert set(closure) == {ROOT, "https://example.com/inner.json"}

    def test_reference_to_unknown_nested_id_is_missing(self):
        root = schema2020(
            {
                "$id": ROOT,
                "$defs": {"inner": {"$id": "inner.json"}},
                "properties": {"nope": {"$ref": "other-inner.json"}},
            },
        )
        with pytest.raises(OfflineImportError) as excinfo:
            close_over(ROOT, {ROOT: root})
        (missing,) = excinfo.value.missing
        assert missing.location == "/properties/nope/$ref"
        assert missing.resolved == "https://example.com/other-inner.json"


class TestDynamicReferences:
    def test_dynamic_references_are_noted_not_resolved(self):
        root = schema2020(
            {
                "$id": ROOT,
                "$ref": "node.json",
                "$defs": {
                    "node": {
                        "$dynamicAnchor": "node",
                        "properties": {
                            "child": {"$dynamicRef": "#node"},
                        },
                    },
                },
            },
        )
        node = schema2020(
            {
                "$id": "https://example.com/node.json",
                "$dynamicAnchor": "node",
                "properties": {"child": {"$dynamicRef": "#node"}},
            },
        )
        closure = close_over(
            ROOT,
            {ROOT: root, "https://example.com/node.json": node},
        )

        assert len(closure.dynamic) == 2
        (dynamic,) = [
            each for each in closure.dynamic if each.referrer == ROOT
        ]
        assert dynamic == DynamicReference(
            referrer=ROOT,
            location="/$defs/node/properties/child/$dynamicRef",
            ref="#node",
            kind="dynamic",
            constraint=dynamic.constraint,
        )
        assert "dynamic scope" in dynamic.constraint

        # The static targets still resolve, fully offline.
        resolved = closure.registry.resolver(ROOT).lookup("node.json")
        assert resolved.contents["$dynamicAnchor"] == "node"

    def test_recursive_references_are_noted(self):
        root = DRAFT201909.create_resource(
            {
                "$id": ROOT,
                "properties": {"child": {"$recursiveRef": "#"}},
            },
        )
        closure = close_over(ROOT, {ROOT: root})
        (recursive,) = closure.dynamic
        assert recursive.kind == "recursive"
        assert recursive.location == "/properties/child/$recursiveRef"

    def test_dynamic_reference_to_missing_resource_is_missing(self):
        root = schema2020({"$dynamicRef": "node.json#node"})
        with pytest.raises(OfflineImportError) as excinfo:
            close_over(ROOT, {ROOT: root})
        (missing,) = excinfo.value.missing
        assert missing.resolved == "https://example.com/node.json"

    def test_dynamic_reference_to_missing_anchor_is_missing(self):
        root = schema2020({"$dynamicRef": "node.json#node"})
        node = schema2020({"$id": "https://example.com/node.json"})
        with pytest.raises(OfflineImportError) as excinfo:
            close_over(
                ROOT,
                {ROOT: root, "https://example.com/node.json": node},
            )
        (missing,) = excinfo.value.missing
        assert missing.reason == "no such anchor"
