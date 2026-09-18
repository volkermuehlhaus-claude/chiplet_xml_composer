#
# Copyright 2025 Volker Muehlhaus and IHP PDK Authors
#
# Licensed under the GNU General Public License, Version 3.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    https://www.gnu.org/licenses/gpl-3.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
########################################################################
#
# chiplet_xml_composer: stitches several per-chiplet gds2palace stackup XML files into one
# combined stackup XML, driven by an IHP .chiplet assembly YAML file - see
# https://github.com/IHP-GmbH/chiplet-spec/blob/dev/docs/CHIPLET_FORMAT_SPEC.md
#
# Generalizes the by-hand steps already proven out in
# gds2palace_ihp_sg13g2/test_data/chiplet/merge_chiplet_stackup.py (attach one chiplet's own
# Reference-chained Dielectric/Layer stack onto a base/interposer stackup at a named
# attachment Dielectric, renaming colliding names and shifting colliding GDSII Layer=
# numbers) into a tool driven by a proper assembly description instead of hardcoded
# constants for one hardcoded pair.
#
# Scope (v1): Z-stack merge only. This produces one combined stackup XML - it does not
# read/place/rotate/mirror any GDS geometry. A .chiplet component's `position`/`rotation`/
# `anchor` fields (everything about where a die sits in x/y) are read for informational
# purposes only and never drive output here.
#
# This is a standalone package, deliberately kept out of gds2palace_ihp_sg13g2/gds2openEMS:
# strip_outer_air_dielectrics()/reverse_stackup_z_order() below started out as new methods on
# gds2palace's own util_stackup_reader.dielectric_layers_list, but that coupled a published,
# cross-repo-duplicated reader module's release cadence to functionality only this one tool
# needs, and left it holding a documented inconsistency (see each function's docstring) other
# code on that shared class doesn't have. They live here instead as plain functions, operating
# on the already-public objects stackup_reader.parse_substrate() returns - gds2palace itself is
# an ordinary declared dependency (see pyproject.toml), not something this file's presence
# changes the release cadence of.
#
# chiplet_format_io (the .chiplet YAML reader) is a hard dependency (see pyproject.toml) - every
# user of this package needs it, unlike gds2palace/setupEM, which never touch chiplet
# composition at all.
#
# Run directly:
#   python -m chiplet_xml_composer.composer --chiplet-file assembly.chiplet \
#       --stackup tech_a=stackup_a.xml --stackup tech_b=stackup_b.xml \
#       --attach die1=iPassive --attach die2=iPassive \
#       --interconnect-methods interconnect_methods.json \
#       --connection-materials connection_materials.json \
#       --boundary-layer die1=235 --boundary-layer die2=236 \
#       -o combined.xml
# or, once installed, via the console script:
#   chiplet_xml_composer --chiplet-file assembly.chiplet ...
#
# Or import compose() directly:
#   from chiplet_xml_composer import compose
#   compose(chiplet_path=..., stackup_map=..., ...)

__version__ = "1.0.0"

import argparse
import copy
import json
import os
import sys
import xml.etree.ElementTree as ET

import chiplet_format_io as cfio
from gds2palace import stackup_reader


class ComposeError (Exception):
  """Raised for any chiplet_xml_composer-specific hard error (missing mapping, unresolvable
     material, ambiguous component, ...) - always printed with a clear message and exit(1)'d
     by main(), or left for an API caller to catch directly.
  """
  pass


# -------------------- chiplet-composition-only stackup preprocessing ---------------------------
#
# These two operate purely on already-resolved dielectric_layers_list/metal_layers_list objects
# (i.e. the output of stackup_reader.parse_substrate()/read_substrate()) - every "="-expression,
# Reference/ReferenceEdge chain, or absolute/implicit position in the source XML is already
# baked into plain zmin/zmax/thickness floats by the time either function runs, so neither needs
# to know how a value was originally spelled in the XML.
#
# Neither recomputes metal_layer.above/.below or dielectric_layer.metals_inside (both stale
# after either call) - compose() below never reads that bookkeeping from these objects; it
# always re-derives ownership itself and gets a fully correct, fresh recompute once the final
# combined XML is written and re-parsed. A caller using these for some other purpose would need
# to re-parse (or otherwise recompute that bookkeeping) before relying on it.

def strip_outer_air_dielectrics (dielectrics_list):
  """Remove any Dielectric at this stack's own outermost z (its topmost and/or bottommost, per
     the is_top/is_bottom flags parse_substrate() already sets) whose Material resolves to the
     built-in "AIR" material - not just a Dielectric literally named "AIR".

     A standalone die's own stackup XML conventionally carries an AIR dielectric representing
     open space above (or below) its own physical body - meaningless once that die is bonded
     into a larger assembly at that surface (the role is played by whatever it now bonds to
     instead), or once it becomes an inner chiplet whose own true outer boundary is the shared
     base's AIR instead. Generalizes the by-hand step used to build gpdk180_reversed.xml
     (dropping gpdk180's own "AIR" and attaching "IMD" directly to the new bridging dielectric).

     Every remaining Dielectric's resolved zmin/zmax is left bit-for-bit unchanged: AIR is
     physically empty space, so removing it never needs to shift anything else - it only closes
     a gap that had nothing positioned inside it. Only a single, non-cascading check is made per
     end (this stack's original outermost Dielectric, not whatever becomes outermost after a
     removal) - two stacked AIR dielectrics at one end is not a pattern any fixture in this
     workspace uses. Never removes the stack's only Dielectric (checked before removing
     anything, using the original list): a warning is printed and nothing is removed in that
     case, since a stackup with zero Dielectrics cannot be resolved.

     Call only on an already-resolved dielectric_layers_list (post calculate_zpositions()) -
     i.e. after read_substrate()/parse_substrate(). This only removes list entries; it does not
     touch metals_list (an AIR dielectric should never legitimately have metals inside it).
  Args:
      dielectrics_list (gds2palace.stackup_reader.dielectric_layers_list): a single, standalone
        chiplet's own dielectrics (not a whole multi-chiplet assembly - mirroring every
        dielectric in an already-merged file around one pivot would incorrectly flip the
        interposer and every other chiplet too)
  Returns:
      list of str: names of the Dielectric(s) actually removed, empty if none qualified
  """
  candidates = [d for d in dielectrics_list.dielectrics if (d.is_top or d.is_bottom) and d.material == "AIR"]
  if not candidates:
    return []
  if len(candidates) >= len(dielectrics_list.dielectrics):
    print('WARNING: strip_outer_air_dielectrics() would remove every Dielectric in this '
          'stack - leaving it untouched instead: ', [d.name for d in candidates])
    return []
  for dielectric in candidates:
    dielectrics_list.dielectrics.remove(dielectric)
  return [d.name for d in candidates]


def reverse_stackup_z_order (dielectrics_list, metals_list, pivot_z):
  """Mirror every Dielectric's and every Layer's resolved [zmin, zmax] around pivot_z, reversing
     the stack's build order while leaving every element's own thickness unchanged - e.g. what
     was the top-most Dielectric becomes the bottom-most, at a matching distance from pivot_z on
     the other side.

     Generalizes the by-hand mirroring used to build gpdk180_reversed.xml from gpdk180.xml for
     flip-chip mounting: a die mounted face-down needs its own internal z-stack reversed (what
     was near its top surface now faces the interposer), which the .chiplet format itself never
     specifies - orientation: flip_chip there only mirrors the GDS artwork in X and offsets the
     mounting z, per coord_frame_contract.md section 2.4. The caller picks pivot_z (the
     confirmed convention is the die's own attachment/bond surface - in practice, whichever
     resolved z was exposed by strip_outer_air_dielectrics() removing the AIR dielectric that
     used to sit at that surface).

     Call only on already-resolved lists (post calculate_zpositions()/resolve_references()).
     Recomputes is_top/is_bottom on the mirrored dielectrics (whichever end was highest is now
     lowest and vice versa); does not touch metals_list.above/below or
     dielectrics_list.metals_inside, which go stale under a z-mirror.
  Args:
      dielectrics_list (gds2palace.stackup_reader.dielectric_layers_list): a single, standalone
        chiplet's own dielectrics (see strip_outer_air_dielectrics() for why not a whole
        multi-chiplet assembly)
      metals_list (gds2palace.stackup_reader.metal_layers_list): this stack's own metals,
        mirrored alongside its dielectrics - not a whole-file metals_list that includes other
        chiplets/interposer metals, since every one of those would be mirrored too
      pivot_z (float): the z-value everything is mirrored around
  """
  for dielectric in dielectrics_list.dielectrics:
    old_zmin, old_zmax = dielectric.zmin, dielectric.zmax
    dielectric.zmin = 2 * pivot_z - old_zmax
    dielectric.zmax = 2 * pivot_z - old_zmin
    dielectric.thickness = dielectric.zmax - dielectric.zmin

  for metal in metals_list.metals:
    old_zmin, old_zmax = metal.zmin, metal.zmax
    metal.zmin = 2 * pivot_z - old_zmax
    metal.zmax = 2 * pivot_z - old_zmin
    metal.thickness = metal.zmax - metal.zmin

  if dielectrics_list.dielectrics:
    for dielectric in dielectrics_list.dielectrics:
      dielectric.is_top = False
      dielectric.is_bottom = False
    lowest = min(dielectrics_list.dielectrics, key=lambda d: d.zmin)
    highest = max(dielectrics_list.dielectrics, key=lambda d: d.zmax)
    lowest.is_bottom = True
    highest.is_top = True


# -------------------- .chiplet / interconnect_methods.json loading ---------------------------

def _load_chiplet_file (path):
  """Load a .chiplet assembly YAML file via the chiplet_format_io reference library.
  Args:
      path (string): path to the .chiplet file
  Returns:
      dict: the parsed, validated assembly document
  Raises:
      ComposeError: the file fails to load/validate
  """
  try:
    return cfio.load(path)
  except cfio.ChipletFormatError as e:
    raise ComposeError(f'Could not load .chiplet file "{path}": {e}')


def _load_interconnect_methods (path):
  """Load an interconnect_methods.json registry (see
     https://github.com/IHP-GmbH/chiplet-spec/blob/dev/examples/interconnect_methods.json) -
     a plain JSON sidecar, not part of the .chiplet format itself; a component's `connection:`
     id is only meaningful together with this file (see the "methods" map).
  Args:
      path (string): path to an interconnect_methods.json file
  Returns:
      dict: the parsed registry
  Raises:
      ComposeError: the file doesn't exist or isn't valid JSON
  """
  if not os.path.isfile(path):
    raise ComposeError(f'--interconnect-methods file not found: "{path}"')
  try:
    with open(path, "r", encoding="utf-8") as f:
      return json.load(f)
  except json.JSONDecodeError as e:
    raise ComposeError(f'Could not parse interconnect_methods.json "{path}": {e}')


def _resolve_connection_stack (interconnect_methods, connection_id):
  """Resolve a component's `connection:` id into its ordered bump/pillar layers, each with a
     real GDS layer number from the registry's `layer_registry` - a .chiplet file's own
     optional `connection_stacks` block never carries a GDS layer number, even when
     self-contained, so this always goes through interconnect_methods.json instead.
  Args:
      interconnect_methods (dict): parsed interconnect_methods.json
      connection_id (string): a component's `connection:` value
  Returns:
      list of dict: [{"name":, "material":, "height":, "diameter":, "gds_layer":,
        "gds_datatype":}, ...], bottom (interposer side) to top, in the same order the
        registry's own connection_stack.layers[] declares
  Raises:
      ComposeError: connection_id, or one of its layer names, isn't in the registry
  """
  methods = interconnect_methods.get("methods", {})
  method = methods.get(connection_id)
  if method is None:
    raise ComposeError(
        f'connection: "{connection_id}" is not a known method in this interconnect_methods.json '
        f'(known methods: {sorted(methods.keys())})')

  layer_registry = interconnect_methods.get("layer_registry", {})
  resolved = []
  for layer in method.get("connection_stack", {}).get("layers", []):
    name = layer["name"]
    registry_entry = layer_registry.get(name)
    if registry_entry is None:
      raise ComposeError(
          f'connection_stack layer "{name}" (used by method "{connection_id}") has no entry in '
          f'this interconnect_methods.json\'s layer_registry - cannot resolve its GDS layer number')
    resolved.append({
        "name": name,
        "material": layer["material"],
        "height": float(layer["height"]),
        "diameter": float(layer.get("diameter", 0.0)),
        "gds_layer": int(registry_entry["gds_layer"]),
        "gds_datatype": registry_entry.get("gds_datatype"),
    })
  return resolved


# -------------------- .chiplet component selection ---------------------------

def _components (chiplet_data):
  return chiplet_data.get("components", []) or []


def _pick_interposer_component (chiplet_data):
  """Returns:
      dict: the single component with type "interposer"
  Raises:
      ComposeError: zero or more than one such component exists - v1 assumes a flat "2.5D"
        assembly (one shared interposer/base, N dies attached directly to it), not die-on-die
        stacking or multiple interposers
  """
  interposers = [c for c in _components(chiplet_data) if c.get("type") == "interposer"]
  if len(interposers) != 1:
    raise ComposeError(
        f'Expected exactly one component with type: interposer, found {len(interposers)} - '
        f'chiplet_xml_composer v1 only supports a single shared interposer/base')
  return interposers[0]


def _pick_die_components (chiplet_data):
  """Returns:
      list of dict: every component with type "die" or "die_array" - "substrate" and
        "interposer" components are out of scope for v1
  """
  return [c for c in _components(chiplet_data) if c.get("type") in ("die", "die_array")]


# -------------------- name/GDS-layer collision avoidance ---------------------------

def _unique_name (existing_names, base):
  """Same pattern as setupEM's stackupEditor._unique_name(): base if it's free, else
     base2, base3, ... until one is.
  Args:
      existing_names (set of string): names already in use
      base (string): preferred name
  Returns:
      string: base, or the first base<N> that's free
  """
  if base not in existing_names:
    return base
  n = 2
  while f"{base}{n}" in existing_names:
    n += 1
  return f"{base}{n}"


def _element_names (root, tag):
  return {el.get("Name") for el in root.iter(tag) if el.get("Name") is not None}


def _gds_layer_numbers (root):
  """Every GDSII layer number already used in root: <Layer Layer="...">,
     <DerivedLayer Layer="...">, and every <Operand Layer="..."> inside a DerivedLayer -
     mirrors merge_chiplet_stackup.py's own GDSII_LAYER_OFFSET reasoning (regular Layers,
     DerivedLayer targets, and DerivedLayer Operand references must all be considered, since
     a boolean operation's operands must keep resolving after a shift).
  Returns:
      set of int
  """
  numbers = set()
  for el in root.iter("Layer"):
    if el.get("Layer") is not None:
      numbers.add(int(el.get("Layer")))
  for el in root.iter("DerivedLayer"):
    if el.get("Layer") is not None:
      numbers.add(int(el.get("Layer")))
    for operand in el.findall("Operand"):
      if operand.get("Layer") is not None:
        numbers.add(int(operand.get("Layer")))
  return numbers


def _pick_gds_layer_offset (chip_layer_numbers, used_layer_numbers):
  """Choose the smallest multiple of 1000 that shifts every one of chip_layer_numbers clear of
     used_layer_numbers - mirrors this ecosystem's existing convention of round-number GDSII
     layer offsets (e.g. the interposer's own "i"-prefixed layers are already +1000 from their
     un-prefixed counterparts; merge_chiplet_stackup.py's GDSII_LAYER_OFFSET=2000 for gpdk180).
  Args:
      chip_layer_numbers (set of int): this chiplet's own native GDS layer numbers
      used_layer_numbers (set of int): every GDS layer number already spoken for (base +
        every earlier chiplet already merged)
  Returns:
      int: offset to add to every one of this chiplet's own GDS layer numbers
  """
  offset = 0
  while chip_layer_numbers and any((n + offset) in used_layer_numbers for n in chip_layer_numbers):
    offset += 1000
  return offset


class _NameRenamer:
  """Tracks Material/Dielectric/Layer name collisions across the base file and every chiplet
     merged so far, and assigns each chiplet's own colliding names a unique replacement
     (base name + component id suffix, via _unique_name()). One instance covers a single
     compose() run; call rename() once per element before appending it to the combined tree,
     in the order Materials -> Dielectrics -> Layers -> DerivedLayers, so a later element's own
     Material=/Reference=/Operand-Layer= pointers can look up an earlier element's new name.
  """

  def __init__ (self, base_root):
    self.material_names = _element_names(base_root, "Material")
    self.dielectric_names = _element_names(base_root, "Dielectric")
    self.layer_names = _element_names(base_root, "Layer")
    self.used_gds_layers = _gds_layer_numbers(base_root)
    # per-chiplet rename maps, reset by start_chiplet() - old name -> new name, one dict per
    # element kind, since a Material and a Dielectric may legitimately share one name in the
    # source format (they're different namespaces) and must not collide with each other's map
    self._material_renames = {}
    self._dielectric_renames = {}
    self._layer_renames = {}
    self._gds_layer_offset = 0

  def start_chiplet (self, chip_root):
    """Call once per chiplet, before renaming any of its elements: computes and remembers this
       chiplet's own GDS layer offset (chosen clear of every layer number used so far,
       including by earlier chiplets) and resets the per-chiplet name-rename maps.

       Immediately reserves this chiplet's own (now-offset) layer numbers in
       used_gds_layers, so the NEXT chiplet's own _pick_gds_layer_offset() call is chosen
       clear of them too - without this, every chiplet would only ever avoid colliding with
       the base file, not with each other.
    """
    self._material_renames = {}
    self._dielectric_renames = {}
    self._layer_renames = {}
    chip_layer_numbers = _gds_layer_numbers(chip_root)
    self._gds_layer_offset = _pick_gds_layer_offset(chip_layer_numbers, self.used_gds_layers)
    self.used_gds_layers.update(n + self._gds_layer_offset for n in chip_layer_numbers)

  def gds_layer_offset (self):
    return self._gds_layer_offset

  def _rename (self, kind, used_names, renames, old_name):
    if old_name in renames:
      return renames[old_name]
    new_name = _unique_name(used_names, old_name)
    renames[old_name] = new_name
    used_names.add(new_name)
    return new_name

  def rename_material (self, old_name):
    return self._rename("Material", self.material_names, self._material_renames, old_name)

  def rename_dielectric (self, old_name):
    return self._rename("Dielectric", self.dielectric_names, self._dielectric_renames, old_name)

  def rename_layer (self, old_name):
    return self._rename("Layer", self.layer_names, self._layer_renames, old_name)

  def material_lookup (self, old_name):
    """Look up this chiplet's own already-assigned rename for a Material name, without
       assigning a new one - for resolving a Dielectric/Layer's Material= pointer, which must
       reuse whatever rename_material() already gave that Material, or the original name if
       that Material was never renamed at all (no collision).
    """
    return self._material_renames.get(old_name, old_name)


# -------------------- connection-material sidecar (--connection-materials) ---------------------------
#
# Neither the .chiplet file nor a real interconnect_methods.json carries electrical/thermal
# properties for a connection_stack layer's material or the bridging Dielectric's material -
# IHP's own interconnect_methods.json only ever names a material (layer_registry.<name>.material,
# connection_stack.layers[].material), it never gives it a Conductivity/Permittivity. Rather than
# require these to be smuggled into an unrelated interposer/die stackup XML (which they are not
# physically part of), or invent a value, chiplet_xml_composer requires a separate, explicitly
# project-specific sidecar file (--connection-materials) that has no counterpart in the real
# ecosystem - see test_data/connection_materials.json for the shape and sourced example values.

def _load_connection_materials (path):
  """Load a --connection-materials sidecar JSON file.
  Args:
      path (string): path to the file
  Returns:
      dict: the parsed sidecar - see test_data/connection_materials.json for the shape
  Raises:
      ComposeError: the file doesn't exist or isn't valid JSON
  """
  if not os.path.isfile(path):
    raise ComposeError(f'--connection-materials file not found: "{path}"')
  try:
    with open(path, "r", encoding="utf-8") as f:
      return json.load(f)
  except json.JSONDecodeError as e:
    raise ComposeError(f'Could not parse --connection-materials file "{path}": {e}')


def _topmost_metal (metals_list, context):
  """The non-sheet metal with the highest resolved zmax in metals_list - the real conductor
     surface a connection_stack's lower end should anchor to (a Type="sheet" Layer is a
     zero-thickness idealized boundary condition, e.g. a PEC backside ground plane, never a
     real bond pad, so it's excluded even if it happens to be the outermost element).
  Args:
      metals_list (gds2palace.stackup_reader.metal_layers_list): already-resolved metals
      context (string): human-readable description of metals_list's owner, for the error message
  Returns:
      gds2palace.stackup_reader.metal_layer
  Raises:
      ComposeError: metals_list has no non-sheet metal at all
  """
  candidates = [m for m in metals_list.metals if not m.is_sheet]
  if not candidates:
    raise ComposeError(f'{context} has no non-sheet metal Layer for a connection_stack to '
                        f'anchor to')
  return max(candidates, key=lambda m: m.zmax)


def _bottommost_metal (metals_list, context):
  """The non-sheet metal with the lowest resolved zmin in metals_list - see _topmost_metal()."""
  candidates = [m for m in metals_list.metals if not m.is_sheet]
  if not candidates:
    raise ComposeError(f'{context} has no non-sheet metal Layer for a connection_stack to '
                        f'anchor to')
  return min(candidates, key=lambda m: m.zmin)


def _ensure_connection_material (base_materials_el, renamer, connection_materials,
                                  added_material_names, material_name, context):
  """Add a <Material> element for material_name, sourced from --connection-materials, unless
     one was already added earlier in this same compose() run (a connection method reused by
     several dies - e.g. the same Cu-pillar method on two dies - must get exactly one shared
     Material, not a renamed duplicate per die, the same way its GDS layer number is shared
     rather than offset per chiplet). No-op if material_name was already added.
  Args:
      base_materials_el (xml.etree.ElementTree.Element): the combined output's <Materials>
      renamer (_NameRenamer): used only to check for a name collision against every Material
        already contributed by the interposer or by an earlier die's own <Materials> - never to
        rename material_name itself, since it must stay exactly what interconnect_methods.json's
        connection_stack/--bondline-material named
      connection_materials (dict): parsed --connection-materials sidecar
      added_material_names (set of string): names already added this compose() run - mutated
      material_name (string): the Material name to ensure exists
      context (string): human-readable description, for error messages (e.g. "connection_stack
        layer 'CuPillar' (method 'cupillar_opt1')" or "component 'die1''s bridging Dielectric")
  Raises:
      ComposeError: material_name isn't defined in connection_materials, or collides with a
        Material already contributed by the interposer or a die's own stackup XML
  """
  if material_name in added_material_names:
    return
  if material_name in renamer.material_names:
    raise ComposeError(
        f'Material "{material_name}" (needed for {context}, from --connection-materials) '
        f'collides with a Material of the same name already defined in an input stackup XML - '
        f'rename one of them')
  spec = connection_materials.get("materials", {}).get(material_name)
  if spec is None:
    raise ComposeError(
        f'Material "{material_name}" (needed for {context}) has no entry in this '
        f'--connection-materials file\'s "materials" map')

  el = ET.SubElement(base_materials_el, "Material")
  el.set("Name", material_name)
  material_type = spec.get("type", "")
  el.set("Type", material_type)
  if material_type.lower() == "conductor":
    if "conductivity" not in spec:
      raise ComposeError(
          f'Material "{material_name}" (needed for {context}) is Type="Conductor" in '
          f'--connection-materials but has no "conductivity"')
    el.set("Conductivity", str(spec["conductivity"]))
  elif material_type.lower() == "dielectric":
    if "permittivity" not in spec:
      raise ComposeError(
          f'Material "{material_name}" (needed for {context}) is Type="Dielectric" in '
          f'--connection-materials but has no "permittivity"')
    el.set("Permittivity", str(spec["permittivity"]))
    if "dielectric_loss_tangent" in spec:
      el.set("DielectricLossTangent", str(spec["dielectric_loss_tangent"]))
  else:
    raise ComposeError(
        f'Material "{material_name}" (needed for {context}) has unsupported '
        f'"type": "{material_type}" in --connection-materials (must be "Conductor" or '
        f'"Dielectric")')
  if "color" in spec:
    el.set("Color", spec["color"])

  renamer.material_names.add(material_name)
  added_material_names.add(material_name)


def compose (chiplet_path, stackup_map, interconnect_methods_path=None,
             connection_materials_path=None, attach_map=None, boundary_layer_map=None,
             bondline_material="Underfill"):
  """Stitch every die component in a .chiplet assembly onto its interposer's stackup XML,
     producing one combined stackup XML tree.
  Args:
      chiplet_path (string): path to the .chiplet assembly file
      stackup_map (dict): technology id -> our own stackup XML path - a .chiplet file's own
        technology.stackup field is a different, YAML schema and is never read here
      interconnect_methods_path (string, optional): path to interconnect_methods.json -
        required if any die component declares a `connection:`
      connection_materials_path (string, optional): path to a --connection-materials sidecar
        JSON file (see test_data/connection_materials.json) - required under the same condition
        as interconnect_methods_path. Supplies electrical/thermal properties for every
        connection_stack layer's material and for bondline_material below - these are never
        looked up in an input stackup XML, since they are not physically part of either piece
        being joined, and neither the .chiplet file nor a real interconnect_methods.json carries
        this data at all.
      attach_map (dict, optional): component id -> Dielectric name in the interposer's own
        stackup XML that this component attaches to. Every die component must have an entry.
      boundary_layer_map (dict, optional): component id -> GDS layer number to restrict that
        component's bridging (bondline) Dielectric's Boundary= to. Only required for a
        component that actually gets a bridging Dielectric (i.e. has a connection_stack with
        nonzero total height) - nothing in the .chiplet file or interconnect_methods.json
        names a boundary layer for the bridging dielectric itself (only the via/pillar metal
        layers get one, from interconnect_methods.json's layer_registry).
      bondline_material (string, optional): Material name for each bridging Dielectric - looked
        up in connection_materials_path (default "Underfill" - see test_data/connection_materials.json
        for a sourced example)
  Returns:
      xml.etree.ElementTree.ElementTree: the combined stackup, ready to write with
        stackup_reader-compatible structure (call .write(path, encoding="UTF-8",
        xml_declaration=True) after xml.etree.ElementTree.indent())
  Raises:
      ComposeError: any unresolvable input (missing mapping, undefined material, ambiguous
        component, ...)
  """
  attach_map = attach_map or {}
  boundary_layer_map = boundary_layer_map or {}

  chiplet_data = _load_chiplet_file(chiplet_path)
  interposer_component = _pick_interposer_component(chiplet_data)
  die_components = _pick_die_components(chiplet_data)

  interconnect_methods = None
  connection_materials = None
  if any(die.get("connection") for die in die_components):
    if interconnect_methods_path is None:
      raise ComposeError(
          'At least one component declares connection:, but no --interconnect-methods was '
          'given - it is required whenever any component uses connection: (a GDS layer number '
          'for a connection_stack layer is never available from the .chiplet file alone)')
    interconnect_methods = _load_interconnect_methods(interconnect_methods_path)
    if connection_materials_path is None:
      raise ComposeError(
          'At least one component declares connection:, but no --connection-materials was '
          'given - it is required whenever any component uses connection: (electrical '
          'properties for a connection_stack layer\'s material are never available from the '
          '.chiplet file or interconnect_methods.json alone)')
    connection_materials = _load_connection_materials(connection_materials_path)

  def stackup_path_for (component):
    tech = component.get("technology")
    path = stackup_map.get(tech)
    if path is None:
      raise ComposeError(
          f'Component "{component.get("id")}" uses technology "{tech}", which has no '
          f'--stackup mapping (known: {sorted(stackup_map.keys())})')
    return path

  base_path = stackup_path_for(interposer_component)
  base_tree = ET.parse(base_path)
  base_root = base_tree.getroot()
  # parsed once, before any die is appended below, so a connection_stack's lower anchor (see
  # _topmost_metal() below) always resolves against the interposer's own original metals -
  # never against a metal an earlier die in this same compose() run just added
  _, base_dielectrics, base_metals = stackup_reader.parse_substrate(base_root)

  chip_roots_by_id = {}
  for die in die_components:
    chip_roots_by_id[die["id"]] = ET.parse(stackup_path_for(die)).getroot()

  renamer = _NameRenamer(base_root)
  added_connection_material_names = set()  # shared across every die this compose() run - see
                                            # _ensure_connection_material()'s own docstring
  base_dielectrics_el = base_root.find("./ELayers/Dielectrics")
  base_layers_el = base_root.find("./ELayers/Layers")
  base_materials_el = base_root.find("./Materials")

  for die in die_components:
    component_id = die["id"]
    chip_root = chip_roots_by_id[component_id]

    attach_point = attach_map.get(component_id)
    if attach_point is None:
      raise ComposeError(
          f'Component "{component_id}" has no --attach mapping (which Dielectric in the '
          f'interposer\'s stackup it attaches to)')

    renamer.start_chiplet(chip_root)
    gds_offset = renamer.gds_layer_offset()

    # ---- read and preprocess this chiplet's own resolved stackup ----
    _, chip_dielectrics, chip_metals = stackup_reader.parse_substrate(chip_root)
    strip_outer_air_dielectrics(chip_dielectrics)

    orientation = die.get("orientation", "face_up")
    if orientation == "flip_chip":
      # pivot at this stack's own current topmost surface. If it had an outer AIR dielectric,
      # the line above already removed it, so this is the real dielectric that used to sit
      # right under that AIR - the die's own attach/bond surface. If it never had one, this is
      # simply that die's own real outermost dielectric, which is exactly the same surface -
      # AIR was never required to exist for this pivot to be correct, only to be removed *if*
      # present, so nothing about this flip depends on the outcome of the line above.
      pivot_z = max(d.zmax for d in chip_dielectrics.dielectrics)
      reverse_stackup_z_order(chip_dielectrics, chip_metals, pivot_z)

    # sorted once here (not just where the Dielectrics loop used to do it) - the connection-stack
    # anchor below and the Dielectrics loop further down both need "this chiplet's own bottommost
    # Dielectric", and both must agree on which one that is
    ordered_dielectrics = sorted(chip_dielectrics.dielectrics, key=lambda d: d.zmin)

    # ---- connection stack: bridging Dielectric + one real via Layer per bump/pillar layer ----
    connection_id = die.get("connection")
    current_reference = attach_point
    current_edge = "Top"

    if connection_id:
      stack_layers = _resolve_connection_stack(interconnect_methods, connection_id)
      total_height = sum(layer["height"] for layer in stack_layers)
      if total_height > 0:
        boundary_layer = boundary_layer_map.get(component_id)
        if boundary_layer is None:
          raise ComposeError(
              f'Component "{component_id}" needs a bridging Dielectric (connection: '
              f'"{connection_id}" has nonzero height) but has no --boundary-layer mapping for '
              f'its Boundary= GDS layer number')
        _ensure_connection_material(base_materials_el, renamer, connection_materials,
                                     added_connection_material_names, bondline_material,
                                     f'component "{component_id}"\'s bridging Dielectric')

        bondline_name = _unique_name(renamer.dielectric_names, f"{component_id}_bondline")
        renamer.dielectric_names.add(bondline_name)
        bondline_el = ET.SubElement(base_dielectrics_el, "Dielectric")
        bondline_el.set("Name", bondline_name)
        bondline_el.set("Material", bondline_material)
        bondline_el.set("Reference", attach_point)
        bondline_el.set("ReferenceEdge", "Top")
        bondline_el.set("Thickness", f"{total_height:.4f}")
        bondline_el.set("Boundary", str(int(boundary_layer)))

        # A connection_stack's declared total height is a pad-to-pad bump/pillar height, not a
        # dielectric-surface-to-dielectric-surface gap - the via/pillar metal itself must reach
        # the real conductor surface on each side, not just span the bondline's own nominal
        # extent. Any passivation (or other dielectric) sitting between --attach's own Dielectric
        # edge and the real pad metal is bridged by extending the via chain's own two outer ends
        # past the bondline's own Bottom/Top - overlapping into the neighboring Dielectric on
        # each side, same as any other via/metal embedded inside its surrounding Dielectric (see
        # find_z_overlaps(), which only flags two overlapping DIELECTRICS, never a Layer
        # overlapping one - a via reaching past its own bondline into the next Dielectric over is
        # exactly as normal as iTopMetal2 sitting embedded inside iSiO2). The bondline Dielectric
        # itself, and the die's own first Dielectric right above it, are deliberately left
        # untouched - shifting either of *those* would create exactly the Dielectric-vs-Dielectric
        # overlap find_z_overlaps() exists to catch. See
        # HOW_IT_WORKS.md#connection-stack-pad-to-pad-anchoring.
        attach_dielectric = base_dielectrics.get_by_name(attach_point)
        if attach_dielectric is None:
          raise ComposeError(
              f'Component "{component_id}"\'s --attach target "{attach_point}" is not a '
              f'Dielectric in the interposer\'s own stackup')
        interposer_top_metal = _topmost_metal(base_metals, 'the interposer')
        chip_bottom_metal = _bottommost_metal(
            chip_metals, f'component "{component_id}"\'s own stackup')
        gap_below_attach = attach_dielectric.zmax - interposer_top_metal.zmax
        metal_offset_in_die = chip_bottom_metal.zmin - ordered_dielectrics[0].zmin

        # each via/pillar layer is embedded WITHIN the bondline dielectric's own z-range, same
        # convention as e.g. interposer_SG13G2.xml's own TopMetal2, embedded in SiO2 via
        # Reference="SiO2" ReferenceEdge="Bottom" - except the very first layer's own Zmin and
        # the very last layer's own Zmax are pulled past the bondline's own Bottom/Top by
        # gap_below_attach/metal_offset_in_die respectively, to reach the real pad metal on each
        # side. Referenced off the bondline's own Bottom edge rather than off attach_point
        # directly - numerically identical (the bondline's own zmin IS attach_point's zmax), but
        # this keeps the via's Reference chain inside the chiplet's own subtree, so
        # detect_chiplet_groups() scopes it to this chiplet instead of unconditionally treating
        # it as interposer (see that function's docstring: a Layer referencing an interposer
        # Dielectric directly is always interposer-scoped, regardless of where it physically sits)
        via_offset_zmin = 0.0
        for i, layer in enumerate(stack_layers):
          _ensure_connection_material(
              base_materials_el, renamer, connection_materials, added_connection_material_names,
              layer["material"],
              f'connection_stack layer "{layer["name"]}" (method "{connection_id}")')
          via_name = _unique_name(renamer.layer_names, f"{component_id}_{layer['name']}")
          renamer.layer_names.add(via_name)
          via_zmin = via_offset_zmin - (gap_below_attach if i == 0 else 0.0)
          via_offset_zmin += layer["height"]
          via_zmax = via_offset_zmin + (metal_offset_in_die if i == len(stack_layers) - 1 else 0.0)
          via_el = ET.SubElement(base_layers_el, "Layer")
          via_el.set("Name", via_name)
          via_el.set("Type", "VIA")
          via_el.set("Material", layer["material"])
          via_el.set("Layer", str(layer["gds_layer"]))
          via_el.set("Reference", bondline_name)
          via_el.set("ReferenceEdge", "Bottom")
          via_el.set("Zmin", f"{via_zmin:.4f}")
          via_el.set("Zmax", f"{via_zmax:.4f}")

        current_reference = bondline_name
        current_edge = "Top"

    # ---- Materials: copy every Material this chiplet defines, renaming on collision ----
    chip_materials_el = chip_root.find("./Materials")
    if chip_materials_el is not None:
      for mat_el in chip_materials_el:
        old_name = mat_el.get("Name")
        new_name = renamer.rename_material(old_name)
        new_el = copy.deepcopy(mat_el)
        new_el.set("Name", new_name)
        base_materials_el.append(new_el)

    # ---- Dielectrics: Reference-chained onto the bondline (or attach point directly, for
    #      connection: "") bottom-to-top by resolved zmin - a fresh chain, matching
    #      merge_chiplet_stackup.py's own approach, rather than trying to preserve/rewrite the
    #      chiplet's original Reference graph, which strip_outer_air_dielectrics()/
    #      reverse_stackup_z_order() only maintain at the resolved-object level ----
    dielectric_new_names = {}
    for dielectric in ordered_dielectrics:
      new_name = renamer.rename_dielectric(dielectric.name)
      dielectric_new_names[dielectric.name] = new_name
      el = ET.SubElement(base_dielectrics_el, "Dielectric")
      el.set("Name", new_name)
      el.set("Material", renamer.material_lookup(dielectric.material))
      el.set("Reference", current_reference)
      el.set("ReferenceEdge", current_edge)
      el.set("Thickness", f"{dielectric.thickness:.4f}")
      if dielectric.gdsboundary is not None:
        el.set("Boundary", str(int(dielectric.gdsboundary) + gds_offset))
      current_reference = new_name
      current_edge = "Top"

    # ---- Layers: Reference-chained by resolved zmin, same reasoning as Dielectrics - a Layer
    #      with no Reference at all is unconditionally interposer-scoped (see
    #      detect_chiplet_groups()), so this must never fall back to absolute Zmin/Zmax ----
    ordered_metals = sorted(chip_metals.metals, key=lambda m: m.zmin)
    metal_reference = None
    metal_reference_base = None  # the z-value resolve() will use as `base`, for whichever
                                  # ReferenceEdge was just set below (Bottom -> zmin, Top -> zmax)
    for metal in ordered_metals:
      new_name = renamer.rename_layer(metal.name)
      el = ET.SubElement(base_layers_el, "Layer")
      el.set("Name", new_name)
      el.set("Type", metal.type)
      el.set("Material", renamer.material_lookup(metal.material))
      el.set("Layer", str(int(metal.layernum) + gds_offset))
      if metal_reference is None:
        # first metal in this chiplet (by zmin) - anchor it to whichever Dielectric its zmin
        # actually falls inside, so it starts at the right height rather than always at the
        # bottom of the chiplet's own stack
        owner = next((d for d in ordered_dielectrics if d.zmin - 1e-6 <= metal.zmin < d.zmax + 1e-6), None)
        if owner is None:
          owner = ordered_dielectrics[0]
        el.set("Reference", dielectric_new_names[owner.name])
        el.set("ReferenceEdge", "Bottom")
        base = owner.zmin
      else:
        el.set("Reference", metal_reference)
        el.set("ReferenceEdge", "Top")
        base = metal_reference_base  # previous metal's own zmax, since its ReferenceEdge is Top
      el.set("Zmin", f"{metal.zmin - base:.4f}")
      el.set("Zmax", f"{metal.zmax - base:.4f}")
      metal_reference = new_name
      metal_reference_base = metal.zmax

    # ---- DerivedLayers: target Layer= and every Operand Layer= shifted by this chiplet's own
    #      gds_offset, same convention as merge_chiplet_stackup.py ----
    chip_derived_el = chip_root.find("./ELayers/DerivedLayers")
    if chip_derived_el is not None:
      base_derived_el = base_root.find("./ELayers/DerivedLayers")
      if base_derived_el is None:
        base_derived_el = ET.SubElement(base_root.find("./ELayers"), "DerivedLayers")
      for dl_el in chip_derived_el:
        new_el = copy.deepcopy(dl_el)
        new_el.set("Name", renamer.rename_layer(new_el.get("Name")))
        new_el.set("Layer", str(int(new_el.get("Layer")) + gds_offset))
        for operand_el in new_el.findall("Operand"):
          operand_el.set("Layer", str(int(operand_el.get("Layer")) + gds_offset))
        base_derived_el.append(new_el)

  return base_tree


def _parse_kv (arg, what):
  if "=" not in arg:
    raise argparse.ArgumentTypeError(f'expected {what} in the form KEY=VALUE, got "{arg}"')
  key, value = arg.split("=", 1)
  return key, value


def _kv_action (what):
  class _Action (argparse.Action):
    def __call__ (self, parser, namespace, values, option_string=None):
      key, value = _parse_kv(values, what)
      getattr(namespace, self.dest)[key] = value
  return _Action


def main (argv=None):
  parser = argparse.ArgumentParser(
      description="Stitch several per-chiplet stackup XML files into one combined stackup "
                  "XML, driven by a .chiplet assembly file (see "
                  "https://github.com/IHP-GmbH/chiplet-spec).")
  parser.add_argument("--chiplet-file", required=True, help="path to the .chiplet assembly file")
  parser.add_argument("--stackup", metavar="TECH=PATH", action=_kv_action("TECH=PATH"),
                       default={}, dest="stackup_map",
                       help="technology id -> our own stackup XML path, repeatable")
  parser.add_argument("--attach", metavar="COMPONENT_ID=DIELECTRIC", action=_kv_action("COMPONENT_ID=DIELECTRIC"),
                       default={}, dest="attach_map",
                       help="component id -> Dielectric name in the interposer stackup it "
                            "attaches to, repeatable")
  parser.add_argument("--boundary-layer", metavar="COMPONENT_ID=GDSLAYER",
                       action=_kv_action("COMPONENT_ID=GDSLAYER"), default={}, dest="boundary_layer_map",
                       help="component id -> GDS layer number for its bridging Dielectric's "
                            "Boundary=, repeatable")
  parser.add_argument("--interconnect-methods", default=None,
                       help="path to interconnect_methods.json - required if any component "
                            "declares connection:")
  parser.add_argument("--connection-materials", default=None,
                       help="path to a --connection-materials sidecar JSON file (see "
                            "test_data/connection_materials.json) - required under the same "
                            "condition as --interconnect-methods; supplies electrical/thermal "
                            "properties for connection_stack layer materials and for "
                            "--bondline-material, since neither the .chiplet file nor "
                            "interconnect_methods.json carries these")
  parser.add_argument("--bondline-material", default="Underfill",
                       help='Material name for each bridging Dielectric, looked up in '
                            '--connection-materials (default: "Underfill")')
  parser.add_argument("-o", "--output", required=True, help="path to write the combined stackup XML to")
  args = parser.parse_args(argv)

  print("chiplet_xml_composer", __version__)
  print("  chiplet file:         ", args.chiplet_file)
  print("  stackup map:          ", args.stackup_map)
  print("  attach map:           ", args.attach_map)
  print("  boundary layer map:   ", args.boundary_layer_map)
  print("  interconnect methods: ", args.interconnect_methods)
  print("  connection materials: ", args.connection_materials)
  print("  bondline material:    ", args.bondline_material)
  print("  output:               ", args.output)

  try:
    boundary_layer_map = {k: int(v) for k, v in args.boundary_layer_map.items()}
    tree = compose(
        chiplet_path=args.chiplet_file,
        stackup_map=args.stackup_map,
        interconnect_methods_path=args.interconnect_methods,
        connection_materials_path=args.connection_materials,
        attach_map=args.attach_map,
        boundary_layer_map=boundary_layer_map,
        bondline_material=args.bondline_material,
    )
  except ComposeError as e:
    print(f"ERROR: {e}")
    return 1

  ET.indent(tree, space="  ")
  tree.write(args.output, encoding="UTF-8", xml_declaration=True)
  print("wrote", args.output)

  # verify the result the same way the Stackup Editor's own checks would
  materials_list, dielectrics_list, metals_list = stackup_reader.read_substrate(args.output)
  chiplet_groups = dielectrics_list.chiplet_groups
  print(f"  detected {len(chiplet_groups.chiplets)} chiplet(s): "
        f"{[c.id for c in chiplet_groups.chiplets]}")
  overlaps = dielectrics_list.find_z_overlaps()
  missing_boundaries = dielectrics_list.find_missing_chiplet_boundary_warnings()
  for warning in overlaps + missing_boundaries:
    print("  WARNING:", warning)
  if not overlaps and not missing_boundaries:
    print("  no z-overlaps, no missing chiplet boundaries")

  return 0


if __name__ == "__main__":
  sys.exit(main())
