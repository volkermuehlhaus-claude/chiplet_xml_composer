# What's read from `.chiplet` and `interconnect_methods.json`

`chiplet_xml_composer` reads only a narrow slice of the [`.chiplet` format](https://github.com/IHP-GmbH/chiplet-spec).
It also reads a separate `interconnect_methods.json` registry.

This document lists exactly what the tool reads from each file. It also lists what the tool
ignores. Some information is missing from both files entirely. For that information, you must
use command-line flags instead.

See [`USAGE.md`](USAGE.md) for how to supply that missing information. See
[`HOW_IT_WORKS.md`](HOW_IT_WORKS.md) for what the tool does with what it reads.

This document has two parts. The first is for human readers. It explains why each field is or
isn't read. The second is for AI agents. It is a flat, field-by-field table. Check a `.chiplet`
file against it before constructing an invocation.

---

## For human readers

### What `.chiplet` actually is

The `.chiplet` format is a 3D assembly placement format. It describes where each die, interposer,
and substrate sits in a shared x/y/z frame. It describes how each die is mounted: its orientation
and interconnect method. It also names which technology and GDS layout each component uses.

The `.chiplet` format does not describe a die's own dielectric/metal z-stack. A gds2palace
stackup XML does that instead. Merging those stackup files is exactly what this tool does.

`chiplet_xml_composer`'s v1 scope is narrower than the whole `.chiplet` spec. It is a Z-stack
merge only tool. It never reads or writes GDSII geometry.

Because of that, every field about where something sits in x/y is out of scope. The tool
ignores:

- `position`
- `rotation`
- `anchor`
- `dimensions.width`
- `dimensions.height`

Your `.chiplet` file can still include these fields. The tool just ignores them.

### Read

| `.chiplet` field | Used for |
|---|---|
| `components[].id` | Key for `--attach`/`--boundary-layer` mappings, and for auto-generated names (`<id>_bondline`, `<id>_<layer-name>`). |
| `components[].type` | Selecting the one `interposer` component (the merge base) and every `die`/`die_array` component. See [Not read](#not-read-out-of-scope-for-v1) for `substrate`. |
| `components[].technology` | Key into `--stackup` to find that component's own stackup XML. |
| `components[].orientation` | `flip_chip` triggers outer-AIR stripping and z-mirroring. See [`HOW_IT_WORKS.md`](HOW_IT_WORKS.md#flip-chip-z-reversal). Any other value, including absent, is treated as a direct, unflipped attach. |
| `components[].connection` | Key into `interconnect_methods.json`'s `methods` map. Builds a bridging Dielectric and via Layer(s) if the resolved stack has nonzero height. Absent or empty means a direct bond, with no bridging layer at all. |

| `interconnect_methods.json` field | Used for |
|---|---|
| `methods.<connection_id>.connection_stack.layers[].name` | Looked up in `layer_registry` for a GDS layer number. |
| `methods.<connection_id>.connection_stack.layers[].material` | The via Layer's `Material=`. Must already be defined as a `<Material>` in an input stackup XML. |
| `methods.<connection_id>.connection_stack.layers[].height` | Summed into the bridging Dielectric's own `Thickness=`. Also sets each layer's own z-extent within it. |
| `layer_registry.<name>.gds_layer` | The via Layer's `Layer=` (GDS layer number). |

### Not read (out of scope for v1)

- `position`, `rotation`, `anchor`, `dimensions`. No XY placement or GDS assembly happens here.
  See the module docstring in `composer.py` for the scope statement.
- `technologies.<id>.stackup`. This is a path to a YAML layer-stackup file in IHP's own
  ecosystem. It uses a different schema than a gds2palace stackup XML. Use `--stackup` instead.
  That flag points at your own XML file for that technology id.
- `connection_stacks`. This is the `.chiplet` file's own optional, inlined version of a
  connection stack. Even when a `.chiplet` file self-contains this block, it never carries a GDS
  layer number. See the next section. `interconnect_methods.json` is the only source this tool
  reads for that.
- `interfaces[]`, `io_pads[]`, `netlist`, `flow`, `interposer.adapter`, `interconnect.adapter`.
  None of these affect a stackup merge.
- `type: substrate` components. These are silently skipped. This is not an error. Nothing in a
  gds2palace stackup XML models a package substrate or carrier separately from the interposer
  today.
- `type: die_array`. Treated exactly like a single `die`. One instance of that chiplet's own
  stackup is merged once. v1 has no XY placement to multiply an array across. Array `count` and
  `pitch` are not read.

### Not read: missing information

Two pieces of information are missing from both files entirely. That is why `--attach` and
`--boundary-layer` exist as command-line flags. Nothing derives them automatically.

- **Which Dielectric a die attaches to.** A `.chiplet` file has no field for this, within v1's
  scope. This would ordinarily fall out of x/y/z placement. This tool does not process
  placement. You must supply this via `--attach`.
- **The bridging Dielectric's own `Boundary=` GDS layer.** `interconnect_methods.json`'s
  `layer_registry` gives a GDS layer for each via/pillar metal layer. Nothing gives one for the
  dielectric that surrounds them, the bondline or underfill material. None of these name it:

  - The `.chiplet` file itself
  - `connection_stacks`
  - `interconnect_methods.json`'s `fab_anchors` block

  `fab_anchors` entries are UBM/passivation-opening markers on the interposer's own side. They
  are not an outline for the bonding material.

  You must supply this GDS layer via `--boundary-layer`.

---

## For AI agents

Flat reference: for every field below, "yes" means `chiplet_xml_composer` reads it, "no" means
it's parsed by `chiplet_format_io`/`json.load` but never used, and "n/a" means it's structurally
irrelevant here (e.g. a field of a block type that's skipped entirely).

### `.chiplet` fields

| Field | Read? | Notes |
|---|---|---|
| `format_version` | indirectly | Validated by `chiplet_format_io.load()` before this tool sees the document; not inspected again here. |
| `assembly.*` | no | Purely descriptive; never consulted. |
| `technologies.<id>.description` | no | |
| `technologies.<id>.layer_properties` | no | |
| `technologies.<id>.stackup` | **no - common mistake** | This is a YAML path in a different schema. Do not pass it as `--stackup`; find or ask for an XML stackup file instead. |
| `technologies.<id>.dbu` | no | |
| `components[].id` | yes | Required; used as the key for `--attach`/`--boundary-layer` and for generated element names. |
| `components[].type` | yes | Only `"interposer"` (exactly one required) and `"die"`/`"die_array"` are acted on. `"substrate"` and anything else is silently skipped. |
| `components[].technology` | yes | Key into `--stackup`. If absent or not in the `--stackup` map, `compose()` raises `ComposeError` naming it. |
| `components[].anchor` | no | |
| `components[].layout`, `top_cell`, `cells` | no | GDS references; irrelevant to a Z-stack-only merge. |
| `components[].position` | no | |
| `components[].rotation` | no | |
| `components[].orientation` | yes | Only the literal string `"flip_chip"` triggers z-reversal. Any other value, or absent, is treated as face-up (no reversal). The canonical vocabulary is `face_up`/`flip_chip`/`face_down`, but this tool only branches on `"flip_chip"` specifically - do not rely on it rejecting an unrecognized value. |
| `components[].connection` | yes | Falsy (absent, `None`, `""`) means "no bridging layer, direct bond." A non-empty value must resolve in `interconnect_methods.json`. |
| `components[].dimensions` | no | |
| `components[].attachment_surface_z` | no | |
| `components[].io_pads` | no | |
| `components[].metadata` | no | |
| `components[].array` | no | Even for `type: die_array`; the array is not expanded. |
| `connection_stacks` (top-level) | no | Never read, inlined or not - see the human section above for why. |
| `interposer`, `interconnect` (top-level adapter blocks) | no | |
| `interfaces[]` | no | |
| `netlist` | no | |
| `flow` | no | |
| `_metadata` | indirectly | An intermediate file (`finalize_required: true`) is refused by `chiplet_format_io.load()` itself, before this tool runs. |

### `interconnect_methods.json` fields

This file's own top-level schema (`schema_version`, `pdk`, `description`, `default_method`,
`default_connection_library`, `fab_anchors`) is not defined by `.chiplet` itself - it's a
separate registry this tool expects to find `methods` and `layer_registry` maps in, in the exact
shape shown in [`USAGE.md`](USAGE.md#2-flip-chip-die-with-a-cu-pillar-connection-stack).

| Field | Read? | Notes |
|---|---|---|
| `methods.<id>.connection_stack.layers[].name` | yes | Looked up in `layer_registry`; `ComposeError` if missing there. |
| `methods.<id>.connection_stack.layers[].material` | yes | Must resolve to an existing `<Material>` in some `--stackup` input, or `ComposeError`. |
| `methods.<id>.connection_stack.layers[].height` | yes | Summed for the bondline `Thickness=`; individual layer z-extents. |
| `methods.<id>.connection_stack.layers[].diameter` | no | Read into the internal per-layer dict but not used - v1 draws a full-width via Layer with a `Boundary=`-restricted footprint, not a real per-bump-diameter shape. |
| `layer_registry.<name>.gds_layer` | yes | Becomes the via Layer's `Layer=`. |
| `layer_registry.<name>.gds_datatype` | no | Read into the internal dict but never written anywhere - the gds2palace stackup XML `<Layer>` element has no datatype field at all. |
| `layer_registry.<name>.material`, `.purpose` | no | The *method's* own `layers[].material` is used instead, not the registry entry's. |
| Anything else (`fab_anchors`, `default_method`, `pitch_rules`, `fab_params`, `adapter`, ...) | no | |

### Checklist before invoking, for an agent parsing a `.chiplet` file cold

1. `len([c for c in components if c["type"] == "interposer"]) == 1` - else this file can't be
   composed with v1 at all.
2. For each `technology` value seen across all components: do you have an XML stackup file for
   it? (Never `technologies.<id>.stackup` - see above.)
3. For each `die`/`die_array` component: do you have an attachment Dielectric name for it? (Not
   in the file - external knowledge or a question back to the requester.)
4. For each `die`/`die_array` component with a non-empty `connection`: do you have
   `interconnect_methods.json`, does its `methods` map contain that id, does every one of that
   method's `connection_stack.layers[].name` appear in `layer_registry` with a `gds_layer`, and
   do you have a `--boundary-layer` value for that component? (The last one is never in either
   file - external knowledge or a question back to the requester.)
5. For each connection_stack layer's `material`, and for `--bondline-material` (default
   `"Underfill"`): does it exist as a `<Material Name="...">` in at least one `--stackup` XML?
