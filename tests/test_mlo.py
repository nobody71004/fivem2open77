"""Offline checks for the map/MLO converter: python -m pytest tests  or  python tests/test_mlo.py

Set F2O77_TEST_MAP to a FiveM map resource (folder or archive) to also run the parser checks on
real GTA files (they are not shipped with the repository), and WOLVENKIT_CLI to check that
WolvenKit reads the generated entity / sector / block documents back unchanged.
"""
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.cp77 import package  # noqa: E402
from fivem2open77.gta.bounds import Collision  # noqa: E402
from fivem2open77.mlo import bake as B  # noqa: E402
from fivem2open77.mlo import collision as C  # noqa: E402
from fivem2open77.mlo import deploy as D  # noqa: E402
from fivem2open77.mlo import ghost as G  # noqa: E402
from fivem2open77.mlo import resource as R  # noqa: E402
from fivem2open77.mlo import world as W  # noqa: E402
from fivem2open77.mlo.build import MapModel, MapPart, Placement, _sanitize  # noqa: E402
from fivem2open77.mlo.scene import q_inverse, trs  # noqa: E402

HERE = os.path.dirname(__file__)


def _box_tris(lo, hi):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], float)
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return v[np.array(f)]


def _layout(name="cedar_crest"):
    base = rf"fivem2open77\maps\{name}"
    return {
        "name": name, "display": 'Cedar Crest "Estate"', "version": "1.0.0",
        "parts": {"mlo_house": {"mesh": rf"{base}\meshes\mlo_house.mesh", "ent": rf"{base}\ents\mlo_house.ent",
                                "kind": "mlo", "radius": 30.0, "collision": True, "bounds": [[-20, -20, 0], [20, 20, 12]]},
                  "chunk_500_500": {"mesh": rf"{base}\meshes\chunk_500_500.mesh", "ent": rf"{base}\ents\chunk_500_500.ent",
                                    "kind": "chunk", "radius": 40.0, "collision": True, "bounds": [[-24, -24, -2], [24, 24, 2]]}},
        "placements": [{"part": "mlo_house", "x": 3.0, "y": 4.0, "z": 0.5, "yaw": 0.0},
                       {"part": "mlo_house", "x": -3.0, "y": 4.0, "z": 0.5, "yaw": 180.0},
                       {"part": "chunk_500_500", "x": 24.0, "y": 24.0, "z": 0.0, "yaw": 0.0}],
        "spawn": {"x": 0.0, "y": 0.0, "z": 1.0}, "bounds": {"min": [-30, -30, -5], "max": [50, 50, 15]},
        "packages": [f"{name}.zip"], "archives": [f"fivem2open77_map_{name}.archive"], "default": None,
    }


def _model():
    """A tiny MapModel: a 20 x 10 x 6 m box house placed twice and a ground chunk."""
    house = MapPart("mlo_house", "mlo")
    tris = _box_tris((-10, -5, 0), (10, 5, 6))
    house.colliders = C.triangles_to_colliders(tris, np.zeros(len(tris), np.int32), min_extent=0.0)
    house.submeshes = []
    ground = MapPart("chunk_500_500", "chunk")
    ground.colliders = [C.Collider("box", "concrete.physmat", (0, 0, -0.5), (0, 0, 0, 1), (24, 24, 0.5))]
    m = MapModel(name="cedar_crest", display_name="Cedar Crest", anchor=(0, 0, 0), spawn=(0, 0, 1))
    m.parts = {"mlo_house": house, "chunk_500_500": ground}
    m.placements = [Placement("mlo_house", (3.0, 4.0, 0.5), 0.0, "a"), Placement("mlo_house", (-30.0, 4.0, 0.5), 90.0, "b"),
                    Placement("chunk_500_500", (0.0, 0.0, 0.0), 0.0, "c")]
    return m


# ------------------------------------------------------------------------------ collision
def test_box_mesh_becomes_box_slabs():
    tris = _box_tris((0, 0, 0), (4, 0.2, 3))          # a wall
    cols = C.triangles_to_colliders(tris, np.zeros(len(tris), np.int32), min_extent=0.0)
    assert cols and all(c.kind == "box" for c in cols), [c.kind for c in cols]
    # floor/top faces (up) and 4 side faces; the bottom (ceiling, facing down) is skipped
    assert len(cols) == 5
    for c in cols:
        assert min(c.half) > 0


def test_flat_ground_heightfield_merges():
    tris = []
    for i in range(40):
        for j in range(40):
            a, b, c, d = (i, j, 0), (i + 1, j, 0), (i + 1, j + 1, 0), (i, j + 1, 0)
            tris += [(a, b, c), (a, c, d)]
    col = Collision(triangles=np.array(tris, float), tri_materials=np.zeros(len(tris), np.int32))
    cols, note = C.ground_colliders(col, spacing=1.0)
    assert note["heightfield"] <= 16, note            # merged into a handful of big boxes
    assert all(c.kind == "box" for c in cols)
    top = max(c.center[2] + c.half[2] for c in cols)
    assert abs(top) < 1e-6


def _quad_tris(x0, y0, x1, y1, z, n=1):
    """Flat walkable quad at height z, split into an n x n grid of triangle pairs."""
    xs, ys = np.linspace(x0, x1, n + 1), np.linspace(y0, y1, n + 1)
    out = []
    for i in range(n):
        for j in range(n):
            a, b = (xs[i], ys[j], z), (xs[i + 1], ys[j], z)
            c, d = (xs[i + 1], ys[j + 1], z), (xs[i], ys[j + 1], z)
            out += [(a, b, c), (a, c, d)]
    return out


def test_fill_covers_the_floor_under_a_roof_and_narrow_paths():
    from fivem2open77.mlo import coverage as CV
    tris = _quad_tris(0, 0, 30, 30, 0.0, n=6)            # yard
    tris += _quad_tris(5, 5, 15, 15, 3.0)                 # carport roof over part of it
    tris += _quad_tris(40, 0, 41.2, 30, 0.5, n=4)         # a 1.2 m wide path: narrower than the grid
    tris += [((20, 20, 0), (26, 20, 0), (26, 20, 2.5)), ((20, 20, 0), (26, 20, 2.5), (20, 20, 2.5))]   # a wall
    t = np.array(tris, float)
    col = Collision(triangles=t, tri_materials=np.zeros(len(t), np.int32))
    bare, _ = C.ground_colliders(col, fill=False)
    assert CV.uncovered_triangles(bare, t).any()          # the heightfield alone keeps only the top
    cols, note = C.ground_colliders(col)
    assert note["filled_triangles"] > 0 and note["fill"] > 0, note
    assert not CV.uncovered_triangles(cols, t).any()
    assert all(c.kind == "box" and min(c.half) > 0 for c in cols)
    # under the roof: something solid right below the yard surface at (10, 10)
    C3, R3, H3 = CV.obb_arrays(cols)
    caster = CV.BoxCaster(C3, R3, H3)
    d = caster.cast(np.array([[10.0, 10.0, 1.0]]), np.array([[0.0, 0.0, -1.0]]), 2.0)
    assert abs(d[0] - 1.0) < 0.05, d
    # no ramp from the yard up to the roof edge: next to the roof the first thing below is the yard
    ring = np.array([[x, y, 2.9] for x in (15.3, 16.0, 16.9) for y in (6.0, 10.0, 14.0)] +
                    [[x, y, 2.9] for x in (6.0, 10.0, 14.0) for y in (3.2, 4.0, 4.7)])
    d = caster.cast(ring, np.tile([0.0, 0.0, -1.0], (len(ring), 1)), 4.0)
    assert np.allclose(d, 2.9, atol=0.05), d


def test_heavy_parts_spill_into_collision_only_parts():
    from fivem2open77.mlo import build as MB
    m = _model()
    big = MapPart("chunk_501_500", "chunk")
    big.colliders = [C.Collider("box", "concrete.physmat", (i * 0.1, (i % 7) * 3.0, 0.0), (0, 0, 0, 1), (0.05, 0.05, 0.05))
                     for i in range(MB.MAX_SHAPES * 2 + 17)]
    m.parts["chunk_501_500"] = big
    m.placements.append(Placement("chunk_501_500", (48.0, 0.0, 0.0), 0.0, "chunk"))
    b = MB.MapBuilder.__new__(MB.MapBuilder)
    b.model = m
    rep = {}
    b._split_heavy_parts(rep)
    names = rep["split_parts"]["chunk_501_500"]
    assert names == ["chunk_501_500_c1", "chunk_501_500_c2"]
    sizes = [len(m.parts[n].colliders) for n in ["chunk_501_500"] + names]
    assert sizes == [MB.MAX_SHAPES, MB.MAX_SHAPES, 17] and all(m.parts[n].collision_only for n in names)
    for n in names:
        pl = [p for p in m.placements if p.part == n]
        assert len(pl) == 1 and pl[0].position == (48.0, 0.0, 0.0)
    assert "mlo_house" not in rep["split_parts"]


def _builder(model):
    from fivem2open77.mlo import build as MB
    b = MB.MapBuilder.__new__(MB.MapBuilder)
    b.model, b.name, b.log = model, model.name, (lambda *_: None)
    return b


def test_wide_parts_split_into_render_only_pieces():
    """Sea Dome: a 271-submesh dome crashed the game (the chunk count is 8 bits).  No mesh keeps
    more than MAX_CHUNKS; the pieces draw only, sit where the part sits, and are spatially compact."""
    from fivem2open77.mlo import build as MB
    m = _model()
    dome = MapPart("mlo_dome", "mlo")
    rng = np.random.default_rng(7)
    subs = []
    for i in range(271):
        ang = 2 * math.pi * i / 271
        c = np.array([90 * math.cos(ang), 90 * math.sin(ang), 20 + 10 * math.sin(3 * ang)])
        sm = _mesh(list(_box_tris(c - rng.uniform(1, 4, 3), c + rng.uniform(1, 4, 3))))
        sm.material = f"mat{i % 268}"
        subs.append(sm)
    dome.submeshes = list(subs)
    dome.colliders = [C.Collider("box", "concrete.physmat", (0, 0, 0), (0, 0, 0, 1), (1, 1, 1))]
    m.parts["mlo_dome"] = dome
    m.placements += [Placement("mlo_dome", (1.0, 2.0, 3.0), 30.0, "d1"), Placement("mlo_dome", (-9.0, 2.0, 3.0), 0.0, "d2")]
    rep = {}
    _builder(m)._split_wide_parts(rep)
    info = rep["mesh_split_parts"]["mlo_dome"]
    pieces = info["pieces"]
    assert len(pieces) + 1 == math.ceil(271 / MB.MAX_CHUNKS) == 5, info
    assert all(len(m.parts[n].submeshes) <= MB.MAX_CHUNKS for n in ["mlo_dome"] + pieces)
    got = [id(sm) for n in ["mlo_dome"] + pieces for sm in m.parts[n].submeshes]
    assert sorted(got) == sorted(id(sm) for sm in subs), "every submesh exactly once"
    assert len(m.parts["mlo_dome"].colliders) == 1 and m.parts["mlo_dome"].piece_of is None
    for n in pieces:
        pc = m.parts[n]
        assert pc.piece_of == "mlo_dome" and pc.kind == "mlo" and not pc.colliders and not pc.collision_only
        pl = sorted((p.position, p.yaw) for p in m.placements if p.part == n)
        assert pl == [((-9.0, 2.0, 3.0), 0.0), ((1.0, 2.0, 3.0), 30.0)]
    # compact: each piece covers a slice of the ring, not the whole of it
    full = np.ptp(np.concatenate([s.positions for s in subs]), 0)
    for n in ["mlo_dome"] + pieces:
        ext = np.ptp(np.concatenate([s.positions for s in m.parts[n].submeshes]), 0)
        assert ext[0] * ext[1] < 0.6 * full[0] * full[1], (n, ext, full)
    # small parts and door leaves are left alone
    assert "mlo_house" not in rep["mesh_split_parts"] and "chunk_500_500" not in rep["mesh_split_parts"]


def test_chunk_groups_respect_the_chunk_and_vertex_caps():
    from fivem2open77.mlo import build as MB
    from types import SimpleNamespace
    rng = np.random.default_rng(3)
    for n, cap, vcap in ((271, 64, 400_000), (65, 64, 10 ** 9), (300, 64, 50_000), (5, 64, 10), (130, 7, 10 ** 9)):
        subs = [SimpleNamespace(positions=rng.uniform(-100, 100, (3, 3)) + rng.uniform(-500, 500, 3),
                                vertex_count=int(rng.integers(100, 60_000))) for _ in range(n)]
        groups = MB._chunk_groups(subs, cap, vcap)
        assert sum(len(g) for g in groups) == n and all(1 <= len(g) <= cap for g in groups), (n, [len(g) for g in groups])
        want = min(n, max(math.ceil(n / cap), math.ceil(sum(s.vertex_count for s in subs) / vcap)))
        assert len(groups) == want, (n, len(groups), want)
    assert MB._chunk_groups([], 64, 1000) == []


def test_meshes_with_too_many_chunks_are_refused():
    from fivem2open77.cp77 import mesh as M
    donor = {"Data": {"RootChunk": {"localMaterialBuffer": {}, "appearances": [{"Data": {}}]}}}
    try:
        M.template_doc(donor, {"m": {}}, ["m"] * (M.MAX_RENDER_CHUNKS + 1))
    except ValueError as e:
        assert "render chunks" in str(e)
    else:
        raise AssertionError("a mesh with 256 chunks must be refused")


def test_broken_normals_and_tangents_are_repaired():
    from fivem2open77.mlo import build as MB
    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]], float)
    tri = np.array([[0, 1, 2], [1, 3, 2]])
    nrm = np.array([[0, 0, 1], [0, 0, 0], [np.nan, 0, 1], [0, 0, 1e-9]], float)
    tan = np.array([[1, 0, 0, 1], [0, 0, 0, -1], [np.nan, 0, 0, 1], [1, 0, 0, 0]], float)
    n2, t2 = MB._repair_frames(pos, tri, nrm, tan)
    assert np.isfinite(n2).all() and np.isfinite(t2).all()
    assert (np.linalg.norm(n2, axis=1) > 0.5).all()
    assert np.allclose(MB._normalize(n2), [[0, 0, 1]] * 4)
    assert np.allclose(np.abs(np.sum(t2[:, :3] * MB._normalize(n2), axis=1)), 0, atol=1e-6)
    assert (np.linalg.norm(t2[:, :3], axis=1) > 0.5).all() and set(np.abs(t2[:, 3])) == {1.0}
    assert t2[1, 3] == -1.0                       # a good handedness is kept
    good_n, good_t = MB._repair_frames(pos, tri, np.tile([0.0, 0, 1], (4, 1)), np.tile([1.0, 0, 0, 1], (4, 1)))
    assert np.array_equal(good_t, np.tile([1.0, 0, 0, 1], (4, 1)))


def test_ghost_sees_a_cut_building_as_one():
    """The pieces of a cut mesh change nothing in the wireframe: same footprint, one building box."""
    from fivem2open77.mlo import build as MB
    whole = _meshed_model()
    cut = _meshed_model()
    house = cut.parts["obj_house"]
    tris = list(_box_tris((-10, -5, 0), (10, 5, 6)))
    house.submeshes = [_mesh(tris[:6]), _mesh(tris[6:])]
    old = MB.MAX_CHUNKS
    MB.MAX_CHUNKS = 1
    try:
        rep = {}
        _builder(cut)._split_wide_parts(rep)
    finally:
        MB.MAX_CHUNKS = old
    assert rep["mesh_split_parts"]["obj_house"]["pieces"] == ["obj_house_m1"], rep
    assert "door_main_gate" not in rep["mesh_split_parts"]
    a = [tuple(round(v, 4) if isinstance(v, float) else v for v in ln) for ln in G.ghost_lines(whole)]
    b = [tuple(round(v, 4) if isinstance(v, float) else v for v in ln) for ln in G.ghost_lines(cut)]
    assert a == b


def test_spots_with_a_minus_sign_on_the_command_line():
    """`--anchor -3096.573,-848.458,67.991,-41.65` (west and south of the centre): argparse took the
    value for an option, the deploy's rebuild failed, and Sea Dome stayed at its first spot."""
    import contextlib
    import io
    from fivem2open77 import __main__ as M
    from fivem2open77.argv import glue_vectors
    from fivem2open77.mlo import baker as BK
    spot = "-3096.573,-848.458,67.991,-41.65"
    assert glue_vectors(["bake", "seadome", "--anchor", spot, "--in-place"]) == \
        ["bake", "seadome", f"--anchor={spot}", "--in-place"]
    assert glue_vectors(["--anchor", "12,3,4", "--place", "-1,2,3"]) == ["--anchor", "12,3,4", "--place=-1,2,3"]
    assert glue_vectors(["--anchor", "--json"]) == ["--anchor", "--json"]
    assert glue_vectors(["--anchor", "-v"]) == ["--anchor", "-v"]
    tmp = tempfile.mkdtemp(prefix="f2o77-argv-")
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = BK.main(["bake", "seadome", "--anchor", spot, "--in-place", "--server-dir", tmp])
        assert rc == 2 and '"ok": false' in out.getvalue(), out.getvalue()      # parsed: no such map here
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            try:
                M.main(["mlo-bake", os.path.join(tmp, "nothing"), "--anchor", spot])
            except SystemExit:
                pass
            except Exception:   # noqa: BLE001 - the missing map, after parsing
                pass
        assert "expected one argument" not in err.getvalue(), err.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    src = open(os.path.join(HERE, "..", "tools", "deploy", "remote", "map_deploy.sh")).read()
    assert '--anchor="$old"' in src and '--anchor "$old"' not in src


def test_coverage_of_a_tiny_map():
    from fivem2open77.mlo import coverage as CV
    m = _model()
    # the reference collision: the house shell where the house colliders were made from, and the ground
    house = Collision(triangles=_box_tris((-10, -5, 0), (10, 5, 6)), tri_materials=np.zeros(12, np.int32))
    mlo = SimpleNamespace(name="house", matrix=trs((3.0, 4.0, 0.5), (0, 0, 0, 1)), collision=house, children=[])
    gt = np.array(_quad_tris(-20, -20, 20, 20, 0.0, n=4), float)
    scene = SimpleNamespace(mlos=[mlo], instances=[], world_collisions=[("ground", Collision(
        triangles=gt, tri_materials=np.zeros(len(gt), np.int32)))], entity_collision=lambda h: Collision())
    rep = CV.coverage(scene, m, density=4.0)
    assert rep["walkable"]["covered_pct"] > 99.0 and rep["walls"]["covered_pct"] > 95.0, rep
    m.parts["chunk_500_500"].colliders = []                 # no ground collision at all
    rep = CV.coverage(scene, m, density=4.0)
    assert rep["walkable"]["covered_pct"] < 50.0 and rep["holes"], rep


def _staircase(steps=10, rise=0.2, run=0.28, width=1.2, x0=0.0, z0=0.0):
    """Treads and risers of a straight flight going +x, with the floor before and the landing after."""
    tris = []

    def quad(p, q, r, s_):
        tris.extend([(p, q, r), (p, r, s_)])
    for i in range(steps):
        x, z = x0 + i * run, z0 + i * rise
        quad((x, 0, z), (x, 0, z + rise), (x, width, z + rise), (x, width, z))                       # riser, faces -x
        quad((x, 0, z + rise), (x + run, 0, z + rise), (x + run, width, z + rise), (x, width, z + rise))  # tread, faces up
    xe, ze = x0 + steps * run, z0 + steps * rise
    tris.extend([((x0 - 3, 0, z0), (x0, width, z0), (x0 - 3, width, z0)), ((x0 - 3, 0, z0), (x0, 0, z0), (x0, width, z0))])
    tris.extend([((xe, 0, ze), (xe + 3, width, ze), (xe, width, ze)), ((xe, 0, ze), (xe + 3, 0, ze), (xe + 3, width, ze))])
    return np.array(tris, float)


def test_stairs_stay_climbable_whatever_the_budget():
    from fivem2open77.mlo import coverage as CV
    t = _staircase()
    # make it big enough that a small budget would have asked for the coarsest merging before
    col = Collision(triangles=t, tri_materials=np.zeros(len(t), np.int32))
    for budget in (5, 2500):
        cols, note = C.build_colliders(col, budget=budget)
        caster = CV.BoxCaster(*CV.obb_arrays(cols))
        for i in range(10):
            p = np.array([[0.14 + i * 0.28, 0.6, (i + 1) * 0.2]])
            above = caster.cast(p + [0, 0, 0.25], np.array([[0.0, 0.0, -1.0]]), 1.0)[0] - 0.25
            assert -0.03 < above < 0.06, (budget, i, above, note.get("simplification"))   # a tread, not a block


def test_prop_collision_by_size_and_shape():
    from fivem2open77.mlo import coverage as CV
    zero = lambda t: Collision(triangles=t, tri_materials=np.zeros(len(t), np.int32))
    chair = zero(_box_tris((0, 0, 0), (0.5, 0.5, 0.9)))
    assert len(C.prop_colliders(chair)) == 1
    # an L-shaped low sofa, 3 x 3 m: the floor inside the L stays open
    sofa = zero(np.concatenate([_box_tris((0, 0, 0), (3, 0.9, 0.8)), _box_tris((0, 0.9, 0), (0.9, 3, 0.8))]))
    cols = C.prop_colliders(sofa)
    caster = CV.BoxCaster(*CV.obb_arrays(cols))
    inside = lambda q: caster.cast(np.array([q]), np.array([[0.0, 0.0, -1.0]]), 0.01)[0] < 1e-9
    assert inside((1.5, 0.45, 0.4)) and inside((0.45, 2.0, 0.4)), "the sofa is solid"
    assert not inside((2.0, 2.0, 0.4)), "the floor inside the L is open"
    assert len(cols) <= 20, len(cols)
    # a staircase prop (taller than furniture): its treads stay steps
    stairs = zero(_staircase(steps=12))
    cols = C.prop_colliders(stairs)
    caster = CV.BoxCaster(*CV.obb_arrays(cols))
    p = np.array([[0.14 + 5 * 0.28, 0.6, 6 * 0.2 + 0.25]])
    assert abs(caster.cast(p, np.array([[0.0, 0.0, -1.0]]), 1.0)[0] - 0.25) < 0.06


def test_colliders_follow_their_object():
    b = C.Collider("box", "wood.physmat", center=(1.0, 0.0, 0.5), half=(0.5, 0.2, 0.5))
    m = trs((10.0, 20.0, 0.0), (0, 0, math.sin(math.pi / 4), math.cos(math.pi / 4)))
    t = C.transform_colliders([b], m)[0]
    assert np.allclose(t.center, (10.0, 21.0, 0.5)) and np.allclose(t.half, b.half)
    assert np.allclose(np.abs(t.rotation), (0, 0, math.sin(math.pi / 4), math.cos(math.pi / 4)), atol=1e-6)


def _wall_with_door(length=12.0, height=3.0, door_x=(5.0, 6.0), door_h=2.1):
    """A wall in the x-z plane (facing -y), triangulated around a doorway cut out of its bottom edge
    (shared edges, as an exported collision mesh has them)."""
    a, b = door_x
    pts2 = [[(0, 0), (a, 0), (a, door_h)], [(0, 0), (a, door_h), (0, height)], [(0, height), (a, door_h), (b, door_h)],
            [(0, height), (b, door_h), (length, height)], [(b, door_h), (b, 0), (length, 0)],
            [(b, door_h), (length, 0), (length, height)]]
    tris = []
    for tri in pts2:
        t3 = np.array([(x, 0.0, z) for x, z in tri], float)
        n = np.cross(t3[1] - t3[0], t3[2] - t3[0])
        tris.append(t3 if n[1] < 0 else t3[::-1])
    return np.array(tris)


def test_doorways_stay_open():
    from fivem2open77.mlo import coverage as CV
    t = _wall_with_door()
    col = Collision(triangles=t, tri_materials=np.zeros(len(t), np.int32))
    for cols in (C.build_colliders(col)[0], C.ground_colliders(col)[0]):
        caster = CV.BoxCaster(*CV.obb_arrays(cols))
        probes = np.array([[x, y, z] for x in (5.1, 5.5, 5.9) for y in (-0.05, 0.0, 0.05) for z in (0.3, 1.0, 1.9)])
        inside = caster.cast(probes, np.tile([0.0, 0.0, -1.0], (len(probes), 1)), 0.001) < 1e-9
        assert not inside.any(), probes[inside]
        # and the wall beside the door is solid
        wall = caster.cast(np.array([[2.0, 0.05, 1.0], [9.0, 0.05, 1.0]]), np.tile([0.0, 0.0, -1.0], (2, 1)), 0.001)
        assert (wall < 1e-9).all(), wall


def test_doorways_stay_open_when_the_collision_is_doubled():
    """Sea Dome's main entrance: the interior's collision holds the entrance hall twice (two
    overlapping children).  The doubled area made the wall around the doorway pass the "nearly
    convex" test as a filled rectangle, and one slab closed the doorway: an invisible wall."""
    from fivem2open77.mlo import coverage as CV
    t = _wall_with_door(length=13.4, height=5.5, door_x=(3.0, 10.5), door_h=3.7)
    # the same faces twice; then two different triangulations of the same wall (no shared faces)
    t2 = _wall_with_door(length=13.4, height=5.5, door_x=(3.0, 10.5), door_h=3.7)
    alt = []
    for tri in t2:                                  # flip each quad's diagonal: other triangles, same surface
        alt.append(tri[[1, 2, 0]])
    cases = {"doubled": np.concatenate([t, t]), "doubled and turned": np.concatenate([t, np.array(alt)[:, ::-1]])}
    for what, tris in cases.items():
        col = Collision(triangles=tris, tri_materials=np.zeros(len(tris), np.int32))
        for coarse in (False, True):
            if coarse:
                cols = C.triangles_to_colliders(tris, np.zeros(len(tris), np.int32), weld=0.15, angle_tol=8.0,
                                                dist_tol=0.06, min_area=0.05, thickness=0.15, max_extra=0.15,
                                                max_boxes=16)
            else:
                cols = C.build_colliders(col)[0]
            caster = CV.BoxCaster(*CV.obb_arrays(cols))
            probes = np.array([[x, y, z] for x in (3.5, 6.0, 9.5, 10.2) for y in (-0.05, 0.0, 0.05)
                               for z in (0.3, 1.0, 1.9, 3.3)])
            inside = caster.cast(probes, np.tile([0.0, 0.0, -1.0], (len(probes), 1)), 0.001) < 1e-9
            assert not inside.any(), (what, coarse, probes[inside])
            wall = caster.cast(np.array([[1.5, 0.05, 1.0], [12.0, 0.05, 1.0], [6.0, 0.05, 4.5]]),
                               np.tile([0.0, 0.0, -1.0], (3, 1)), 0.001)
            assert (wall < 1e-9).all(), (what, coarse, wall)


def test_hull_filled_sees_a_hole_overlapping_triangles_hide():
    # a square with a square hole, plus a big triangle pair over the hole's left half only
    sq = np.array([[0, 0], [4, 0], [4, 4], [0, 4]], float)
    verts = np.array([[x, 0.0, z] for x, z in [(0, 0), (4, 0), (4, 4), (0, 4), (1, 1), (3, 1), (3, 3), (1, 3)]])
    ring = [(0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    u, v = np.array([1.0, 0.0, 0.0]), np.array([0.0, 0.0, 1.0])
    assert not C._hull_filled(verts, np.array(ring), u, v, sq, 0.2), "the hole is seen"
    full = ring + [(4, 5, 6), (4, 6, 7)]
    assert C._hull_filled(verts, np.array(full), u, v, sq, 0.2), "a filled square is filled"


def test_doors_open_and_gates_block_by_default():
    from fivem2open77.mlo.build import passable_re
    r = passable_re()
    assert r.search("zbadoor") and r.search("v_ilev_garageliftdoor") and r.search("prop_shutter_01")
    assert not r.search("zextgate") and not r.search("prop_barrier_work05") and not r.search("zzzgateblck")
    g = passable_re(open_gates=True)
    assert g.search("zextgate") and g.search("zbadoor")
    assert passable_re(solid_doors=True) is None and passable_re(True, True).search("gate") and not passable_re(True, True).search("door")


# ------------------------------------------------------------------------------ doors, gates, windows
def test_door_kind_from_the_leaf_shape():
    from fivem2open77.mlo import doors as DR
    # Cedar Crest's double-door leaf: hinge on the model origin, 2 m wide along +x
    d = DR.door_kind("zbadoor", (-0.07, -0.05, -0.07), (1.97, 0.05, 2.28), is_door=True, is_gate=False)
    assert d["opening"] == DR.HINGED and d["axis"] == "x" and d["pivot"] == [0.0, 0.0]
    assert abs(d["center"][0] - 0.95) < 1e-9 and d["zmin"] == -0.07 and d["solid"]
    # the estate's driveway gate: 7 m panel -> slides, device in the middle of the leaf
    g = DR.door_kind("zzzgateblck", (-0.08, -0.08, 0.0), (7.12, 0.08, 2.58), is_door=False, is_gate=True)
    assert g["opening"] == DR.SLIDING and g["pivot"] == [3.52, 0.0] and g["center"] == [0.0, 0.0]
    assert g["slide"] == -7.2 and not g["solid"]                  # retracts past the origin end
    # the pedestrian gate: 2.4 m, origin mid-height -> swings on the origin
    p = DR.door_kind("zzzblingg_gate", (0.0, -0.05, -1.36), (2.4, 0.05, 1.36), is_door=False, is_gate=True)
    assert p["opening"] == DR.HINGED and p["zmin"] == -1.36
    # a leaf along y, modelled around its middle: swings about the edge nearer the origin
    m = DR.door_kind("mid_door", (-0.05, -0.6, 0.0), (0.05, 0.4, 2.1), is_door=True, is_gate=False)
    assert m["axis"] == "y" and m["pivot"] == [0.0, 0.4] and abs(m["center"][1] + 0.5) < 1e-9
    assert DR.door_kind("x_shutter_01", (0, 0, 0), (3.0, 0.1, 2.5), is_door=True, is_gate=False)["opening"] == DR.ROLLING
    # not leaves: the gatehouse, a door frame wall, things too small
    assert DR.door_kind("ethanos_gate", (-13.07, -4.35, -4.8), (13.07, 4.35, 4.8), is_door=False, is_gate=True) is None
    assert DR.door_kind("door_wall", (0, 0, 0), (8.0, 0.2, 3.0), is_door=True, is_gate=False) is None
    assert DR.door_kind("doorstop", (0, 0, 0), (0.1, 0.1, 0.1), is_door=True, is_gate=False) is None
    assert DR.door_kind("table", (0, 0, 0), (1, 1, 1), is_door=False, is_gate=False) is None
    box = DR.leaf_box(g, "metal.physmat")
    assert box.kind == "box" and abs(box.half[0] - 3.6) < 1e-9 and abs(box.half[1] - 0.08) < 1e-9
    assert abs(box.center[2] - 1.29) < 1e-9
    assert DR.leaf_box(d, "wood.physmat").half[1] == DR.MIN_LEAF_THICKNESS / 2      # a 10 cm leaf: 12 cm box


def test_device_ids_follow_the_games_node_ref_hash():
    from fivem2open77.mlo import doors as DR
    ref = DR.node_ref("cedar_crest", 7)
    assert ref == "$/fivem2open77/cedar_crest/#door_007"
    # FNV-1a 64 of the path without '#': what 03_night_city.devices lists for vanilla devices and
    # what WolvenKit writes for a NodeRef (both checked against the game files)
    h = 0xCBF29CE484222325
    for b in b"$/fivem2open77/cedar_crest/door_007":
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    assert DR.node_ref_hash(ref) == h and DR.door_id(ref) == f"0x{h:016X}" and len(DR.door_id(ref)) == 18
    assert DR.node_ref_hash("$/03_night_city/#a/b") == DR.node_ref_hash("$/03_night_city/a/b")


def _door_template():
    """The shape of the game's fence_barred_large.ent (handles defined in the package chunks,
    referenced from the component list)."""
    from fivem2open77.cp77.wkjson import cname, depot

    def h(i, data):
        return {"HandleId": str(i), "Data": data}

    def lt():
        z = {"$type": "FixedPoint", "Bits": 0}
        return {"$type": "WorldTransform", "Orientation": {"$type": "Quaternion", "i": 0, "j": 0, "k": 0, "r": 1},
                "Position": {"$type": "WorldPosition", "x": dict(z), "y": dict(z), "z": dict(z)}}

    def move(i, name, target):
        return {"$type": "gameTransformAnimationDefinition", "name": cname(name), "timeline": {"items": [
            {"$type": "gameTransformAnimationTrackItem", "duration": 1.0, "impl": h(i, {
                "$type": "gameTransformAnimation_Move",
                "targetPositionEvaluator": h(i + 1, {"$type": "gameTransformAnimation_Position_LocalPosition",
                                                     "position": {"$type": "Vector3", "X": target[0], "Y": target[1], "Z": target[2]}})})}]}}

    def area(name, i):
        return {"$type": "gameStaticTriggerAreaComponent", "name": cname(name), "localTransform": lt(),
                "outline": h(i, {"$type": "AreaShapeOutline", "height": 2, "points": []})}

    door = {"$type": "Door", "animationType": "REGULAR", "doorOpeningType": "SLIDING_HORIZONTALLY"}
    package = [
        dict(door),
        {"$type": "entPhysicalMeshComponent", "name": cname("mesh"), "mesh": depot("base\\old.mesh"),
         "meshAppearance": cname("rusty"), "localTransform": lt(), "simulationType": "Kinematic",
         "filterData": h(0, {"$type": "physicsFilterData", "preset": cname("Device transparent")})},
        area("trigger", 1), area("SideOne", 2), area("SideTwo", 3),
        {"$type": "gameinteractionsComponent", "name": cname("interaction"), "localTransform": lt()},
        {"$type": "gameTransformAnimatorComponent", "name": cname("DoorTransformAnimator"), "localTransform": lt(),
         "animations": [move(4, "doorSlideHorizontally", (2.5, 0, 0)), move(6, "doorSlideVertically", (0, 0, 2.4))]},
        {"$type": "DoorController", "name": cname("controller"), "persistentState": h(8, {
            "$type": "DoorControllerPS", "doorProperties": {"$type": "DoorSetup", "doorType": "INTERACTIVE",
                                                            "automaticallyClosesItself": 0, "initialDoorState": "CLOSED",
                                                            "isShutter": 0}})},
        {"$type": "entMeshComponent", "name": cname("NumberPlateScreenMesh_side_1"), "isEnabled": 1},
    ]

    def refs(o):
        if isinstance(o, dict):
            if "HandleId" in o and "Data" in o:
                return {"HandleRefId": o["HandleId"]}
            return {k: refs(v) for k, v in o.items()}
        if isinstance(o, list):
            return [refs(v) for v in o]
        return o

    root = {"$type": "entEntityTemplate", "compiledData": {"Data": {"Chunks": package}},
            "components": [refs(c) for c in package[1:]], "entity": h(9, dict(door)),
            "inplaceResources": [depot("base\\old.mesh", "Embedded")],
            "resolvedDependencies": [depot("base\\old.mesh", "Embedded")]}
    return {"Header": {}, "Data": {"RootChunk": root, "EmbeddedFiles": [{"FileName": "old.mesh"}]}}


def test_door_entity_from_the_game_template():
    from fivem2open77.mlo import doors as DR
    tpl = _door_template()
    gate = DR.door_kind("zzzgateblck", (-0.08, -0.08, 0.0), (7.12, 0.08, 2.58), is_door=False, is_gate=True)
    doc = DR.door_entity_doc(tpl, r"fivem2open77\maps\m\meshes\door_gate.mesh", gate)
    assert tpl["Data"]["EmbeddedFiles"], "the template is not modified"
    root = doc["Data"]["RootChunk"]
    assert not doc["Data"]["EmbeddedFiles"] and not root["inplaceResources"] and not root["resolvedDependencies"]
    pkg = root["compiledData"]["Data"]["Chunks"]
    by = {c["name"]["$value"]: c for c in pkg[1:]}
    for e in (pkg[0], root["entity"]["Data"]):
        assert e["animationType"] == "TRANSFORM" and e["doorOpeningType"] == DR.SLIDING
    for c in (by["mesh"], root["components"][0]):
        assert c["mesh"]["DepotPath"]["$value"].endswith("door_gate.mesh") and c["meshAppearance"]["$value"] == "default"
    assert by["mesh"]["filterData"]["Data"]["preset"]["$value"] == "Device transparent"      # a gate: see-through
    assert root["components"][0]["filterData"] == {"HandleRefId": "0"}
    trig = by["trigger"]["outline"]["Data"]["points"]
    assert min(p["X"] for p in trig) == -4.6 and max(p["X"] for p in trig) == 4.6     # 7.2 m leaf + 1 m each side
    assert max(p["Y"] for p in trig) == 2.5 and by["trigger"]["outline"]["Data"]["height"] >= 2.5
    s1 = by["SideOne"]["outline"]["Data"]["points"]
    assert min(p["Y"] for p in s1) == 0.2 and max(p["Y"] for p in s1) == 5.0
    slide = by["DoorTransformAnimator"]["animations"][0]["timeline"]["items"][0]
    assert slide["impl"]["Data"]["targetPositionEvaluator"]["Data"]["position"]["X"] == -7.2 and slide["duration"] == 2.4
    props = by["controller"]["persistentState"]["Data"]["doorProperties"]
    assert props["doorType"] == "AUTOMATIC" and props["automaticallyClosesItself"] == 1
    assert by["NumberPlateScreenMesh_side_1"]["isEnabled"] == 0
    # a solid door leaf along y: sight-blocking filter, trigger turned with the leaf
    leaf = DR.door_kind("door_y", (-0.05, 0.0, 0.0), (0.05, 1.2, 2.2), is_door=True, is_gate=False)
    d2 = DR.door_entity_doc(_door_template(), "x.mesh", leaf)["Data"]["RootChunk"]["compiledData"]["Data"]["Chunks"]
    by2 = {c["name"]["$value"]: c for c in d2[1:]}
    assert by2["mesh"]["filterData"]["Data"]["preset"]["$value"] == "Device solid visible"
    pts = by2["trigger"]["outline"]["Data"]["points"]
    assert max(p["Y"] for p in pts) == 1.6 and max(p["X"] for p in pts) == 2.5
    assert by2["trigger"]["localTransform"]["Position"]["y"]["Bits"] == round(0.6 * 131072)
    assert d2[0]["doorOpeningType"] == DR.HINGED


def _door_layout():
    from fivem2open77.mlo import doors as DR
    L = _layout()
    base = r"fivem2open77\maps\cedar_crest"
    L["parts"]["door_zbadoor"] = {"mesh": base + r"\meshes\door_zbadoor.mesh", "ent": base + r"\ents\door_zbadoor.ent",
                                  "kind": "door", "radius": 1.6, "collision": True, "bounds": [[-0.1, -0.1, 0], [2, 0.1, 2.3]],
                                  "door": DR.door_kind("zbadoor", (-0.07, -0.05, -0.07), (1.97, 0.05, 2.28), is_door=True, is_gate=False)}
    L["parts"]["door_zextgate"] = {"mesh": base + r"\meshes\door_zextgate.mesh", "ent": base + r"\ents\door_zextgate.ent",
                                   "kind": "door", "radius": 3.6, "collision": True, "bounds": [[-3.6, -0.1, 0], [3.6, 0.1, 2.5]],
                                   "door": DR.door_kind("zextgate", (-0.49, -0.07, -0.07), (6.61, 0.07, 2.4), is_door=False, is_gate=True)}
    L["placements"] += [{"part": "door_zbadoor", "x": 1.0, "y": 2.0, "z": 0.0, "yaw": 90.0},
                        {"part": "door_zbadoor", "x": 1.0, "y": 5.94, "z": 0.0, "yaw": -90.0},
                        {"part": "door_zextgate", "x": 10.0, "y": 0.0, "z": 0.0, "yaw": 180.0}]
    return L


def test_sector_doors_are_device_nodes_listed_in_devices():
    from fivem2open77.mlo import doors as DR
    L = _door_layout()
    anchor = {"x": 100.0, "y": 200.0, "z": 50.0, "yaw": 90.0}
    doc, _ = W.sector_doc(L, anchor)
    root = doc["Data"]["RootChunk"]
    nodes, data = root["nodes"], root["nodeData"]["Data"]
    kinds = [n["Data"]["$type"] for n in nodes]
    assert kinds == ["worldEntityNode"] * 3 + ["worldDeviceNode"] * 3
    devs = W.door_devices(L, anchor)
    assert [d["ref"] for d in devs] == [DR.node_ref("cedar_crest", i) for i in range(3)]
    assert root["nodeRefs"] == [DR.node_ref_json(d["ref"]) for d in devs]
    for n, d, dev in zip(nodes[3:], data[3:], devs):
        assert n["Data"]["deviceClassName"]["$value"] == "None" and n["Data"]["instanceData"] is None
        assert n["Data"]["entityTemplate"]["DepotPath"]["$value"] == L["parts"][dev["part"]]["ent"]
        assert d["QuestPrefabRefHash"] == {"$type": "NodeRef", "$storage": "string", "$value": dev["ref"]}
        assert abs(d["Position"]["X"] - dev["position"][0]) < 1e-6 and abs(d["Position"]["Y"] - dev["position"][1]) < 1e-6
        assert dev["id"] == DR.door_id(dev["ref"])
    # first leaf: local (1, 2) turned 90 degrees about the anchor; its yaw 90 + 90; the leaf's middle
    # is 0.95 m along the leaf's +x, which now points to world -x
    d0 = devs[0]
    assert np.allclose(d0["position"], (100 - 2, 200 + 1, 50)) and abs(abs(d0["yaw"]) - 180) < 1e-9
    assert np.allclose(d0["center"], (98 - 0.95, 201, 49.93)) and d0["radius"] == 4.02          # 2.04 m leaf
    assert devs[2]["opening"] == DR.SLIDING and devs[2]["radius"] == 6.55
    dd = DR.devices_doc([(d["ref"], d["position"]) for d in devs])["Data"]["RootChunk"]
    rows = dd["data"]["Data"]["unk1"]
    assert dd["$type"] == "gameDeviceResource" and dd["data"]["Data"]["version"] == 2 and len(rows) == 3
    assert rows[0]["className"]["$value"] == "DoorControllerPS" and rows[0]["hash"] == str(DR.node_ref_hash(devs[0]["ref"]))
    assert rows[1]["nodePosition"]["X"] == devs[1]["position"][0] and rows[0]["parents"] == [] == rows[0]["children"]
    text = W.archive_xl(r"a\s.streamingblock", r"a\s.devices")
    assert "resource:\n  patch:\n    a\\s.devices:\n      targets:\n        - " + DR.WORLD_DEVICES in text
    try:
        import yaml
    except ImportError:
        return
    assert yaml.safe_load(text) == {"streaming": {"blocks": [r"a\s.streamingblock"]},
                                    "resource": {"patch": {r"a\s.devices": {"targets": [DR.WORLD_DEVICES]}}}}


def test_bake_with_doors_writes_devices_and_registers_them():
    root = tempfile.mkdtemp()
    res = _converted_map(root)
    L = _door_layout()
    with open(os.path.join(res, "layout.json"), "w", encoding="utf-8") as f:
        json.dump(L, f)
    wk = _FakeWk()
    seen = []
    real = wk.deserialize_tree

    def spy(json_root, out_root, scratch):
        for dp, _, fns in os.walk(json_root):
            seen.extend(os.path.relpath(os.path.join(dp, fn), json_root) for fn in fns)
        real(json_root, out_root, scratch)
    wk.deserialize_tree = spy
    r = B.bake(res, B.parse_anchor("480,1800,174,0"), wk, log=lambda *a: None)
    assert os.path.join("fivem2open77", "maps", "cedar_crest", "sector", "cedar_crest.devices.json") in seen, seen
    with zipfile.ZipFile(os.path.join(res, "dist", "cedar_crest_sector.zip")) as zf:
        xl = zf.read("archive/pc/mod/fivem2open77_map_cedar_crest_sector.archive.xl").decode()
    assert "cedar_crest.devices:" in xl and "03_night_city.devices" in xl
    assert r["baked"]["doors"] == 3 and r["baked"]["devices"].endswith("cedar_crest.devices")
    server = open(os.path.join(res, "server", "main.lua"), encoding="utf-8").read()
    devs = json.load(open(os.path.join(res, "doors.json")))
    assert len(devs) == 3 and all(d["id"] in server for d in devs)
    assert "Open77.exports.call('open77_doors'" in server and "openRadius = d.radius" in server
    luac = shutil.which("luac5.4") or shutil.which("luac")
    if luac:
        rr = subprocess.run([luac, "-p", os.path.join(res, "server", "main.lua")], capture_output=True, text=True)
        assert rr.returncode == 0, rr.stderr
    # no doors any more (re-baked from a layout without them): no devices, no registration
    with open(os.path.join(res, "layout.json"), "w", encoding="utf-8") as f:
        json.dump(_layout(), f)
    B.bake(res, B.parse_anchor("480,1800,174,0"), _FakeWk(), log=lambda *a: None)
    assert not os.path.exists(os.path.join(res, "doors.json"))
    assert "open77_doors" not in open(os.path.join(res, "server", "main.lua"), encoding="utf-8").read()


def test_door_registration_lua_runs():
    lua = shutil.which("lua5.4") or shutil.which("lua")
    if not lua:
        print("   (skipped: no lua5.4)")
        return
    from fivem2open77.mlo import doors as DR
    body = DR.register_lua("cedar_crest", [{"id": "0x00000000000000AA", "position": (1.0, 2.0, 3.0), "radius": 4.0},
                                           {"id": "0x00000000000000BB", "position": (5.0, 6.0, 7.0), "radius": 6.5}])
    harness = r"""
local calls, threads = {}, {}
function Wait(ms) end
function CreateThread(fn) threads[#threads + 1] = fn end
local mode = ...
Open77 = { exports = { call = function(res, method, ...)
    calls[#calls + 1] = { res = res, method = method, args = { ... } }
    if mode == "missing" then return nil, "resource_not_found" end
    local args = { ... }
    return { await = function()
        if method == "list" then return { doors = {}, total = 0 } end
        if method == "register" then return { id = args[1].id } end
        if method == "configure" then
            local p = args[3]
            assert(p.openRadius >= 1 and p.openRadius <= 12 and p.closeRadius >= p.openRadius and p.closeRadius <= 12)
            assert(p.closeDelay >= 0.2 and p.automatic == true)
            return true
        end
    end }
end } }
""" + body + r"""
for _, t in ipairs(threads) do t() end
local n = { list = 0, register = 0, configure = 0 }
for _, c in ipairs(calls) do assert(c.res == "open77_doors"); n[c.method] = n[c.method] + 1 end
if mode == "missing" then
    assert(n.register == 0 and n.list == 12, "gives up after the retries")
else
    assert(n.list == 1 and n.register == 2 and n.configure == 2)
    assert(calls[2].args[1].id == "0x00000000000000AA" and calls[2].args[1].position.z == 3.0)
end
print("OK doors " .. mode)
"""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "doors.lua")
    with open(p, "w", encoding="utf-8") as f:
        f.write(harness)
    for mode in ("present", "missing"):
        r = subprocess.run([lua, p, mode], capture_output=True, text=True)
        assert r.returncode == 0 and f"OK doors {mode}" in r.stdout, r.stdout + r.stderr


def _geo(tris, mat="m_wood"):
    """Synthetic drawable geometry (what MapBuilder.drawable_geometry caches) from triangles."""
    t = np.asarray(tris, float).reshape(-1, 3, 3)
    pos = t.reshape(-1, 3)
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    n = np.repeat(n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12), 3, axis=0)
    tan = np.tile([1.0, 0.0, 0.0, 1.0], (len(pos), 1))
    return [(mat, {"position": pos, "normal": n, "tangent": tan, "uv0": np.zeros((len(pos), 2)), "uv1": None,
                   "color": None}, np.arange(len(pos)).reshape(-1, 3))]


def _door_scene():
    """An MLO room (12 x 8 m, a doorway and a window hole in its front wall) with a door leaf and a
    window pane, plus a 7 m sliding gate and a gatehouse outside."""
    from fivem2open77.gta.bounds import Collision as Col
    from fivem2open77.mlo.scene import Instance, MloPlacement
    from fivem2open77.model.ir import Material
    wall = _wall_with_door(length=12.0, height=3.0, door_x=(5.0, 6.0), door_h=2.1)
    # a window hole 1..3 m along, 1..2 m up: cut the triangles of the left part away in that box
    # (the wall collision keeps a hole there, as GTA's does)
    left = np.array([[[0, 0, 0], [1, 0, 0], [1, 0, 3]], [[0, 0, 0], [1, 0, 3], [0, 0, 3]],
                     [[1, 0, 0], [3, 0, 0], [3, 0, 1]], [[1, 0, 0], [3, 0, 1], [1, 0, 1]],
                     [[1, 0, 2], [3, 0, 2], [3, 0, 3]], [[1, 0, 2], [3, 0, 3], [1, 0, 3]],
                     [[3, 0, 0], [5, 0, 0], [5, 0, 3]], [[3, 0, 0], [5, 0, 3], [3, 0, 3]]], float)
    left = np.array([t if np.cross(t[1] - t[0], t[2] - t[0])[1] < 0 else t[::-1] for t in left])
    right = wall[[4, 5]]
    lintel = np.array([[[5, 0, 2.1], [6, 0, 2.1], [6, 0, 3]], [[5, 0, 2.1], [6, 0, 3], [5, 0, 3]]], float)
    lintel = np.array([t if np.cross(t[1] - t[0], t[2] - t[0])[1] < 0 else t[::-1] for t in lintel])
    floor = _quad_tris(-2, -1, 14, 9, 0.0)
    shell = np.concatenate([left, right, lintel, floor])
    mlo_col = Col(triangles=shell, tri_materials=np.zeros(len(shell), np.int32))
    door_tris = _box_tris((0.0, -0.05, 0.0), (1.0, 0.05, 2.1))
    pane = np.array([[[1, -0.02, 1], [3, -0.02, 1], [3, -0.02, 2]], [[1, -0.02, 1], [3, -0.02, 2], [1, -0.02, 2]]], float)
    gate_tris = _box_tris((0.0, -0.07, 0.0), (7.0, 0.07, 2.4))
    house_tris = _box_tris((-4.0, -3.0, 0.0), (4.0, 3.0, 5.0))
    H_DOOR, H_PANE, H_GATE, H_HOUSE, H_MLO = 11, 12, 13, 14, 15
    children = [Instance(H_DOOR, "test_door", trs((5.0, 0.0, 0.0), (0, 0, 0, 1)), 0, "mlo#0"),
                Instance(H_PANE, "window_glass", np.eye(4), 0, "mlo#1")]
    mlo = MloPlacement(H_MLO, "test_mlo", trs((100.0, 50.0, 10.0), (0, 0, 0, 1)), "ymap", children=children,
                       collision=mlo_col)
    insts = [Instance(H_GATE, "test_gate", trs((120.0, 40.0, 10.0), (0, 0, 0, 1)), 0, "ymap"),
             Instance(H_HOUSE, "test_gatehouse", trs((140.0, 40.0, 10.0), (0, 0, 0, 1)), 0, "ymap")]
    cols = {H_DOOR: Col(triangles=door_tris, tri_materials=np.full(len(door_tris), 0, np.int32)),
            H_GATE: Col(triangles=gate_tris, tri_materials=np.zeros(len(gate_tris), np.int32)),
            H_HOUSE: Col(triangles=house_tris, tri_materials=np.zeros(len(house_tris), np.int32))}
    scene = SimpleNamespace(mlos=[mlo], instances=insts, world_collisions=[], missing={},
                            entity_collision=lambda h: cols.get(h), textures_for=lambda h: {},
                            all_ytd_textures=lambda: {})
    geo = {H_DOOR: _geo(door_tris), H_PANE: _geo(pane, "m_glass"), H_GATE: _geo(gate_tris, "m_metal"),
           H_HOUSE: _geo(house_tris, "m_stone")}
    mats = {"m_wood": Material(key="m_wood", kind="textured"), "m_glass": Material(key="m_glass", kind="glass", alpha="blend"),
            "m_metal": Material(key="m_metal", kind="textured"), "m_stone": Material(key="m_stone", kind="textured")}
    return scene, geo, mats


def _build_door_scene(**opts):
    from fivem2open77.mlo.build import MapBuilder
    scene, geo, mats = _door_scene()
    b = MapBuilder(scene, "door_test", log=lambda *a: None, anchor=(100.0, 50.0, 10.0), **opts)
    b._geo.update(geo)
    b.model.materials.update(mats)
    return b.build()


def test_doors_and_gates_become_devices_and_windows_get_glass():
    from fivem2open77.mlo import coverage as CV
    from fivem2open77.mlo import doors as DR
    m = _build_door_scene()
    doors = {d["part"]: d for d in m.report["doors"]}
    assert set(doors) == {"door_test_door", "door_test_gate"}, doors
    assert doors["door_test_door"]["opening"] == DR.HINGED and doors["door_test_gate"]["opening"] == DR.SLIDING
    leaf = m.parts["door_test_door"]
    assert leaf.kind == "door" and len(leaf.colliders) == 1 and leaf.colliders[0].kind == "box"
    assert leaf.submeshes and leaf.door["width"] == 1.0
    pl = [p for p in m.placements if p.part == "door_test_door"]
    assert len(pl) == 1 and np.allclose(pl[0].position, (5.0, 0.0, 0.0))
    gate = [p for p in m.placements if p.part == "door_test_gate"]
    assert len(gate) == 1 and np.allclose(gate[0].position, (23.5, -10.0, 0.0))     # stands mid-leaf
    assert "obj_test_gatehouse" in m.parts or any(p.startswith("chunk_") for p in m.parts)     # the big one stays static
    mlo = m.parts["mlo_test_mlo"]
    # the door's geometry left the MLO mesh (the device draws it); the MLO keeps the window pane
    assert sum(s.triangle_count if hasattr(s, "triangle_count") else len(s.indices) // 3 for s in mlo.submeshes) == 2
    caster = CV.BoxCaster(*CV.obb_arrays(mlo.colliders))
    # the doorway itself stays open in the static collision (the device closes it) ...
    probe = np.array([[5.5, 0.0, 1.0]])
    assert not (caster.cast(probe, np.array([[0.0, 1.0, 0.0]]), 0.01) < 1).any()
    # ... and the window hole is now glass: walking at it from outside hits a box at the pane
    d = caster.cast(np.array([[2.0, -1.0, 1.5]]), np.array([[0.0, 1.0, 0.0]]), 2.0)
    assert d[0] < 1.05, d
    assert m.report["glass"] == {"mlo_test_mlo": 1} or sum(m.report["glass"].values()) >= 1
    # --static-doors: the old behaviour (door without collision in the MLO, gate solid in a chunk)
    s = _build_door_scene(static_doors=True)
    assert not s.report["doors"] and not any(p.kind == "door" for p in s.parts.values())


def _portal_door_scene(leaf_name="panel_a", with_leaf=True):
    """_door_scene with MLO portals (the doorway 5..6 m along the front wall, the window 1..3 m
    at 1..2 m up) and a door leaf whose name says nothing about doors."""
    scene, geo, mats = _door_scene()
    mlo = scene.mlos[0]
    for ch in mlo.children:
        if ch.name == "test_door":
            ch.name = leaf_name
    if not with_leaf:
        mlo.children = [c for c in mlo.children if c.name != leaf_name]
    portals = [{"roomFrom": 1, "roomTo": 0, "corners": [[5, 0, 0], [6, 0, 0], [6, 0, 2.1], [5, 0, 2.1]]},
               {"roomFrom": 1, "roomTo": 0, "corners": [[1, 0, 1], [3, 0, 1], [3, 0, 2], [1, 0, 2]]},
               {"roomFrom": 1, "roomTo": 0, "corners": [[2, 2, 3], [4, 2, 3], [4, 4, 3], [2, 4, 3]]}]   # a skylight
    scene.archetypes = {mlo.archetype: SimpleNamespace(mlo={"rooms": [{"name": "limbo"}, {"name": "shop"}],
                                                            "portals": portals})}
    return scene, geo, mats


def _build_portal_scene(door_map=None, **kw):
    from fivem2open77.mlo.build import MapBuilder
    scene, geo, mats = _portal_door_scene(**kw)
    b = MapBuilder(scene, "door_test", log=lambda *a: None, anchor=(100.0, 50.0, 10.0), door_map=door_map)
    b._geo.update(geo)
    b.model.materials.update(mats)
    return b.build()


def test_a_leaf_standing_in_a_doorway_is_a_door_whatever_its_name():
    """Cookies: its front door is two 'cookiedesp' leaves -- no 'door' in the name -- standing in
    the MLO's doorway.  They become Door devices; the doorway report says door; the window and
    the skylight are not doorways to walk through."""
    from fivem2open77.mlo import doors as DR
    m = _build_portal_scene(leaf_name="cookiedesp")
    doors = {d["part"]: d for d in m.report["doors"]}
    assert doors["door_cookiedesp"]["found"] == "doorway" and doors["door_cookiedesp"]["opening"] == DR.HINGED, doors
    pl = [p for p in m.placements if p.part == "door_cookiedesp"]
    assert len(pl) == 1 and np.allclose(pl[0].position, (5.0, 0.0, 0.0))
    ways = {d["portal"]: d for d in m.report["doorways"]}
    assert set(ways) == {0, 1}, ways                                    # the skylight is no doorway
    assert ways[0]["status"] == "door" and ways[0]["kind"] == "door" and ways[0]["doors"] == ["door_cookiedesp"]
    assert ways[0]["key"] == "test_mlo#0" and ways[0]["outside"] and abs(ways[0]["width"] - 1.0) < 1e-6
    assert ways[1]["kind"] == "window" and ways[1]["status"] == "window"
    # the static collision keeps the doorway open (the device closes it)
    mlo = m.parts["mlo_test_mlo"]
    assert not any(c.source == "cookiedesp" for c in mlo.colliders)
    assert "cookiedesp" in m.report["door_map"]["objects"] or m.report["door_map"] == {"objects": {}, "doorways": {}}


def test_door_mapping_overrides_the_guesses():
    from fivem2open77.mlo import doors as DR
    # solid: the leaf stays static, and its doorway is reported blocked by it
    m = _build_portal_scene(door_map={"objects": {"cookiedesp": "solid"}}, leaf_name="cookiedesp")
    leafs = lambda m: [d for d in m.report["doors"] if "cookiedesp" in d["part"]]  # noqa: E731 (the gate outside stays a door)
    assert not leafs(m)
    w0 = [d for d in m.report["doorways"] if d["portal"] == 0][0]
    assert w0["status"] == "blocked" and w0["blockers"][0]["source"] == "cookiedesp", w0
    assert [c["name"] for c in m.report["door_candidates"]] == ["cookiedesp"]
    # rolling: a garage door going up
    m = _build_portal_scene(door_map={"objects": {"cookiedesp": "rolling"}}, leaf_name="cookiedesp")
    d = leafs(m)[0]
    assert d["opening"] == DR.ROLLING and d["found"] == "mapped"
    # open: no collision, no device, the doorway is open
    m = _build_portal_scene(door_map={"objects": {"cookiedesp": "open"}}, leaf_name="cookiedesp")
    assert not leafs(m) and [d["status"] for d in m.report["doorways"] if d["portal"] == 0] == ["open"]
    # remove: gone entirely
    m = _build_portal_scene(door_map={"objects": {"cookiedesp": "remove"}}, leaf_name="cookiedesp")
    assert m.report["skipped"].get("cookiedesp") == 1
    # a doorway opened by the mapping: a solid leaf in it is cut out of the static collision
    m = _build_portal_scene(door_map={"objects": {"cookiedesp": "solid"}, "doorways": {"test_mlo#0": "open"}},
                            leaf_name="cookiedesp")
    w0 = [d for d in m.report["doorways"] if d["portal"] == 0][0]
    assert w0["status"] == "open" and w0["mode"] == "open", w0
    # bad entries are refused with a reason
    from fivem2open77.mlo import doorways as W
    for bad in ({"objects": {"x y": "hinged"}}, {"objects": {"a": "swing"}}, {"doorways": {"nohash": "open"}},
                {"doorways": {"m#1": "door-spin"}}, {"other": {}}, "not json"):
        try:
            W.normalize_door_map(bad)
        except W.DoorMapError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    assert W.normalize_door_map('{"objects": {"A_Door": "auto", "B": "open"}}') == {"objects": {"b": "open"}, "doorways": {}}


def test_a_doorway_without_its_door_gets_a_made_one():
    """Most MLO doors are GTA's own models, which a conversion cannot carry: the doorway is an
    empty hole.  door-hinged / door-double / door-rolling put a plain leaf there."""
    from fivem2open77.mlo import doors as DR
    m = _build_portal_scene(with_leaf=False)
    assert [d["status"] for d in m.report["doorways"] if d["portal"] == 0] == ["open"]
    for mode, opening, n in (("door-hinged", DR.HINGED, 1), ("door-double", DR.HINGED, 2), ("door-rolling", DR.ROLLING, 1),
                             ("door-sliding", DR.SLIDING, 1)):
        m = _build_portal_scene(door_map={"doorways": {"test_mlo#0": mode}}, with_leaf=False)
        doors = [d for d in m.report["doors"] if d["found"] == "generated"]
        assert len(doors) == 1 and doors[0]["found"] == "generated" and doors[0]["opening"] == opening, (mode, doors)
        assert doors[0]["devices"] == n
        part = m.parts[doors[0]["part"]]
        assert part.kind == "door" and len(part.colliders) == 1 and part.submeshes[0].vertex_count == 24
        assert part.door["height"] > 2.0 and part.door["width"] <= (1.0 if n == 1 else 0.5) + 1e-6
        w0 = [d for d in m.report["doorways"] if d["portal"] == 0][0]
        assert w0["status"] == "door" and len(w0["doors"]) == n
        pos = sorted(tuple(round(v, 2) for v in p.position) for p in m.placements if p.part == doors[0]["part"])
        if mode == "door-double":
            assert pos == [(5.01, 0.0, 0.0), (5.99, 0.0, 0.0)], pos           # hinged at both sides
        elif opening == DR.HINGED:
            assert pos == [(5.01, 0.0, 0.0)], pos
        else:
            assert pos == [(5.5, 0.0, 0.0)], pos                              # mid-leaf
        assert m.materials[part.submeshes[0].material].kind == "textured"


def test_doorway_classification_and_cut():
    from fivem2open77.mlo import doorways as W
    from fivem2open77.mlo.collision import Collider
    floor = np.array([[[-5, -5, 0], [5, -5, 0], [5, 5, 0]], [[-5, -5, 0], [5, 5, 0], [-5, 5, 0]]], float)
    door = W.Doorway("m", 0, ["a", "limbo"], True, np.array([[0, 0, 0], [1, 0, 0], [1, 0, 2.2], [0, 0, 2.2]], float))
    win = W.Doorway("m", 1, ["a", "limbo"], True, np.array([[2, 0, 1], [3, 0, 1], [3, 0, 2], [2, 0, 2]], float))
    W.classify([door, win], floor)
    assert door.kind == "door" and win.kind == "window" and abs(door.floor) < 1e-9
    # a wall box straight across the doorway (box fitting closed a hole): cut, the wall beside stays
    wall = Collider("box", "concrete.physmat", (0.5, 0.0, 1.5), (0, 0, 0, 1), (3.0, 0.1, 1.5), source="collision")
    st = W.doorway_status(door, [wall])
    assert st["status"] == "closed" and st["blockers"][0]["kind"] == "collision"
    cut, removed, stuck = W.carve(door, [wall])
    assert removed == 1 and not stuck and len(cut) >= 2
    assert W.doorway_status(door, cut)["status"] == "open"
    # the pieces left still close the wall on both sides of the doorway
    left = [c for c in cut if c.center[0] < 0]
    right = [c for c in cut if c.center[0] > 1]
    assert left and right
    # a glass slab in a doorway is something the conversion added: blocked
    glass = Collider("box", "glass.physmat", (0.5, 0.0, 1.1), (0, 0, 0, 1), (0.5, 0.03, 1.1), source="glass")
    assert W.doorway_status(door, [glass])["status"] == "blocked"


def test_glass_slabs_only_where_the_pane_is_open():
    pane = np.array([[[0, 0, 1], [2, 0, 1], [2, 0, 2]], [[0, 0, 1], [2, 0, 2], [0, 0, 2]]], float)
    pane = np.array([t if np.cross(t[1] - t[0], t[2] - t[0])[1] < 0 else t[::-1] for t in pane])
    both = np.concatenate([pane, pane[:, ::-1] + [0, 0.01, 0]])             # drawn as two faces
    slabs = C.glass_colliders([], both)
    assert len(slabs) == 1 and slabs[0].physmat == C.GLASS_PHYSMAT, slabs
    for x, z in ((0.2, 1.1), (1.0, 1.5), (1.8, 1.9)):
        assert _inside_box((x, -0.02, z), slabs[0], eps=0.03)
    # a wall right behind the glass already stops the player: nothing added
    wall = C.Collider("box", "concrete.physmat", (1.0, 0.2, 1.5), (0, 0, 0, 1), (2.0, 0.1, 1.5))
    assert C.glass_colliders([wall], both) == []
    # small glass (a lamp shade, a bottle) and flat glass (a table top) never become walls
    small = pane * [0.2, 1, 1] + [0, 0, 0]
    small[:, :, 2] = 1 + (small[:, :, 2] - 1) * 0.3
    assert C.glass_colliders([], small) == []
    top = np.array([[[0, 0, 1], [2, 0, 1], [2, 2, 1]], [[0, 0, 1], [2, 2, 1], [0, 2, 1]]], float)
    assert C.glass_colliders([], top) == []


def _inside_box(pt, c, eps=1e-6):
    from fivem2open77.mlo.collision import _quat_from_axes  # noqa: F401
    x, y, z, w = c.rotation
    R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    local = R.T @ (np.asarray(pt, float) - np.asarray(c.center, float))
    return bool(np.all(np.abs(local) <= np.asarray(c.half) + eps))


def test_hulls_become_boxes_that_cover_them():
    # a sloped triangle (a roof piece): strips that cover it without much spill
    tri = np.array([[0.0, 0.0, 0.0], [6.0, 0.0, 3.0], [0.0, 4.0, 0.0]])
    n = np.cross(tri[1] - tri[0], tri[2] - tri[0])
    boxes = C.polygon_boxes(tri, n, 0.2, "concrete.physmat")
    assert 2 <= len(boxes) <= 4 and all(b.kind == "box" for b in boxes)
    area = 0.5 * np.linalg.norm(n)
    covered = sum(4 * b.half[0] * b.half[1] for b in boxes)
    assert covered <= area * 1.6, (covered, area)
    rng = np.random.default_rng(1)
    nn = n / np.linalg.norm(n)
    for _ in range(300):
        u, v = rng.random(2)
        if u + v > 1:
            u, v = 1 - u, 1 - v
        pt = tri[0] + u * (tri[1] - tri[0]) + v * (tri[2] - tri[0]) - nn * 0.1
        assert any(_inside_box(pt, b, 1e-4) for b in boxes), pt
    # a solid hull (a sofa): one box around every point
    pts = rng.random((40, 3)) * [2.0, 0.9, 0.8] + [10, 5, 0]
    b = C.solid_box(pts, "fabrics.physmat")
    assert b.kind == "box" and all(_inside_box(p, b, 1e-6) for p in pts)
    # anything still convex goes through boxify (flat -> strips, solid -> one box)
    slab = C.Collider("convex", "wood.physmat", vertices=np.concatenate([tri, tri - nn * 0.1]))
    solid = C.Collider("convex", "wood.physmat", vertices=pts)
    out = C.boxify([slab, solid, C.Collider("sphere", "wood.physmat", radius=1.0)])
    assert [c.kind for c in out].count("convex") == 0 and out[-1].kind == "sphere"


def test_mesh_physics_never_contains_convex_shapes():
    from fivem2open77.cp77.wkjson import Handles
    from fivem2open77.mlo.physics import physics_param
    tris = _box_tris((0, 0, 0), (3, 2, 1))
    cols = C.triangles_to_colliders(tris, np.zeros(len(tris), np.int32))
    cols.append(C.Collider("convex", "wood.physmat", vertices=np.random.default_rng(2).random((20, 3))))
    param = physics_param(cols, Handles(), "t")
    shapes = param["Data"]["physicsData"]["Data"]["bodies"][0]["Data"]["collisionShapes"]
    kinds = {s["Data"]["$type"] for s in shapes}
    assert kinds <= {"physicsColliderBox", "physicsColliderSphere", "physicsColliderCapsule"}, kinds
    assert len(shapes) >= 2


def test_physmat_mapping():
    assert C.physmat_for(1) == "concrete.physmat"       # CONCRETE
    assert C.physmat_for(70) == "wood.physmat"          # WOOD_SOLID_MEDIUM
    assert C.physmat_for(9999) == C.DEFAULT_PHYSMAT


def test_entity_rotation_convention():
    """ymap entities store the inverse rotation; 90 degrees about Z stored as its inverse."""
    s = math.sqrt(0.5)
    stored = (0.0, 0.0, -s, s)                          # inverse of +90 degrees
    m = trs((1, 2, 3), q_inverse(stored))
    p = m @ np.array([1.0, 0.0, 0.0, 1.0])
    assert np.allclose(p[:3], (1, 3, 3))                # +X turned to +Y, then translated


def test_map_and_manager_names_follow_open77_name_rules():
    for raw in ("Cedar_Crest_EstateV2", "1 house", "x" * 90, "ab", "--"):
        s = _sanitize(raw)
        assert len(s) <= 48 and re.fullmatch(r"[a-z][a-z0-9_]*", s), (raw, s)
    assert re.fullmatch(r"[a-z][a-z0-9_]{2,63}", R.MANAGER)


# ------------------------------------------------------------------------------ baked world documents
def test_host_entity_is_a_static_physical_mesh():
    doc = W.host_entity_doc("cedar_crest", "mlo_house", r"fivem2open77\maps\cedar_crest\meshes\mlo_house.mesh", 300.0)
    root = doc["Data"]["RootChunk"]
    assert root["$type"] == "entEntityTemplate" and not doc["Data"]["EmbeddedFiles"]
    pkg = root["compiledData"]["Data"]
    assert [c["$type"] for c in pkg["Chunks"]] == ["entEntity", "entPhysicalMeshComponent"]
    comp = pkg["Chunks"][1]
    assert comp["mesh"]["DepotPath"]["$value"].endswith("mlo_house.mesh")
    assert comp["filterData"]["HandleId"] == "0" and comp["filterData"]["Data"]["preset"]["$value"] == "World Static"
    assert root["components"][0]["filterData"] == {"HandleRefId": "0"} and root["entity"]["HandleId"] == "1"
    assert pkg["CruidDict"]["1"] == comp["id"] == root["components"][0]["id"] and int(comp["id"]) > 0
    assert comp["simulationType"] == "Static" and comp["autoHideDistance"] == 300.0
    other = W.host_entity_doc("cedar_crest", "obj_gate", "x.mesh")["Data"]["RootChunk"]["compiledData"]["Data"]
    assert other["CruidDict"]["1"] != pkg["CruidDict"]["1"]


def test_parts_without_collision_get_a_plain_mesh_component():
    root = W.host_entity_doc("cedar_crest", "obj_lamp", "x.mesh", 150.0, physical=False)["Data"]["RootChunk"]
    pkg = root["compiledData"]["Data"]
    assert [c["$type"] for c in pkg["Chunks"]] == ["entEntity", "entMeshComponent"]
    for comp in (pkg["Chunks"][1], root["components"][0]):
        assert comp["$type"] == "entMeshComponent" and not set(W.PHYSICAL_KEYS) & set(comp)
        assert comp["mesh"]["DepotPath"]["$value"] == "x.mesh" and comp["autoHideDistance"] == 150.0
    assert root["entity"]["HandleId"] == "0"            # no physics filter handle before it
    assert "HandleId" not in json.dumps(pkg)


def test_writer_gives_physics_only_to_parts_with_colliders():
    src = open(os.path.join(os.path.dirname(W.__file__), "writer.py"), encoding="utf-8").read()
    assert "physical=bool(part.colliders)" in src


def test_sector_places_every_part_around_the_anchor():
    L = _layout()
    anchor = {"x": 100.0, "y": 200.0, "z": 50.0, "yaw": 90.0}
    doc, (lo, hi) = W.sector_doc(L, anchor)
    root = doc["Data"]["RootChunk"]
    nodes, data = root["nodes"], root["nodeData"]["Data"]
    assert len(nodes) == len(data) == 3 and root["variantIndices"] == [0] and root["version"] == 62
    assert [n["HandleId"] for n in nodes] == ["0", "1", "2"]
    for i, (n, d) in enumerate(zip(nodes, data)):
        assert n["Data"]["$type"] == "worldEntityNode" and d["NodeIndex"] == i
        assert n["Data"]["entityTemplate"]["DepotPath"]["$value"] == L["parts"][L["placements"][i]["part"]]["ent"]
    # placement 0 at local (3, 4, 0.5), yaw 0 -> rotated +90 about the anchor
    p = data[0]["Position"]
    assert abs(p["X"] - (100 - 4)) < 1e-9 and abs(p["Y"] - (200 + 3)) < 1e-9 and abs(p["Z"] - 50.5) < 1e-9
    q = data[0]["Orientation"]
    assert abs(q["k"] - math.sin(math.radians(45))) < 1e-9 and abs(q["r"] - math.cos(math.radians(45))) < 1e-9
    q = data[1]["Orientation"]                         # 90 + 180 = 270 = -90
    assert abs(abs(q["k"]) - math.sin(math.radians(45))) < 1e-9
    refs = {d["QuestPrefabRefHash"]["$value"] for d in data}
    assert len(refs) == 3 and "0" not in refs          # entity ids must not collide
    for d in data:
        assert d["UkFloat1"] >= d["MaxStreamingDistance"] >= 150
        pos = (d["Position"]["X"], d["Position"]["Y"], d["Position"]["Z"])
        assert all(lo[k] < pos[k] < hi[k] for k in range(3))
    block = W.block_doc(B.sector_paths("cedar_crest")[0], (lo, hi))["Data"]["RootChunk"]
    desc = block["descriptors"][0]
    assert desc["data"]["DepotPath"]["$value"].endswith("cedar_crest.streamingsector")
    assert desc["streamingBox"]["Min"]["X"] == lo[0] and desc["numNodeRanges"] == 1
    assert desc["category"] == root["category"] and desc["level"] == root["level"]


def test_archive_xl_streams_the_block():
    text = W.archive_xl(r"fivem2open77\maps\cedar_crest\sector\cedar_crest.streamingblock")
    assert "streaming:\n  blocks:\n    - fivem2open77\\maps\\cedar_crest\\sector\\cedar_crest.streamingblock\n" in text
    try:
        import yaml
    except ImportError:
        return
    assert yaml.safe_load(text) == {"streaming": {"blocks": [r"fivem2open77\maps\cedar_crest\sector\cedar_crest.streamingblock"]}}


def test_wolvenkit_reads_the_documents_back():
    cli = os.environ.get("WOLVENKIT_CLI")
    if not cli:
        print("   (skipped: set WOLVENKIT_CLI)")
        return
    from fivem2open77.cp77.wolvenkit import WolvenKit
    from fivem2open77.cp77.wkjson import save
    wk = WolvenKit(cli, log=lambda *a: None)
    d = tempfile.mkdtemp()
    L = _layout()
    sdoc, box = W.sector_doc(L, {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 30.0})
    from fivem2open77.mlo import doors as DR
    DL = _door_layout()
    ddoc, _ = W.sector_doc(DL, {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 30.0})
    devs = W.door_devices(DL, {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 30.0})
    docs = {"s.streamingsector": sdoc, "b.streamingblock": W.block_doc("a\\s.streamingsector", box),
            "e.ent": W.host_entity_doc("cedar_crest", "mlo_house", "a\\m.mesh", 200.0),
            "p.ent": W.host_entity_doc("cedar_crest", "obj_lamp", "a\\l.mesh", 150.0, physical=False),
            "d.streamingsector": ddoc, "m.devices": DR.devices_doc([(x["ref"], x["position"]) for x in devs])}
    # the game's door template, when a conversion has cached it (FIVEM2OPEN77_CACHE)
    tpl = os.path.join(os.environ.get("FIVEM2OPEN77_CACHE", ""), "donor", "json",
                       DR.DOOR_TEMPLATE.replace("\\", os.sep) + ".json")
    if os.path.isfile(tpl):
        t = json.load(open(tpl, encoding="utf-8"))
        for n, part in (("door.ent", "door_zbadoor"), ("gate.ent", "door_zextgate")):
            docs[n] = DR.door_entity_doc(t, "a\\" + part + ".mesh", DL["parts"][part]["door"])
    else:
        print("   (door template not cached: door entities not checked)")
    for n, doc in docs.items():
        save(doc, os.path.join(d, "json", n + ".json"))
    wk.deserialize(os.path.join(d, "json"), os.path.join(d, "bin"))
    wk.serialize(os.path.join(d, "bin"), os.path.join(d, "rt"))
    for n, doc in docs.items():
        back = json.load(open(os.path.join(d, "rt", n + ".json"), encoding="utf-8"))
        diffs = _diff(json.loads(json.dumps(doc["Data"]["RootChunk"])), back["Data"]["RootChunk"])
        assert not diffs, (n, diffs[:5])


def _diff(a, b, path="", out=None):
    """Structural differences; floats compare at float32 precision (what the game stores)."""
    out = [] if out is None else out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a) | set(b):
            if k not in a or k not in b:
                out.append((path + "/" + k, k in a, k in b))
            else:
                _diff(a[k], b[k], path + "/" + k, out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((path, len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}[{i}]", out)
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        if abs(W.f32(a) - W.f32(b)) > 1e-6 * max(1.0, abs(a)):
            out.append((path, a, b))
    elif a != b:
        out.append((path, a, b))
    return out


# ------------------------------------------------------------------------------ wireframe
def test_ghost_wireframe_budget_and_encoding():
    m = _model()
    lines = G.ghost_lines(m, budget=200)
    assert 12 < len(lines) <= 200
    kinds = {ln[6] for ln in lines}
    assert {G.KIND_BOUNDS, G.KIND_PART, G.KIND_WALL} <= kinds
    nums = [float(v) for v in G.encode(lines).split()]
    assert len(nums) == 7 * len(lines)
    # the second house is turned 90 degrees: its wall rectangles span y, not x, around (-30, 4)
    mn, mx = m.bounds()
    for ln in lines:
        for k in range(3):
            assert mn[k] - 1e-3 <= ln[k] <= mx[k] + 1e-3 and mn[k] - 1e-3 <= ln[3 + k] <= mx[k] + 1e-3
    assert len(G.ghost_lines(m, budget=2400)) <= 2400


def _mesh(tris):
    from types import SimpleNamespace
    pos = np.asarray(tris, np.float32).reshape(-1, 3)
    return SimpleNamespace(positions=pos, indices=np.arange(len(pos), dtype=np.int32), lod=1, material="m",
                           vertex_count=len(pos), normals=np.zeros_like(pos))


def _meshed_model():
    """A 60 x 40 m platform at z 0 with a 6 x 20 m drive going south, a pillar under it down to
    -30 m, a 20 x 10 x 6 m house on it, a lamp (a prop) and a 7 m sliding gate across the drive."""
    ground = MapPart("obj_ground", "object")
    ground.submeshes = [_mesh(_quad_tris(-30, -20, 30, 20, 0.0, 6) + _quad_tris(-3, -40, 3, -20, 0.0, 2)
                              + list(_box_tris((-2, -2, -30), (2, 2, 0))))]
    house = MapPart("obj_house", "object")
    house.submeshes = [_mesh(list(_box_tris((-10, -5, 0), (10, 5, 6))))]
    lamp = MapPart("obj_lamp", "object")
    lamp.submeshes = [_mesh(list(_box_tris((-0.2, -0.2, 0), (0.2, 0.2, 4))))]
    gate = MapPart("door_main_gate", "door", door={
        "opening": "SLIDING_HORIZONTALLY", "axis": "x", "width": 7.0, "height": 2.5, "thickness": 0.1,
        "pivot": [0.0, 0.0], "center": [0.0, 0.0], "zmin": 0.0, "zmax": 2.5, "slide": -7.0, "solid": False})
    gate.submeshes = [_mesh(list(_box_tris((-3.5, -0.05, 0), (3.5, 0.05, 2.5))))]
    m = MapModel(name="estate", display_name="Estate", anchor=(0, 0, 0), spawn=(0, 0, 1))
    m.parts = {"obj_ground": ground, "obj_house": house, "obj_lamp": lamp, "door_main_gate": gate}
    m.placements = [Placement("obj_ground", (0.0, 0.0, 0.0), 0.0, "g"), Placement("obj_house", (5.0, 5.0, 0.0), 0.0, "h"),
                    Placement("obj_lamp", (20.0, -15.0, 0.0), 0.0, "l"),
                    Placement("door_main_gate", (0.0, -38.0, 0.0), 0.0, "gate")]
    return m


def test_ghost_footprint_gates_and_buildings_first():
    m = _meshed_model()
    lines = G.ghost_lines(m)
    kinds = [ln[6] for ln in lines]
    runs = [k for i, k in enumerate(kinds) if i == 0 or kinds[i - 1] != k]
    assert runs[:4] == [G.KIND_FOOT, G.KIND_GATE, G.KIND_BOUNDS, G.KIND_PART], runs
    foot = [ln for ln in lines if ln[6] == G.KIND_FOOT]
    xs = [v for ln in foot for v in (ln[0], ln[3])]
    ys = [v for ln in foot for v in (ln[1], ln[4])]
    assert -31.6 < min(xs) < -28.4 and 28.4 < max(xs) < 31.6, (min(xs), max(xs))
    assert -41.6 < min(ys) < -38.4 and 18.4 < max(ys) < 21.6, (min(ys), max(ys))     # the drive is in it
    assert all(abs(ln[2]) < 0.6 and abs(ln[5]) < 0.6 for ln in foot), "at ground level, not down the pillar"
    assert 6 <= len(foot) <= 40, len(foot)                                            # simplified, not every cell
    starts = sorted((round(ln[0], 2), round(ln[1], 2)) for ln in foot)
    ends = sorted((round(ln[3], 2), round(ln[4], 2)) for ln in foot)
    assert starts == ends, "a closed outline"
    # the drive: the outline narrows to about 6 m south of the platform
    south = sorted({round(x, 1) for ln in foot for x, y in ((ln[0], ln[1]), (ln[3], ln[4])) if y < -30})
    assert len(south) == 2 and 4.5 < south[1] - south[0] < 8.5, south
    gate = [ln for ln in lines if ln[6] == G.KIND_GATE]
    assert len(gate) == 4
    gx = [v for ln in gate for v in (ln[0], ln[3])]
    gz = [v for ln in gate for v in (ln[2], ln[5])]
    assert abs(min(gx) + 3.5) < 1e-6 and abs(max(gx) - 3.5) < 1e-6 and max(gz) == 2.5
    assert all(abs(ln[1] + 38.0) < 1e-6 for ln in gate)
    # the house is a building box; the ground (the footprint's size) and the lamp are not
    parts = [ln for ln in lines if ln[6] == G.KIND_PART]
    assert len(parts) == 12, len(parts)
    px = [v for ln in parts for v in (ln[0], ln[3])]
    assert abs(min(px) + 5.0) < 1e-6 and abs(max(px) - 15.0) < 1e-6
    assert len(lines) <= G.DEFAULT_BUDGET


def test_ghost_without_meshes_still_has_boxes():
    lines = G.ghost_lines(_model())
    assert lines and {ln[6] for ln in lines} >= {G.KIND_BOUNDS}


# ------------------------------------------------------------------------------ resources and Lua
def test_map_resource_is_server_only_and_the_manager_has_the_client():
    m = R.map_manifest("cedar_crest", "1.0.0", "Cedar", ["cedar_crest.zip", "cedar_crest_sector.zip"])
    lines = [ln.strip() for ln in m.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    assert 'dependency "archivexl >=1.27.1"' in lines
    assert 'preload_mod "dist/cedar_crest.zip"' in lines and 'preload_mod "dist/cedar_crest_sector.zip"' in lines
    assert not any(ln.startswith("client_script") for ln in lines)
    p = R.manager_manifest()
    lines = [ln.strip() for ln in p.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    assert 'resource "f2o77_maps"' in lines and 'client_script "client/main.lua"' in lines
    assert 'shared_scripts { "shared/config.lua", "shared/maps/*.lua" }' in lines and 'web_files { "web/**" }' in lines
    assert not any(ln.startswith(("dependency", "preload_mod")) for ln in lines)
    for perm in ("network.events", "acl.read", "input.actions", "http.serve", "world.debug", "world.query",
                 "filesystem.read", "filesystem.write", "runtime.restart", "players.teleport", "ui.vanilla.hud"):
        assert f'"{perm}",' in lines
    assert not any("gizmos" in ln for ln in lines), "the gizmo service is not released on op77.78"


def _write_manager(root, baked=None, bake_id=None):
    """A manager with two maps: cedar_crest (baked) and harbor_dock (not baked, a default spot)."""
    d = os.path.join(root, R.MANAGER)
    R.write_manager_resource(d)
    L = _layout()
    L["display"] = "Cedar Crest Estate"
    R.write_map_def(d, L, baked, G.encode(G.ghost_lines(_meshed_model())), bake_id)
    L2 = _layout("harbor_dock")
    L2["display"] = "Harbor Dock"
    L2["default"] = {"x": -100.0, "y": 50.0, "z": 5.0, "yaw": 0.0}
    R.write_map_def(d, L2, None, "")
    return d


def test_manager_resource_files():
    d = _write_manager(tempfile.mkdtemp(), baked={"x": 480.0, "y": 1800.0, "z": 174.0, "yaw": 0.0}, bake_id="b1")
    for rel in R.MANAGER_CODE:
        assert os.path.isfile(os.path.join(d, *rel.split("/"))), rel
    text = open(os.path.join(d, "shared", "maps", "cedar_crest.lua"), encoding="utf-8").read()
    assert 'F2O77_MAPS["cedar_crest"] = {' in text and 'bakeId = "b1"' in text and "doors = 0" in text
    assert 'display = "Cedar Crest Estate"' in text and 'F2O77_MAPS["cedar_crest"].ghost = [' in text
    harbor = open(os.path.join(d, "shared", "maps", "harbor_dock.lua"), encoding="utf-8").read()
    assert "baked =" not in harbor and "bakeId" not in harbor and "default = {" in harbor
    assert not os.path.exists(os.path.join(d, "data")), "no token unless asked"
    R.write_manager_resource(d, agent_token="t" * 32)
    assert open(os.path.join(d, "data", "agent_token.txt")).read().strip() == "t" * 32
    assert os.path.isfile(os.path.join(d, "shared", "maps", "cedar_crest.lua")), "rewriting the code keeps the maps"
    luac = shutil.which("luac5.4") or shutil.which("luac")
    if luac:
        for dp, _, fns in os.walk(d):
            for fn in fns:
                if fn.endswith(".lua") and fn != "open77.lua":      # the manifest is Open77's own DSL
                    r = subprocess.run([luac, "-p", os.path.join(dp, fn)], capture_output=True, text=True)
                    assert r.returncode == 0, (fn, r.stderr)
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", os.path.join(d, "web", "app.js")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    html = open(os.path.join(d, "web", "index.html"), encoding="utf-8").read()
    ids = set(re.findall(r'id="([^"]+)"', html))
    js = open(os.path.join(d, "web", "app.js"), encoding="utf-8").read()
    for ref in set(re.findall(r"\$\('([^']+)'\)", js)):
        assert ref in ids, f"app.js uses #{ref}, which index.html does not have"
    for op in set(re.findall(r"\[data-op=([a-z_]+)\]", js)):
        assert f'data-op="{op}"' in html, op


def test_manager_lua_server_and_client():
    lua = shutil.which("lua5.4") or shutil.which("lua")
    if not lua:
        print("   (skipped: no lua5.4)")
        return
    d = _write_manager(tempfile.mkdtemp(), baked={"x": 480.0, "y": 1800.0, "z": 174.0, "yaw": 0.0},
                       bake_id="cedar_crest-convert-1")
    r = subprocess.run([lua, os.path.join(HERE, "lua", "maps_manager_server_harness.lua"), d],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK maps manager server" in r.stdout, r.stdout + r.stderr[-6000:]
    # the client also gets a big map for its budget check: 2400 lines, as many as the old wireframes
    rng = np.random.default_rng(3)
    big = rng.uniform(-100, 100, (2400, 6)).round(2)
    kinds = np.repeat([4, 5, 0, 1, 2], [300, 100, 12, 900, 1088])
    Lb = _layout("big_map")
    Lb["display"] = "Big Map"
    R.write_map_def(d, Lb, {"x": 0.0, "y": 0.0, "z": 50.0, "yaw": 0.0},
                    "\n".join(" ".join(f"{v:g}" for v in row) + f" {k}" for row, k in zip(big, kinds)), "big-1")
    r = subprocess.run([lua, os.path.join(HERE, "lua", "maps_manager_client_harness.lua"), d],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK maps manager client" in r.stdout, r.stdout + r.stderr[-6000:]


def test_map_server_script_exports_the_bake():
    lua = shutil.which("lua5.4") or shutil.which("lua")
    text = R.map_server_lua(_layout(), ["cedar_crest.zip", "cedar_crest_sector.zip"],
                            {"x": 1.5, "y": 2.0, "z": 3.0, "yaw": 90.0}, "cedar_crest-abc-1")
    assert 'exports("baked"' in text and 'id = "cedar_crest-abc-1"' in text
    if not lua:
        return
    d = tempfile.mkdtemp()
    p = os.path.join(d, "main.lua")
    with open(p, "w", encoding="utf-8") as f:
        f.write("local E = {}\nfunction exports(n, fn) E[n] = fn end\n" + text +
                "\nlocal b = E.baked()\nassert(b.x == 1.5 and b.yaw == 90.0 and b.id == 'cedar_crest-abc-1')\nprint('OK export')\n")
    r = subprocess.run([lua, p], capture_output=True, text=True)
    assert r.returncode == 0 and "OK export" in r.stdout, r.stdout + r.stderr
    unbaked = R.map_server_lua(_layout(), ["cedar_crest.zip"], None)
    assert "local BAKED = nil" in unbaked and 'exports("baked"' in unbaked


# ------------------------------------------------------------------------------ bake, install, bundle
def _fake_archive(entries=2, stamp=123):
    table = bytearray(struct.pack("<III", entries, 0, 0))
    for i in range(entries):
        table += struct.pack("<Qq5I20s", 1000 + i, stamp + i, 0, 0, 0, 0, 0, b"\0" * 20)
    head = struct.pack("<4sIQIQIQ", b"RDAR", 12, 44, len(table) + 16, 0, 0, 0) + b"\0\0\0\0"
    return head + struct.pack("<IIQ", 8, len(table) + 8, package._crc64_xz(bytes(table))) + table


class _FakeWk:
    """Stands in for WolvenKit: 'deserializes' by hashing the JSON, 'packs' a valid empty archive."""

    def __init__(self):
        self.calls = []

    def deserialize_tree(self, json_root, out_root, scratch):
        for dp, _, fns in os.walk(json_root):
            for fn in fns:
                rel = os.path.relpath(os.path.join(dp, fn), json_root)[:-5]
                dst = os.path.join(out_root, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(os.path.join(dp, fn), "rb") as s, open(dst, "wb") as f:
                    f.write(s.read())
        self.calls.append("deserialize")

    def pack(self, src, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, os.path.basename(src) + ".archive"), "wb") as f:
            f.write(_fake_archive(stamp=len(self.calls) * 1000))
        self.calls.append("pack")


def _converted_map(root):
    """A converted-map folder as the writer leaves it (layout.json, assets zip, ghost, the manager)."""
    L = _layout()
    res = os.path.join(root, "cedar_crest")
    os.makedirs(os.path.join(res, "dist"))
    a = os.path.join(root, "assets.archive")
    with open(a, "wb") as f:
        f.write(_fake_archive())
    package.write_zip(os.path.join(res, "dist", "cedar_crest.zip"), {"archive/pc/mod/fivem2open77_map_cedar_crest.archive": a})
    R.write_map_resource(res, L, L["packages"], None)
    ghost = G.encode(G.ghost_lines(_model()))
    with open(os.path.join(res, "ghost.txt"), "w", encoding="utf-8") as f:
        f.write(ghost)
    m = R.write_manager_resource(os.path.join(root, R.MANAGER), agent_token="t" * 32)
    R.write_map_def(m, L, None, ghost)
    return res


def test_bake_writes_sector_package_and_updates_the_map_and_the_manager():
    root = tempfile.mkdtemp()
    res = _converted_map(root)
    r = B.bake(res, B.parse_anchor("480,1800,174,30"), _FakeWk(), log=lambda *a: None, bake_id="cedar_crest-t-1")
    z = os.path.join(res, "dist", "cedar_crest_sector.zip")
    with zipfile.ZipFile(z) as zf:
        names = sorted(zf.namelist())
        xl = zf.read("archive/pc/mod/fivem2open77_map_cedar_crest_sector.archive.xl").decode()
    assert names == ["archive/pc/mod/fivem2open77_map_cedar_crest_sector.archive",
                     "archive/pc/mod/fivem2open77_map_cedar_crest_sector.archive.xl"], names
    assert "cedar_crest.streamingblock" in xl
    manifest = open(os.path.join(res, "open77.lua"), encoding="utf-8").read()
    assert 'preload_mod "dist/cedar_crest.zip"' in manifest and 'preload_mod "dist/cedar_crest_sector.zip"' in manifest
    baked = json.load(open(os.path.join(res, "baked.json")))
    assert baked["anchor"] == {"x": 480.0, "y": 1800.0, "z": 174.0, "yaw": 30.0} and baked["nodes"] == 3
    assert baked["id"] == "cedar_crest-t-1"
    server = open(os.path.join(res, "server", "main.lua"), encoding="utf-8").read()
    assert 'exports("baked"' in server and '"cedar_crest-t-1"' in server
    mdef = open(os.path.join(root, R.MANAGER, "shared", "maps", "cedar_crest.lua"), encoding="utf-8").read()
    assert r["managerDef"].endswith("cedar_crest.lua") and "baked = {" in mdef and "1800.0" in mdef
    assert 'bakeId = "cedar_crest-t-1"' in mdef and 'F2O77_MAPS["cedar_crest"].ghost = [' in mdef
    assert open(os.path.join(root, R.MANAGER, "data", "agent_token.txt")).read().strip() == "t" * 32
    first = open(z, "rb").read()
    B.bake(res, B.parse_anchor("480,1800,174,30"), _FakeWk(), log=lambda *a: None)
    assert open(z, "rb").read() == first, "same anchor, same package bytes"
    B.bake(res, B.parse_anchor("481,1800,174,30"), _FakeWk(), log=lambda *a: None)
    assert json.load(open(os.path.join(res, "baked.json")))["anchor"]["x"] == 481.0
    assert r["packages"] == ["cedar_crest.zip", "cedar_crest_sector.zip"]


def test_bake_to_a_staging_folder_leaves_the_map_alone():
    root = tempfile.mkdtemp()
    res = _converted_map(root)
    B.bake(res, B.parse_anchor("1,2,3,0"), _FakeWk(), log=lambda *a: None, bake_id="first")

    def snapshot():
        out = {}
        for d in (res, os.path.join(root, R.MANAGER)):
            for dp, _, fns in os.walk(d):
                for fn in fns:
                    p = os.path.join(dp, fn)
                    out[p] = open(p, "rb").read()
        return out
    before = snapshot()
    stage = os.path.join(root, "stage")
    r = B.bake(res, B.parse_anchor("10,20,30,90"), _FakeWk(), log=lambda *a: None, out_dir=stage, bake_id="second")
    assert snapshot() == before, "a staged bake never touches the live map or the manager"
    assert r["out"] == os.path.abspath(stage)
    assert sorted(r["files"]) == ["README.md", "baked.json", "dist/cedar_crest_sector.zip", "open77.lua", "server/main.lua"]
    assert r["remove"] == ["doors.json"], "no doors: a doors.json of an older bake goes"
    for rel in r["files"]:
        assert os.path.isfile(os.path.join(stage, rel)), rel
    assert not os.path.exists(os.path.join(stage, "layout.json")), "the layout does not change with the anchor"
    assert json.load(open(os.path.join(stage, "baked.json")))["id"] == "second"
    mdef = open(r["managerDef"], encoding="utf-8").read()
    assert r["managerDef"] == os.path.join(os.path.abspath(stage), "manager", "cedar_crest.lua")
    assert 'bakeId = "second"' in mdef and "yaw = 90.0" in mdef and "ghost = [" in mdef


def test_install_game_and_bundle():
    root = tempfile.mkdtemp()
    res = _converted_map(root)
    B.bake(res, {"x": 1, "y": 2, "z": 3}, _FakeWk(), log=lambda *a: None)
    game = os.path.join(root, "game")
    os.makedirs(os.path.join(game, "archive", "pc", "content"))
    os.makedirs(os.path.join(game, "archive", "pc", "mod"))
    other = os.path.join(game, "archive", "pc", "mod", "someone_else.archive")
    open(other, "wb").write(b"x")
    stale = os.path.join(game, "archive", "pc", "mod", "fivem2open77_map_cedar_crest_old.archive")
    open(stale, "wb").write(b"old")
    r = D.install_game(res, game)
    assert sorted(r["written"]) == ["fivem2open77_map_cedar_crest.archive", "fivem2open77_map_cedar_crest_sector.archive",
                                    "fivem2open77_map_cedar_crest_sector.archive.xl"], r
    assert r["removed"] == ["fivem2open77_map_cedar_crest_old.archive"] and os.path.exists(other)
    assert D.install_game(res, game)["unchanged"] and not D.install_game(res, game)["written"]
    r = D.install_game(res, game, uninstall=True)
    assert len(r["removed"]) == 3 and os.path.exists(other)
    b = D.bundle(res, os.path.join(root, "bundle.tgz"))
    import tarfile
    with tarfile.open(b["path"]) as t:
        names = t.getnames()
        mdef = t.extractfile("resources/assets/tools/f2o77_maps/shared/maps/cedar_crest.lua").read().decode()
    assert "resources/assets/maps/cedar_crest/open77.lua" in names
    assert "resources/assets/maps/cedar_crest/dist/cedar_crest_sector.zip" in names
    for rel in R.MANAGER_CODE:
        assert f"resources/assets/tools/f2o77_maps/{rel}" in names, rel
    assert not [n for n in names if "/data/" in n], "no data/ (state, tokens) in a bundle"
    assert "baked = {" in mdef and "x = 1.0" in mdef, "the manager's map file says where the bundled package puts it"
    assert b["resources"] == ["cedar_crest", "f2o77_maps"]


def test_parse_anchor():
    assert B.parse_anchor("1, 2, 3") == {"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0}
    assert B.parse_anchor("1,2,3,370")["yaw"] == 10.0
    for bad in ("1,2", "a,b,c", "1,2,nan"):
        try:
            B.parse_anchor(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


# ------------------------------------------------------------------------------ real input
def test_real_map_if_available():
    path = os.environ.get("F2O77_TEST_MAP")
    if not path:
        print("   (skipped: set F2O77_TEST_MAP)")
        return
    from fivem2open77.mlo.pipeline import MapConverter
    conv = MapConverter(None, None, ".", log=lambda *a: None)
    scene, model, coords, name = conv.plan(path)
    assert model.parts and model.placements
    for pl in model.placements:
        assert pl.part in model.parts
        assert all(math.isfinite(v) for v in pl.position)
    for part in model.parts.values():
        for sm in part.submeshes:
            assert len(sm.positions) <= 65535
            assert sm.indices.max() < len(sm.positions)
            assert np.isfinite(sm.positions).all() and np.isfinite(sm.normals).all()
    lines = G.ghost_lines(model)
    assert 0 < len(lines) <= G.DEFAULT_BUDGET


if __name__ == "__main__":
    fails = 0
    for k, fn in sorted(globals().items()):
        if k.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", k)
            except Exception as e:  # noqa: BLE001
                fails += 1
                import traceback
                traceback.print_exc()
                print("FAIL", k, repr(e))
    sys.exit(1 if fails else 0)
