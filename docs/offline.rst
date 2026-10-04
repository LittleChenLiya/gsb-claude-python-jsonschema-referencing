Offline Registries
==================

.. currentmodule:: referencing.offline

Sometimes the set of resources an application may use is known up front --
schemas shipped alongside an application, vendored into a package, or
validated at build time. In that case, resolution should *never* wander off
to the network (or anywhere else) at runtime.

`close_over` is an offline importer which helps build such registries. Give
it a root URI and a fixed set of candidate resources, and it walks every
reference reachable from the root -- following each resource's own
specification (dialect) to find subresources, anchors and relative
references -- and produces a `Closure`: a manifest of exactly the reachable
resources, bundled into a `Registry` with no retrieval function configured.

.. testcode::

    from referencing.jsonschema import DRAFT202012
    from referencing.offline import close_over

    root = DRAFT202012.create_resource({"$ref": "defs.json"})
    defs = DRAFT202012.create_resource({"$ref": "root.json"})

    closure = close_over(
        "https://example.com/root.json",
        {
            "https://example.com/root.json": root,
            "https://example.com/defs.json": defs,
        },
    )

    # The closure is the manifest of root-reachable resources:
    print(sorted(closure))

    # Its registry resolves references fully offline:
    resolver = closure.registry.resolver("https://example.com/root.json")
    print(resolver.lookup("defs.json").contents)

.. testoutput::

    ['https://example.com/defs.json', 'https://example.com/root.json']
    {'$ref': 'root.json'}

References may form cycles (as above); each resource is scanned exactly
once. Resources which are provided but not reachable from the root are not
included in the closure.

Nested identifiers (e.g. the :kw:`$id` keyword) are honored: a subresource
with its own identifier both re-bases relative references beneath it and
provides its URI to the rest of the closure, and plain-name fragments are
checked against the anchors each (sub)resource actually defines.

The import is all-or-nothing. If anything reachable from the root cannot be
satisfied by the provided resources -- a missing resource, an unknown
anchor, or a JSON pointer to nowhere -- `OfflineImportError` is raised
describing *all* problems at once, each with its referring resource and the
JSON pointer location of the reference within it. No partial registry is
published.

.. testcode::

    from referencing.offline import OfflineImportError, close_over

    root = DRAFT202012.create_resource(
        {"properties": {"foo": {"$ref": "defs.json"}}},
    )
    try:
        close_over(
            "https://example.com/root.json",
            {"https://example.com/root.json": root},
        )
    except OfflineImportError as error:
        print(error)

.. testoutput::

    1 reference(s) could not be satisfied offline:
      - https://example.com/root.json (/properties/foo/$ref): 'defs.json' -> 'https://example.com/defs.json' (no such resource)

Dynamic references (``$dynamicRef``, ``$recursiveRef``) are *not* pretended
to be statically resolved. Their static targets are validated like any other
reference, but each one is also recorded in `Closure.dynamic` as a
`DynamicReference` note describing the dynamic-scope constraint which still
applies when the reference is resolved at evaluation time.
