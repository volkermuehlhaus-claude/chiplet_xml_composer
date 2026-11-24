# Tests for chiplet_xml_composer, on the shipped test stackups (copies of the latest IHP IntM4TM2 and SG13G2
# stackups) and examples. Needs gds2palace and chiplet-format-io.
#
# Run from the repo root:   python -m pytest

import copy
import json
import os
import xml.etree.ElementTree as ET

import pytest
import yaml

from gds2palace import stackup_reader
from chiplet_xml_composer import ComposeError, compose
from chiplet_xml_composer.composer import _clean_interposer_variables, _unique_name, main

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
TD = os.path.join(ROOT, "test_data")
EX = os.path.join(ROOT, "examples")
INTERPOSER = os.path.join(TD, "interposer_IntM4TM2.xml")
DIE = os.path.join(TD, "SG13G2_die.xml")
METHODS = os.path.join(TD, "interconnect_methods.json")
MATERIALS = os.path.join(TD, "connection_materials.json")
TWO_DIES = os.path.join(EX, "03_two_dies", "assembly.chiplet")
STACK_HEIGHT = 44.0      # cupillar_opt1: 28 um Cu + 16 um SnAg


def compose_two_dies(**kwargs):
    args = dict(chiplet_path=TWO_DIES, stackup_map={"intm4tm2": INTERPOSER, "sg13g2": DIE},
                attach_map={"die1": "Passive", "die2": "Passive"}, interconnect_methods_path=METHODS,
                connection_materials_path=MATERIALS, boundary_layer_map={"die1": 900, "die2": 901})
    args.update(kwargs)
    return compose(**args)


def resolved(tree, tmp_path, name="combined.xml", **overrides):
    """(xml root, dielectrics by name, metals by name) of a composed tree, read back by gds2palace."""
    path = os.path.join(str(tmp_path), name)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="UTF-8", xml_declaration=True)
    _, dielectrics, metals = stackup_reader.read_substrate(path, variable_overrides=overrides or None)
    return (ET.parse(path).getroot(), {d.name: d for d in dielectrics.dielectrics},
            {m.name: m for m in metals.metals})


def die_dielectrics(dielectrics, boundary):
    return sorted((d for d in dielectrics.values() if str(d.gdsboundary) == str(boundary)), key=lambda d: d.zmin)


def write_chiplet(tmp_path, edit):
    """examples/03_two_dies/assembly.chiplet, changed by edit(document), written to tmp_path."""
    with open(TWO_DIES, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    edit(doc)
    path = os.path.join(str(tmp_path), "assembly.chiplet")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, sort_keys=False)
    return path


# ---------- the shipped examples ----------

EXAMPLES = [
    ("01_direct_bond", ["--attach", "die1=Passive"]),
    ("02_flip_chip_cupillar", ["--attach", "die1=Passive", "--interconnect-methods", METHODS,
                               "--connection-materials", MATERIALS, "--boundary-layer", "die1=900"]),
    ("03_two_dies", ["--attach", "die1=Passive", "--attach", "die2=Passive", "--interconnect-methods", METHODS,
                     "--connection-materials", MATERIALS, "--boundary-layer", "die1=900",
                     "--boundary-layer", "die2=901"]),
]


@pytest.mark.parametrize("folder, flags", EXAMPLES)
def test_examples_are_reproduced(tmp_path, capsys, folder, flags):
    out = os.path.join(str(tmp_path), "combined.xml")
    argv = ["--chiplet-file", os.path.join(EX, folder, "assembly.chiplet"),
            "--stackup", f"intm4tm2={INTERPOSER}", "--stackup", f"sg13g2={DIE}", *flags, "-o", out]
    assert main(argv) == 0
    stdout = capsys.readouterr().out
    assert "WARNING" not in stdout and "no z-overlaps, no missing chiplet boundaries" in stdout
    with open(out, "rb") as f:
        new = f.read().replace(b"\r\n", b"\n")
    with open(os.path.join(EX, folder, "combined.xml"), "rb") as f:
        shipped = f.read().replace(b"\r\n", b"\n")
    assert new == shipped, f"examples/{folder}/combined.xml is not what the composer writes now"


# ---------- z-mounting of a connection stack (coord_frame_contract.md section 3.1) ----------

def test_die_body_starts_at_mount_plane_plus_stack_height(tmp_path):
    _, dielectrics, metals = resolved(compose_two_dies(), tmp_path)
    mount = metals["TopMetal2"].zmax                       # interposer's top metal = mount plane
    gap = dielectrics["Passive"].zmax - mount              # its oxide + passivation above that metal
    for die, boundary in (("die1", 1039), ("die2", 2039)):
        bond = dielectrics[f"{die}_bondline"]
        body = die_dielectrics(dielectrics, boundary)
        assert body[0].zmin == pytest.approx(mount + STACK_HEIGHT, abs=1e-6)
        assert bond.thickness == pytest.approx(STACK_HEIGHT - gap, abs=1e-6)
        cu, cap = metals[f"{die}_CuPillar"], metals[f"{die}_SnAgCap"]
        assert cu.zmin == pytest.approx(mount, abs=1e-6)
        assert cu.zmax - cu.zmin == pytest.approx(28.0, abs=1e-6)
        assert cap.zmin == pytest.approx(cu.zmax, abs=1e-6)
        # the cap ends on the die's pad: its bottommost metal after the flip
        pads = [m for m in metals.values() if not m.is_sheet and int(m.layernum) >= int(str(boundary)[0]) * 1000
                and int(m.layernum) < (int(str(boundary)[0]) + 1) * 1000]
        assert cap.zmax == pytest.approx(min(m.zmin for m in pads), abs=1e-6)


def test_stack_lower_than_interposer_dielectric_is_an_error(tmp_path):
    with open(METHODS, encoding="utf-8") as f:
        methods = json.load(f)
    for layer in methods["methods"]["cupillar_opt1"]["connection_stack"]["layers"]:
        layer["height"] = 0.9                               # 1.8 um in total, below the 1.9 um gap
    path = os.path.join(str(tmp_path), "methods.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(methods, f)
    with pytest.raises(ComposeError, match="inside the interposer"):
        compose_two_dies(interconnect_methods_path=path)


# ---------- interposer: outer AIR and Variables ----------

def test_interposer_air_removed_and_variables_cleaned(tmp_path):
    root, dielectrics, _ = resolved(compose_two_dies(), tmp_path)
    assert "Air" not in dielectrics
    assert {v.get("Name"): v.get("Value") for v in root.iter("Variable")} == {
        "interposer_total_thickness": "250", "interposer_bulk_thickness": "=interposer_total_thickness-12.71"}
    expressions = {(e.get("Name"), k, v) for e in root.iter() if e.tag != "Variable"
                   for k, v in e.attrib.items() if v.startswith("=")}
    assert expressions == {("Substrate_Interposer", "Thickness", "=interposer_bulk_thickness"),
                           ("LBE", "Zmax", "=interposer_bulk_thickness")}


def test_interposer_variable_stays_functional(tmp_path):
    tree = compose_two_dies()
    _, d250, m250 = resolved(copy.deepcopy(tree), tmp_path, "a.xml")
    _, d300, m300 = resolved(tree, tmp_path, "b.xml", interposer_total_thickness=300)
    assert d300["Substrate_Interposer"].thickness == pytest.approx(d250["Substrate_Interposer"].thickness + 50)
    for name in d250:
        if name != "Substrate_Interposer":
            assert d300[name].zmin == pytest.approx(d250[name].zmin + 50)
            assert d300[name].thickness == pytest.approx(d250[name].thickness)
    assert m300["die2_CuPillar"].zmin == pytest.approx(m250["die2_CuPillar"].zmin + 50)


def test_clean_interposer_variables_keeps_used_chain_and_renames():
    root = ET.fromstring(
        '<Stackup><Variables><Variable Name="a" Value="2"/><Variable Name="b" Value="=a*2 + interposer_a"/>'
        '<Variable Name="interposer_a" Value="1"/><Variable Name="c" Value="5"/><Variable Name="d" Value="=c+1"/>'
        '</Variables><ELayers><Dielectrics><Dielectric Name="X" Thickness="=( b + 3 )"/></Dielectrics></ELayers>'
        '</Stackup>')
    renames = _clean_interposer_variables(root, "interposer")
    assert renames == {"a": "interposer_a", "b": "interposer_b", "interposer_a": "interposer_interposer_a"}
    assert [v.get("Name") for v in root.iter("Variable")] == list(renames.values())
    assert root.find(".//Dielectric").get("Thickness") == "=( interposer_b + 3 )"
    variables = stackup_reader.variables_list()
    for el in root.iter("Variable"):
        variables.append(stackup_reader.variable(el))
    variables.resolve_all()
    assert float(stackup_reader.resolve_attr(root.find(".//Dielectric"), "Thickness", None, variables)) == 8.0


def test_clean_interposer_variables_drops_empty_block():
    root = ET.fromstring('<Stackup><Variables><Variable Name="air_thickness" Value="300"/></Variables>'
                         '<ELayers/></Stackup>')
    assert _clean_interposer_variables(root, "interposer") == {}
    assert root.find("Variables") is None


# ---------- --variable overrides and the thickness check ----------

def test_die_override_applies_to_that_die_only(tmp_path):
    root, dielectrics, metals = resolved(compose_two_dies(variable_overrides={"die1": {"total_thickness": 150}}),
                                         tmp_path)
    die1, die2 = die_dielectrics(dielectrics, 1039), die_dielectrics(dielectrics, 2039)
    assert sum(d.thickness for d in die1) == pytest.approx(150.0003, abs=1e-6)
    assert sum(d.thickness for d in die2) == pytest.approx(200.0003, abs=1e-6)
    lbe1 = next(m for m in metals.values() if m.layernum == "1157")          # die1's LBE spans its bulk + EPI
    assert lbe1.zmax - lbe1.zmin == pytest.approx(150 - 19.88 + 3.75, abs=1e-6)
    comments = [c.text for c in ET.parse(os.path.join(str(tmp_path), "combined.xml"),
                                         parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
                .getroot().iter(ET.Comment)]
    assert any("die1:" in c and "total_thickness=150 (override)" in c for c in comments)
    assert any("die2:" in c and "total_thickness=200," in c for c in comments)
    assert not any("--" in c for c in comments)


def test_interposer_override_sets_kept_variable():
    root = compose_two_dies(variable_overrides={"interposer": {"total_thickness": 300}}).getroot()
    assert {v.get("Name"): v.get("Value") for v in root.iter("Variable")}["interposer_total_thickness"] == "300"


@pytest.mark.parametrize("overrides, message", [
    ({"die3": {"total_thickness": 1}}, 'no interposer or die component "die3"'),
    ({"die1": {"thickness": 1}}, 'has no Variable "thickness"'),
    ({"die1": {"total_thickness": "abc"}}, "must be a number"),
])
def test_override_errors(overrides, message):
    with pytest.raises(ComposeError, match=message):
        compose_two_dies(variable_overrides=overrides)


def test_override_without_effect_warns():
    found = []
    compose_two_dies(variable_overrides={"interposer": {"air_thickness": 100}}, warnings=found)
    assert len(found) == 1 and "has no effect" in found[0]


def test_thickness_check(tmp_path):
    thickness = {"interposer": 250.0, "die1": 200.0, "die2": 150.0}

    def add_dimensions(doc):
        for component in doc["components"]:
            component["dimensions"] = {"width": 1000.0, "height": 1000.0, "thickness": thickness[component["id"]]}
    chiplet = write_chiplet(tmp_path, add_dimensions)
    found = []
    compose_two_dies(chiplet_path=chiplet, warnings=found)
    assert len(found) == 1 and 'component "die2"' in found[0] and "is 150 um" in found[0]
    found = []
    compose_two_dies(chiplet_path=chiplet, warnings=found, variable_overrides={"die2": {"total_thickness": 150}})
    assert found == []
    with pytest.warns(UserWarning, match='component "die2"'):          # no list given: Python warnings
        compose_two_dies(chiplet_path=chiplet)


# ---------- orientation ----------

def test_face_down_is_rejected(tmp_path):
    def set_face_down(doc):
        doc["components"][1]["orientation"] = "face_down"
    with pytest.raises(ComposeError, match='only "face_up"'):
        compose_two_dies(chiplet_path=write_chiplet(tmp_path, set_face_down))


# ---------- thermal data, die expressions, names ----------

def test_thermal_tables_and_die_expressions(tmp_path):
    # die: Metal1's thermal conductivity and one table point as expressions on a die-only variable
    die = ET.parse(DIE)
    droot = die.getroot()
    ET.SubElement(droot.find("Variables"), "Variable", Name="k_m", Value="237")
    next(m for m in droot.iter("Material") if m.get("Name") == "Metal1").set("ThermalConductivity", "=k_m")
    droot.find("Tables/Table/Point").set("Value", "=k_m/2")
    die_path = os.path.join(str(tmp_path), "die.xml")
    die.write(die_path)
    # interposer: a different table under the die table's name
    interposer = ET.parse(INTERPOSER)
    table = ET.SubElement(interposer.getroot().find("Tables"), "Table", Name="Si_vs_T_IHP")
    ET.SubElement(table, "Point", Temperature="300", Value="148")
    interposer_path = os.path.join(str(tmp_path), "interposer.xml")
    interposer.write(interposer_path)

    root = compose_two_dies(stackup_map={"intm4tm2": interposer_path, "sg13g2": die_path}).getroot()
    assert sorted(t.get("Name") for t in root.iter("Table")) == sorted(
        ["SiliconThermalCond", "Si_vs_T_IHP", "Si_vs_T_IHP_2"])
    materials = {m.get("Name"): m for m in root.iter("Material")}
    assert materials["Activ"].get("ThermalConductivityTable") == "Si_vs_T_IHP_2"       # die1
    assert materials["Activ_2"].get("ThermalConductivityTable") == "Si_vs_T_IHP_2"     # die2 shares it
    assert float(materials["Metal1"].get("ThermalConductivity")) == 237.0
    shared = next(t for t in root.iter("Table") if t.get("Name") == "Si_vs_T_IHP_2")
    assert float(shared.find("Point").get("Value")) == 118.5
    assert "k_m" not in {v.get("Name") for v in root.iter("Variable")}


def test_connection_material_thermal_conductivity(tmp_path):
    with open(MATERIALS, encoding="utf-8") as f:
        materials = json.load(f)
    for i, entry in enumerate(materials["materials"].values()):
        entry["thermal_conductivity"] = 10.0 + i
    path = os.path.join(str(tmp_path), "materials.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(materials, f)
    root = compose_two_dies(connection_materials_path=path).getroot()
    written = {m.get("Name"): m.get("ThermalConductivity") for m in root.iter("Material")}
    assert {name: written[name] for name in materials["materials"]} == {
        name: str(10.0 + i) for i, name in enumerate(materials["materials"])}
    assert sum(1 for m in root.iter("Material") if m.get("Name") == "CuPillar") == 1


def test_renamed_names_get_underscore_suffix():
    assert _unique_name({"a"}, "b") == "b"
    assert _unique_name({"a", "a_2"}, "a") == "a_3"
    names = {d.get("Name") for d in compose_two_dies().getroot().iter("Dielectric")}
    assert {"Passive", "Passive_2", "Passive_3", "SiO2_2", "SiO2_3", "EPI", "EPI_2"} <= names
