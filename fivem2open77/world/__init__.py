"""World edits: Night City's own objects around a spot -- deleted, moved, replaced -- and new ones
added, baked into an Open77 resource that every player of a server gets.

How it reaches the game.  The world is streamed in sectors (``*.streamingsector``); a sector lists
its node *instances* (``nodeData``) and the nodes they place.  ArchiveXL patches sectors as they
load (``streaming: sectors`` in an ``.xl``): a node instance deleted or mutated (moved, scaled, given
another mesh or appearance), and inside an instance the single elements -- one instance of an
instanced mesh, one actor of a collision node.  The resource ships that ``.xl`` (and, for objects
added, a small sector of its own) as a ``preload_mod``: inert data the launcher installs before
the game starts, so the edit is permanent for everyone on that server.

    sector.py   a sector (WolvenKit JSON) -> its node instances with type, resource, transform,
                bounds and elements, in world coordinates
    index.py    which sectors cover a spot (from the world's streaming block) and a cache of the
                sectors read from the game
    edits.py    the edit list: validated, merged, compiled to ArchiveXL's ``streaming: sectors``
    package.py  the Open77 resource (and the deploy bundle)
"""
