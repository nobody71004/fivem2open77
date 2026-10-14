"""Map collision maths: colliders moved by a placement (all at once, as city districts need) give
exactly what the one-by-one code gave, and a doorway's status is the same with its box arrays
built once for many doorways."""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.mlo import collision as C  # noqa: E402
from fivem2open77.mlo import doorways as D  # noqa: E402


def _reference(cols, m):
    """The pre-1.5 transform, one collider at a time."""
    r, tr = m[:3, :3], m[:3, 3]
    s = float(np.cbrt(abs(np.linalg.det(r)))) or 1.0
    rn = r / s
    out = []
    for c in cols:
        x, y, z, w = c.rotation
        q = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        out.append((r @ np.asarray(c.center, float) + tr, C._quat_from_axes((rn @ q).T), np.asarray(c.half) * s))
    return out


def test_transform_colliders_matches_the_one_by_one_code():
    rng = np.random.default_rng(7)
    cols = []
    for _ in range(500):
        q = rng.normal(size=4)
        q /= np.linalg.norm(q)
        cols.append(C.Collider("box", "concrete", center=tuple(rng.normal(size=3) * 20), rotation=tuple(q), half=(0.5, 1.0, 2.0)))
    for q in [(0, 0, 0, 1), (1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (math.sqrt(0.5), 0, 0, math.sqrt(0.5))]:
        cols.append(C.Collider("box", "metal", center=(1, 2, 3), rotation=q, half=(1, 1, 1), source="wall"))
    cols.append(C.Collider("convex", "metal", vertices=np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1.0]]), source="prop"))
    m = np.eye(4)
    a = 0.7
    m[:3, :3] = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]]) * 1.5
    m[:3, 3] = (5, -3, 2)
    new = C.transform_colliders(cols, m)
    ref = _reference(cols, m)
    assert len(new) == len(cols)
    for n, (ctr, q, half), old in zip(new, ref, cols):
        assert np.allclose(n.center, ctr, atol=1e-9)
        assert min(np.abs(np.subtract(n.rotation, q)).max(), np.abs(np.add(n.rotation, q)).max()) < 1e-9
        assert np.allclose(n.half, half) and n.kind == old.kind and n.source == old.source
    assert np.allclose(new[-1].vertices, cols[-1].vertices @ m[:3, :3].T + m[:3, 3])
    assert C.transform_colliders([], m) == []


def test_quat_mats_round_trip():
    rng = np.random.default_rng(1)
    q = rng.normal(size=(200, 4))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    back = C.quats_from_mats(C.quat_mats(q))
    same = np.minimum(np.abs(back - q).max(1), np.abs(back + q).max(1))
    assert same.max() < 1e-9


def _door(x0=0.0, width=1.2, height=2.2):
    corners = np.array([[x0, 0, 0], [x0 + width, 0, 0], [x0 + width, 0, height], [x0, 0, height]], float)
    w = D.Doorway("shop", 1, [0, 1], True, corners)
    w.floor = 0.0
    return w


def test_doorway_status_with_shared_box_arrays():
    wall_left = C.Collider("box", "concrete", center=(-1.0, 0, 1.5), half=(1.0, 0.1, 1.5), source="collision")
    wall_right = C.Collider("box", "concrete", center=(3.2, 0, 1.5), half=(1.0, 0.1, 1.5), source="collision")
    shutter = C.Collider("box", "metal", center=(0.6, 0, 1.1), half=(0.6, 0.05, 1.1), source="shutter_a")
    far = C.Collider("box", "concrete", center=(400.0, 0, 1.0), half=(1, 1, 1), source="far")
    open_door, shut_door = _door(), _door(x0=10.0)
    pool = [wall_left, wall_right, far]
    boxes = D.box_arrays(pool)
    a = D.doorway_status(open_door, pool)
    b = D.doorway_status(open_door, pool, boxes=boxes)
    assert a["status"] == b["status"] == "open" and a["passage"] == b["passage"] >= 0.9
    pool2 = pool + [shutter]
    st = D.doorway_status(open_door, pool2, suspects={"shutter_a"}, boxes=D.box_arrays(pool2))
    assert st["status"] == "blocked" and st["blockers"][0]["source"] == "shutter_a"
    assert D.doorway_status(shut_door, pool, boxes=boxes)["status"] == "open"
