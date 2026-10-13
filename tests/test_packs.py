"""Car packs: folders named like "[PLOKS_CARS]", one car's warnings out of a 70-car pack, and the
wheel mesh most GTA cars share between all four corners."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fivem2open77.gta.resource import scan_resource  # noqa: E402
from fivem2open77.model.build import VehicleBuilder  # noqa: E402

VEHICLES = """<?xml version="1.0" encoding="UTF-8"?>
<CVehicleModelInfo__InitDataList><InitDatas>
  <Item><modelName>{m}</modelName><txdName>{m}</txdName><handlingId>{M}</handlingId>
        <gameName>{M}</gameName><vehicleMakeName>TEST</vehicleMakeName><vehicleClass>VC_SPORT</vehicleClass></Item>
</InitDatas></CVehicleModelInfo__InitDataList>"""
HANDLING = """<?xml version="1.0" encoding="UTF-8"?>
<CHandlingDataMgr><HandlingData><Item type="CHandlingData"><handlingName>{M}</handlingName>
  <fMass value="1500.0"/><fInitialDriveMaxFlatVel value="150.0"/></Item></HandlingData></CHandlingDataMgr>"""
MANIFEST = """resource_manifest_version '77731fab-63ca-442c-a67b-abc70f28dfa5'
files {{ 'data/vehicles.meta', 'data/handling.meta', 'data/carvariations.meta', 'data/carcols.meta' }}
data_file 'HANDLING_FILE' 'data/handling.meta'
data_file 'VEHICLE_METADATA_FILE' 'data/vehicles.meta'
data_file 'CARCOLS_FILE' 'data/carcols.meta'
data_file 'VEHICLE_VARIATION_FILE' 'data/carvariations.meta'
data_file 'VEHICLE_LAYOUTS_FILE' 'data/vehiclelayouts.meta'
"""


def _car(root, rel, model, carcols="<CVehicleModelInfoVarGlobal/>", handling=True):
    d = os.path.join(root, *rel.split("/"), model)
    os.makedirs(os.path.join(d, "data"))
    os.makedirs(os.path.join(d, "stream"))
    open(os.path.join(d, "__resource.lua"), "w").write(MANIFEST)
    open(os.path.join(d, "data", "vehicles.meta"), "w").write(VEHICLES.format(m=model, M=model.upper()))
    if handling:
        open(os.path.join(d, "data", "handling.meta"), "w").write(HANDLING.format(M=model.upper()))
    open(os.path.join(d, "data", "carvariations.meta"), "w").write("<CVehicleModelInfoVariation/>")
    open(os.path.join(d, "data", "carcols.meta"), "w").write(carcols)
    return d


def test_bracketed_pack_folders_and_warnings_per_car(tmp_path):
    root = str(tmp_path / "FiveM-Civ-Car-Pack")
    e400 = _car(root, "[PLOKS_CARS]/[MERCEDES]", "e400")
    _car(root, "[PLOKS_CARS]/[AUDI]", "r8ppi", carcols="<broken>", handling=False)
    res = scan_resource(root)
    try:
        # every declared file that exists is found: "[PLOKS_CARS]" is a folder name, not a glob
        assert not [w for w in res.warnings if "e400" in w and "missing" in w], res.warnings
        assert {os.path.basename(os.path.dirname(os.path.dirname(f))) for f in res.meta_files["handling"]} == {"e400"}
        # a missing vehiclelayouts.meta is not worth a line: the conversion does not read it
        assert not [w for w in res.warnings if "VEHICLE_LAYOUTS_FILE" in w]
        # the other car's real problems are reported, and belong to that car
        assert any("r8ppi" in w and "HANDLING_FILE" in w for w in res.warnings)
        assert any("cannot parse" in w and "r8ppi" in w for w in res.warnings)
        me = SimpleNamespace(yft=os.path.join(e400, "stream", "e400.yft"))
        mine = res.warnings_for(me)
        assert not [w for w in mine if "r8ppi" in w], mine
        assert mine and mine[-1].endswith("about other cars in this pack (not this one)")
        assert res.resource_of(os.path.join(e400, "data", "handling.meta")) == os.path.normcase(e400)
    finally:
        res.cleanup()


class _Geo:
    def __init__(self, n):
        import numpy as np
        self.triangle_count = n
        self.shader_index = 0
        self.indices = np.zeros(3 * n, np.uint32)
        self.attrs = {"position": np.zeros((3, 3), np.float32)}


def _drawable(n):
    return SimpleNamespace(models={"high": [SimpleNamespace(geometries=[_Geo(n)])]})


def test_rear_wheels_borrow_the_front_mesh_when_their_drawables_are_empty():
    """Most GTA cars ship one wheel mesh (wheel_lf); the other corners' drawables are empty and the
    game draws the front wheel there.  The E400 lost its rear wheels to that."""
    b = object.__new__(VehicleBuilder)
    names = ["chassis", "wheel_lf", "wheel_rf", "wheel_lr", "wheel_rr"]
    b.names = names
    b.sk = SimpleNamespace(bones=[SimpleNamespace(tag=i) for i in range(len(names))])
    b.frag = SimpleNamespace(children=[SimpleNamespace(drawable=_drawable(n), bone_tag=i)
                                       for i, n in ((1, 12), (2, 0), (3, 0), (4, 0))])
    b.vehicle = SimpleNamespace(axles={})
    b.warnings = []
    b.wheel_pieces = {}
    used = {}
    b._wheel_meshes = lambda axle, pieces, mirror: used.__setitem__(axle, (len(pieces), mirror))
    b.collect_wheels()
    assert used == {"front": (1, False), "back": (1, False)}, used
    assert not b.warnings


def test_a_resource_without_metas_lists_the_car_not_its_kit_parts(tmp_path):
    """A Camaro whose vehicles.meta is missing ships mod-kit parts named after another car's kit
    (sturbo_bumf_1): only the model with its own textures is a car."""
    d = tmp_path / "69camaross" / "stream"
    d.mkdir(parents=True)
    for n in ("69camaross.yft", "69camaross_hi.yft", "69camaross.ytd", "sturbo_bumf_1.yft", "sturbo_hood_2.yft",
              "sabregt_cage.yft", "69camaross_wing_1.yft"):
        (d / n).write_bytes(b"x")
    res = scan_resource(str(tmp_path / "69camaross"))
    try:
        assert [v.model for v in res.vehicles] == ["69camaross"]
    finally:
        res.cleanup()


def test_studio_caches_are_per_converter_version(monkeypatch):
    """An upgraded Studio re-reads a project instead of showing the old version's findings."""
    from fivem2open77.studio import analysis
    k1 = analysis._key({"neon": {}}, "abc")
    monkeypatch.setattr(analysis, "VERSION", "9.9.9")
    assert analysis._key({"neon": {}}, "abc") != k1
