"""
Tests for the offline reference importer.
"""

from __future__ import annotations

import pytest

from referencing import (
    OfflineImportError,
    Resource,
    import_offline,
)
from referencing.jsonschema import DRAFT202012


def resource(contents, specification=DRAFT202012):
    return Resource(contents=contents, specification=specification)


class TestImportOffline:
    def test_root_and_defs_cycle_closes_to_two_resources(self):
        """
        root references defs, defs references root -- the closed set has both.
        """
        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {"defs": {"$ref": "defs"}},
            },
        )
        defs = resource(
            {
                "$id": "http://example.com/defs",
                "type": "object",
                "properties": {"root": {"$ref": "root"}},
            },
        )

        result = import_offline([root, defs], root="http://example.com/root")

        assert [uri for uri, _ in result.resources] == [
            "http://example.com/defs",
            "http://example.com/root",
        ]
        assert len(result.registry) == 2

        resolver = result.registry.resolver()
        assert resolver.lookup("http://example.com/root").contents["$id"] == (
            "http://example.com/root"
        )
        assert resolver.lookup("http://example.com/defs").contents["$id"] == (
            "http://example.com/defs"
        )

    def test_missing_defs_is_located_in_root(self):
        """
        Removing defs reports the reference path inside root, and no retrieval
        is ever attempted.
        """
        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {
                    "a": {"$ref": "defs"},
                    "b": {"$ref": "defs#/foo"},
                },
            },
        )

        with pytest.raises(OfflineImportError) as exc_info:
            import_offline([root], root="http://example.com/root")

        missing = exc_info.value.missing
        assert len(missing) == 2
        by_path = {m.site.path: m for m in missing}
        assert by_path[("properties", "a")].target == "http://example.com/defs"
        assert by_path[("properties", "b")].target == (
            "http://example.com/defs#/foo"
        )
        for each in missing:
            assert each.site.resource == "http://example.com/root"
            assert each.detail == "no such resource"

    def test_no_partial_registry_is_published_when_missing(self):
        """
        If any reference is missing, the error is raised and no result (with a
        registry) is returned.
        """
        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {"a": {"$ref": "defs"}},
            },
        )

        with pytest.raises(OfflineImportError):
            import_offline([root], root="http://example.com/root")

    def test_anchors_are_checked_against_uri_fragments(self):
        """
        A plain-name fragment must name an anchor in the target resource; a
        JSON pointer fragment must resolve.
        """
        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "$anchor": "myAnchor",
                "$defs": {
                    "ok_anchor": {"$ref": "root#myAnchor"},
                    "bad_anchor": {"$ref": "root#missing"},
                    "ok_pointer": {"$ref": "root#/type"},
                    "bad_pointer": {"$ref": "root#/nope"},
                },
            },
        )

        with pytest.raises(OfflineImportError) as exc_info:
            import_offline([root], root="http://example.com/root")

        missing = {m.site.path: m for m in exc_info.value.missing}
        assert set(missing) == {
            ("$defs", "bad_anchor"),
            ("$defs", "bad_pointer"),
        }
        assert missing[("$defs", "bad_anchor")].detail == (
            "no such anchor 'missing'"
        )
        assert missing[("$defs", "bad_pointer")].detail == (
            "JSON pointer points to nowhere"
        )

    def test_nested_dollar_id_changes_base_uri(self):
        """
        A subresource with its own ``$id`` changes the base URI for references
        within it.
        """
        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {
                    "inner": {
                        "$id": "http://example.com/inner",
                        "type": "object",
                        "properties": {
                            "x": {"$ref": "other"},
                        },
                    },
                },
            },
        )
        other = resource(
            {"$id": "http://example.com/other", "type": "string"},
        )

        result = import_offline(
            [root, other],
            root="http://example.com/root",
        )

        assert [uri for uri, _ in result.resources] == [
            "http://example.com/inner",
            "http://example.com/other",
            "http://example.com/root",
        ]
        resolver = result.registry.resolver()
        assert resolver.lookup("http://example.com/inner").contents["$id"] == (
            "http://example.com/inner"
        )

    def test_dynamic_references_are_recorded_not_faked(self):
        """
        ``$dynamicRef`` is recorded as a dynamic reference with its static
        target and a description of the dynamic scope constraint.
        """
        root = resource(
            {
                "$id": "http://example.com/dyn",
                "type": "object",
                "properties": {"foo": {"$dynamicRef": "#foo"}},
                "$dynamicAnchor": "foo",
            },
        )

        result = import_offline([root], root="http://example.com/dyn")

        assert len(result.dynamic_references) == 1
        dynamic = result.dynamic_references[0]
        assert dynamic.site.path == ("properties", "foo")
        assert dynamic.target == "http://example.com/dyn#foo"
        assert "dynamic scope" in dynamic.description

    def test_dynamic_reference_still_validates_static_target_exists(self):
        """
        Even though dynamic resolution may redirect, the static target must
        exist; a missing static target is reported.
        """
        root = resource(
            {
                "$id": "http://example.com/dyn",
                "type": "object",
                "properties": {"foo": {"$dynamicRef": "#missing"}},
            },
        )

        with pytest.raises(OfflineImportError) as exc_info:
            import_offline([root], root="http://example.com/dyn")

        assert len(exc_info.value.missing) == 1
        assert exc_info.value.missing[0].detail == "no such anchor 'missing'"

    def test_multiple_missing_references_are_aggregated(self):
        """
        All missing references are reported in one go, not just the first.
        """
        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {
                    "a": {"$ref": "one"},
                    "b": {"$ref": "two"},
                    "c": {"$ref": "three"},
                },
            },
        )

        with pytest.raises(OfflineImportError) as exc_info:
            import_offline([root], root="http://example.com/root")

        assert len(exc_info.value.missing) == 3
        targets = {m.target for m in exc_info.value.missing}
        assert targets == {
            "http://example.com/one",
            "http://example.com/two",
            "http://example.com/three",
        }

    def test_result_registry_has_no_retrieval_function(self):
        """
        The published registry has no retrieval function, so resolving known
        URIs works without ever retrieving.
        """
        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {"defs": {"$ref": "defs"}},
            },
        )
        defs = resource(
            {"$id": "http://example.com/defs", "type": "string"},
        )

        result = import_offline([root, defs], root="http://example.com/root")

        assert result.registry._retrieve.__name__ == "_fail_to_retrieve"
        resolver = result.registry.resolver()
        assert resolver.lookup("http://example.com/defs").contents["type"] == (
            "string"
        )

    def test_root_not_found_reports_error(self):
        """
        If the designated root URI does not identify a given resource, an
        error is raised.
        """
        root = resource(
            {"$id": "http://example.com/root", "type": "object"},
        )

        with pytest.raises(OfflineImportError) as exc_info:
            import_offline([root], root="http://example.com/nope")

        assert len(exc_info.value.missing) == 1
        assert exc_info.value.missing[0].target == "http://example.com/nope"

    def test_bare_resources_with_internal_ids_are_accepted(self):
        """
        Resources with internal identifiers can be passed directly.
        """
        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {"defs": {"$ref": "defs"}},
            },
        )
        defs = resource(
            {"$id": "http://example.com/defs", "type": "string"},
        )

        result = import_offline([root, defs], root="http://example.com/root")

        assert [uri for uri, _ in result.resources] == [
            "http://example.com/defs",
            "http://example.com/root",
        ]

    def test_recursive_ref_is_recorded_as_dynamic(self):
        """
        Draft 2019-09's ``$recursiveRef`` is recorded as a dynamic reference.
        """
        from referencing.jsonschema import DRAFT201909

        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {"foo": {"$recursiveRef": "#"}},
                "$recursiveAnchor": True,
            },
            specification=DRAFT201909,
        )

        result = import_offline([root], root="http://example.com/root")

        assert len(result.dynamic_references) == 1
        assert result.dynamic_references[0].site.recursive is True
        assert "recursive" in result.dynamic_references[0].description

    def test_relative_refs_resolve_against_resource_base(self):
        """
        A relative reference resolves against the base URI of the resource
        containing it.
        """
        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {
                    "a": {"$ref": "defs"},
                    "b": {"$ref": "defs#x"},
                },
                "$defs": {
                    "defs": {
                        "$id": "http://example.com/defs",
                        "type": "object",
                        "$anchor": "x",
                    },
                },
            },
        )

        result = import_offline([root], root="http://example.com/root")

        assert [uri for uri, _ in result.resources] == [
            "http://example.com/defs",
            "http://example.com/root",
        ]

    def test_reference_to_discovered_subresource(self):
        """
        A reference may point to a subresource discovered during the walk.
        """
        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {"a": {"$ref": "a"}},
                "$defs": {
                    "a": {
                        "$id": "http://example.com/a",
                        "type": "string",
                    },
                },
            },
        )

        result = import_offline([root], root="http://example.com/root")

        assert [uri for uri, _ in result.resources] == [
            "http://example.com/a",
            "http://example.com/root",
        ]
        resolver = result.registry.resolver()
        assert resolver.lookup("http://example.com/a").contents["type"] == (
            "string"
        )

    def test_deeply_nested_subresources_with_ids(self):
        """
        Subresources nested within subresources are discovered and closed.
        """
        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {
                    "outer": {
                        "$id": "http://example.com/outer",
                        "type": "object",
                        "properties": {
                            "inner": {
                                "$id": "http://example.com/inner",
                                "type": "object",
                                "properties": {
                                    "x": {"$ref": "deep"},
                                },
                            },
                        },
                    },
                },
            },
        )
        deep = resource(
            {"$id": "http://example.com/deep", "type": "integer"},
        )

        result = import_offline(
            [root, deep],
            root="http://example.com/root",
        )

        assert [uri for uri, _ in result.resources] == [
            "http://example.com/deep",
            "http://example.com/inner",
            "http://example.com/outer",
            "http://example.com/root",
        ]

    def test_legacy_draft7_anchors_and_refs(self):
        """
        Draft 7 uses ``$id`` fragments as anchors and ``$ref``.
        """
        from referencing.jsonschema import DRAFT7

        root = resource(
            {
                "$id": "http://example.com/root",
                "type": "object",
                "properties": {"a": {"$ref": "defs#myAnchor"}},
                "definitions": {
                    "defs": {
                        "$id": "http://example.com/defs",
                        "type": "object",
                        "properties": {"b": {"type": "string"}},
                    },
                },
            },
            specification=DRAFT7,
        )

        # A missing anchor in draft 7 is reported.
        with pytest.raises(OfflineImportError) as exc_info:
            import_offline([root], root="http://example.com/root")

        assert len(exc_info.value.missing) == 1
        assert exc_info.value.missing[0].detail == "no such anchor 'myAnchor'"
