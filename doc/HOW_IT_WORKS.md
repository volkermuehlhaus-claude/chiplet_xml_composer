# How the merge works

This document explains what `compose()` actually does. It takes a `.chiplet` file plus a set of
per-chiplet stackup XML files. It produces one combined stackup XML.

This document covers the internal algorithm. See [`CHIPLET_FORMAT.md`](CHIPLET_FORMAT.md) for
the inputs and outputs. See [`USAGE.md`](USAGE.md) for how to invoke the tool.

This document has two parts. The first is for human readers. It explains the physical reasoning
behind each step. The second is for AI agents. It gives the same steps as a terse, ordered
algorithm. Use it when debugging an unexpected output or extending the tool.

---

## For human readers

### Pipeline, one die at a time

`compose()` parses the interposer's stackup XML once. It then processes each die component in
turn. For each die, it appends new `<Dielectric>`, `<Layer>`, and `<Material>` elements directly
into the interposer's own tree.

Before the first die, the interposer's own outer `AIR` Dielectric is removed, by the same rule as
a die's in step 2 below. A standalone interposer stackup has air above its passivation, on the
very Dielectric the dies attach to. Kept, it would sit beside every die as a second branch on
that Dielectric, be detected as a chiplet of its own, and overlap the dies. The air around the
combined model comes from gds2palace's `air_around` setting instead. An `AIR` Dielectric that
another element references is kept.

Here is what happens for each die:

1. **Parse the die's own stackup XML.** This uses `gds2palace.stackup_reader.parse_substrate()`,
   the same fully-resolving parse used everywhere else in gds2palace. By the time the next steps
   run, everything is already reduced to plain `zmin`/`zmax`/`thickness` floats. This includes
   `"="`-expressions, Reference/ReferenceEdge chains, and absolute positions.

2. **Strip the die's own outer AIR**, using `strip_outer_air_dielectrics()`. A standalone die's
   stackup XML conventionally has an `AIR` dielectric. It represents open space above or below
   the die's own physical body. Once the die is bonded to something else at that surface, that
   space is meaningless.

   This step only checks the stack's own topmost and bottommost Dielectric. It uses the
   resolved z, through the `is_top`/`is_bottom` flags the parse already set. It only removes a
   Dielectric if its Material resolves to the built-in `AIR` material.

   Removing it never shifts anything else. AIR is empty space. Nothing is positioned inside it
   that needs to move.

3. **Reverse the z-order for `orientation: flip_chip`**, using `reverse_stackup_z_order()`. A
   die mounted face-down needs its own internal z-stack mirrored. What was nearest its top
   surface, near the bond pads, now faces the interposer. What was nearest its bottom, the bulk
   silicon, ends up farthest away.

   The `.chiplet` format itself never specifies this. Per `coord_frame_contract.md` section
   2.4, `orientation: flip_chip` only defines an X-mirror of the GDS artwork plus a z-mounting
   offset. Reversing the die's own internal dielectric build order is a physical necessity.
   This tool has to handle it itself.

   The pivot is this stack's own current topmost surface, after step 2. If that die had an
   outer AIR dielectric, step 2 already removed it, so this is whichever real dielectric used to
   sit right under it. If the die never had one, this is simply that die's own real outermost
   dielectric - the same surface either way. An outer AIR dielectric was never required to
   exist for this to work; step 2 only removes one *if* present. Mirroring `[zmin, zmax]` for
   every Dielectric and every Layer around that one z-value preserves every element's own
   thickness exactly. It also flips which end is "up."

4. **Build the bridging Dielectric and via Layer(s)**, if `connection:` is set and resolves to a
   nonzero-height stack.

   One `<Dielectric>` is created, called the bondline. It is `Reference`d onto the `--attach`
   target's Top edge. It ends where the die's body starts, at the interposer's top metal plus
   the connection stack's total height (the `.chiplet` z-mounting rule), so it is thinner than
   the stack by the interposer's dielectric above that metal. It carries
   `Boundary=` from `--boundary-layer`. Its `Material=` is `--bondline-material` (default
   `"Underfill"`).

   One `<Layer Type="VIA">` is created per `connection_stack` entry. Each one sits on its own
   real GDS layer, from `interconnect_methods.json`'s `layer_registry`. Each one is embedded
   within the bondline's own z-range, not stacked on top of it. `interposer_IntM4TM2.xml`'s own
   `Metal4` works the same way: it sits embedded inside `SiO2`, using `Reference="SiO2"
   ReferenceEdge="Bottom"`. A bump or pillar physically sits inside its surrounding underfill,
   not above it.

   The via chain starts on the interposer's top metal, below the bondline's own Bottom, and its
   last layer reaches past the bondline's Top into the die, up to the die's own bottom metal, so
   the bump touches real pad metal on both sides. See
   [Connection-stack z-mounting](#connection-stack-z-mounting).

   Neither the bondline's material nor any via layer's material can come from a `--stackup`
   input file. A bump or pillar material, and the material bonding it in place, aren't
   physically part of either the interposer's or the die's own stackup - so this tool never
   looks for them there. It resolves both from `--connection-materials` instead, synthesizing a
   new `<Material>` element the first time each name is needed in this `compose()` run. See
   [Connection-material sourcing and deduplication](#connection-material-sourcing-and-deduplication).

   Each via is `Reference`d off the bondline's own Bottom edge. Its `Zmin=`/`Zmax=` are no
   longer simply `0` and the bondline's own thickness, though - see the next section. Going
   through the bondline (rather than referencing the attach point directly) keeps the via's
   Reference chain inside the chiplet's own subtree. See
   [Chiplet detection: branch points, not just attachments](#chiplet-detection-branch-points-not-just-attachments)
   for why that distinction matters.

   If `connection:` is absent, or resolves to zero total height, no bridging Dielectric is
   created at all. The die's own dielectric chain attaches directly to the `--attach` target.

5. **Reference-chain the die's own Dielectrics and Layers**, onto the bondline or onto the
   attach point directly. They are sorted bottom-to-top by resolved `zmin`.

   This is a fresh chain. It is built from each element's resolved name, material, and
   thickness. It does not try to preserve or translate the die's own original Reference graph.
   Steps 2 and 3 only maintain that graph at the resolved-object level anyway. See
   [Why a fresh chain, not a rewritten one](#why-a-fresh-chain-not-a-rewritten-one).

   A Layer is always Reference-chained. It never gets an absolute position. A Layer with no
   Reference at all is unconditionally treated as belonging to the shared interposer, by
   `detect_chiplet_groups()`. That would silently defeat chiplet-scoping for every metal in
   the die.

6. **Rename on collision, and shift GDS layer numbers.** Every Material, Dielectric, and Layer
   name this die introduces gets checked. The check is against every name already used, by the
   interposer and by every earlier die. A colliding name gets a unique suffix, for example
   `IMD` becomes `IMD_2`. See
   [Name/GDS-layer collision avoidance](#namegds-layer-collision-avoidance).

   Every native GDS layer number in this die's own stackup XML gets shifted too. The shift is a
   chosen multiple of 1000, picked clear of every layer number already claimed.

   One number is the exception: the connection-stack via's own GDS layer number, from
   `interconnect_methods.json`. This one is never shifted. It is a fixed, shared, PDK-wide
   registry number, not a technology-arbitrary one. Two dies using the same bump method are
   supposed to share it.

7. **Copy DerivedLayers, if any.** Their target `Layer=` and every `Operand Layer=` get shifted
   by the same offset as step 6. A `DerivedLayer` has no z-position of its own. It is a pure
   GDS-plane boolean operation on other layer numbers. Steps 2 and 3 never need to touch it.

Steps 6 and 7 also copy the die's thermal tables and resolve its `=` expressions. See
[Variables and thermal tables](#variables-and-thermal-tables).

After every die is processed, `compose()` returns the mutated interposer tree.

The CLI's `main()` then writes the tree to disk. It re-parses the written file. It prints a
summary, from `find_z_overlaps()`, `find_missing_chiplet_boundary_warnings()`, and
`chiplet_groups`. These are the same checks already shipped in gds2palace's Stackup Editor.

### Flip-chip z-reversal

This is the mirroring formula. It applies to every Dielectric and every Layer, in the die's own
lists, after AIR-stripping.

For a pivot `P`:

- `new_zmin = 2*P - old_zmax`
- `new_zmax = 2*P - old_zmin`

This preserves `thickness` exactly, since `new_zmax - new_zmin` equals `old_zmax - old_zmin`. It
reverses which element ends up nearest `P`.

`P` itself is the resolved z of this stack's own current topmost or bottommost Dielectric, after
step 2's AIR-stripping. The confirmed convention treats this as the die's physical attach or
bond surface - whether or not an outer AIR dielectric ever existed there to strip. See step 3
above.

Mirroring around that surface has a useful side effect. The die's own first remaining
Dielectric lands with its `zmin` exactly at the pivot. In the `SG13G2_die.xml` fixture, that
Dielectric is `Passive`. It is then ready to Reference-chain directly onto the bondline, with
zero extra offset arithmetic.

### Connection-stack z-mounting

The `.chiplet` coordinate-frame contract fixes where a die on a connection stack sits
(`coord_frame_contract.md`, section 3.1):

```
z_die = interposer mount plane + connection.total_height()
```

- **Mount plane:** the interposer's `attachment_surface_z`, the top of its top metal under the
  passivation opening (10.8103 µm above the silicon for IntM4TM2, its TopMetal2 top). The
  stack's first layer starts there.
- **`z_die`:** the die's seating plane, the bottom face of its body, i.e. its outermost
  dielectric once flipped.
- **Total height:** the sum of the stack's layer `height`s from `interconnect_methods.json`, 28 +
  16 = 44 µm for `cupillar_opt1`.

The stack height therefore includes the interposer's own dielectric above its pad: 0.4 µm of
`Passive` and 1.5 µm of `SiO2` above `TopMetal2` in `interposer_IntM4TM2.xml`. `compose()` builds
it that way:

- **Mount plane:** the interposer's own topmost metal, the non-sheet `Layer` with the highest
  resolved `zmax`, among the interposer's own metals only (never one an earlier die added, see
  `base_metals` below). A `Type="sheet"` Layer is never a pad and is skipped.
- **Bondline:** starts on `--attach`'s top and is `total_height` minus the gap between that top
  and the mount plane thick: 44 − 1.8997 = 42.1003 µm in the examples. The die's dielectrics
  chain onto its top, so the die's body starts exactly at mount plane + 44 µm. A stack no higher
  than that gap is a `ComposeError`: the die would sit inside the interposer.
- **Vias:** one per stack layer, each its own `height`, stacked from the mount plane: Cu pillar
  248.1003..276.1003 µm, SnAg cap from there on. The last one also reaches on into the die, by the
  die's own dielectric below its bottommost metal (its pad, after the flip), so it ends on that
  metal: 1.9 µm into the die in the examples. The contract places only the die's body; this
  extension is what makes the bump touch the die's pad.

All vias are `Reference`d to the bondline's own `Bottom` edge, so their chain stays inside the
chiplet's subtree (see [Chiplet detection](#chiplet-detection-branch-points-not-just-attachments)),
with a negative `Zmin=` for the first one. A via overlapping a Dielectric other than its bondline
is normal in this format (the interposer's `TopMetal2` sits inside its `SiO2` the same way):
`find_z_overlaps()` only flags two **Dielectrics** whose resolved z-ranges overlap, and the
bondline and the die's dielectrics never overlap anything. See
[Built-in post-write verification](#built-in-post-write-verification).

A die without a `connection:` (or with a zero-height one) is different: it rests directly on
`--attach`'s top, not on the mount plane, which would put it inside the interposer's
passivation.

`base_metals` - the interposer's own materials/dielectrics/metals - is parsed once, from the
interposer's own stackup XML, before any die is processed. Every die's own connection stack
anchors to that same, original parse, never to a metal an earlier die in a multi-die assembly
(like this repo's example 3) already added - two dies attached to the same interposer Dielectric
both anchor to the same real interposer pad, independent of `.chiplet` component order.

### Name/GDS-layer collision avoidance

A `_NameRenamer` instance tracks three separate name namespaces: Material, Dielectric, and
Layer. The same name can legitimately exist in two different namespaces without colliding. The
renamer also tracks one set of claimed GDS layer numbers, seeded from the interposer's own
stackup XML.

For each die, in order:

- **Choose a fresh GDS-layer offset.** This is the smallest multiple of 1000 that clears every
  number already claimed, by the interposer and by every earlier die. The offset is reserved
  immediately, before moving on, so the next die's own offset search sees it too. Without that
  immediate reservation, two dies of the same technology could independently compute the same
  "first free" offset. They would then collide with each other, while each stayed clear of the
  interposer alone.
- **Rename each Material, Dielectric, and Layer name.** Each name is looked up in a
  per-chiplet rename cache first. This way, the same original name always maps to the same new
  name within one die. That matters because a name gets renamed once, then looked up again
  later for a `Material=` or `Reference=` pointer. If a name isn't in the cache yet, it gets the
  first free variant: `name`, then `name_2`, then `name_3`, and so on. The `_` keeps the counter
  apart from the name: `Metal4_2`, not `Metal42`, which would read like a layer of its own.

### Connection-material sourcing and deduplication

A connection method's materials, and the bondline material, are resolved once per whole
`compose()` run, not once per die. `compose()` keeps a single set of "already synthesized"
material names, shared across every die it processes. The first die that needs `CuPillar`
causes a `<Material Name="CuPillar" .../>` to be built from `--connection-materials` and
appended to the interposer's own `<Materials>` block. A second die using the same connection
method, later in the same run, finds `CuPillar` already in that set and does nothing - it
reuses the existing element instead of creating a second one.

This mirrors how a connection method's GDS layer number is already handled (see
[Name/GDS-layer collision avoidance](#namegds-layer-collision-avoidance)): the via layer number
is a fixed, PDK-wide registry number, so two dies sharing a connection method are meant to share
it, never get their own private offset copy. A connection material is the same kind of
fact - it names one real physical material, and two Cu-pillar bumps in the same assembly use the
literal same material, not two coincidentally-identical ones. Deduplicating them keeps the
output's `<Materials>` block from growing a redundant entry per die and keeps `Name="CuPillar"`
meaning one thing throughout the file.

This is why a same-named `<Material>` already present in a `--stackup` input file is treated as
a collision (`ComposeError`), not a fallback source: allowing it would mean this tool sometimes
takes a connection material's properties from `--connection-materials` and sometimes silently
from whichever die happened to define a same-named `<Material>` first, depending on `.chiplet`
component order. That's exactly the kind of hidden, order-dependent data source this design
exists to rule out - see [`CHIPLET_FORMAT.md`](CHIPLET_FORMAT.md#what-connection_materialsjson-actually-is).

### Variables and thermal tables

**The dies' variables are resolved, not copied.** A die's dielectrics and layers are written
from resolved numbers (step 5), in a new Reference chain. A die variable copied along would not
reach those numbers: changing a die's `total_thickness` in the combined file would resize its
substrate dielectric but leave layers such as its `LBE` and `BACKSIDEGND` where they were. So
every `=` expression in what is copied from a die is resolved with that die's own variables
first. This covers its `<Material>` attributes (e.g. `ThermalConductivity="=k_metal"` becomes
`237`), its thermal table points and its DerivedLayers. The combined file has no die variable and
no expression that points at one: a die's thickness is fixed when the file is composed.

To compose a die with other values, `--variable die1:total_thickness=150` sets the variable in
that die's own parsed stackup tree before anything in it is resolved. Every later step reads
from that one tree - dielectrics, layers, materials, tables, derived layers, the bump anchor and
the flip - so they can't disagree. Each die gets an XML comment after `<Variables>` that lists
the values it was resolved with, overrides marked `(override)`.

**The interposer's variables stay working.** The interposer is the base: its elements are kept
as they are, expressions included, and every die is chained on top of it. Changing an interposer
variable therefore moves the dies with it, as in its own file. In the combined file:

- **Renamed with the component id:** each one becomes `<id>_<name>`, e.g.
  `interposer_total_thickness`, and every expression is updated to match. The combined file holds
  several parts, and a plain `total_thickness` would not say which one it sets. Only the name
  tokens are rewritten; the rest of each expression is kept as written.
- **Unused ones dropped:** a variable that no expression uses any more, directly or through
  another variable, is removed. The interposer's `air_thickness` is the usual case: only its
  outer `AIR` used it, and that is removed (see
  [Pipeline, one die at a time](#pipeline-one-die-at-a-time)). With none left, `<Variables>` is
  removed too.

A `--variable interposer:total_thickness=300` sets the value before the merge, and a script can
still override it when it reads the combined file, e.g.
`stackup_reader.read_substrate(path, variable_overrides={'interposer_total_thickness': 300})`.

**Thickness check.** `dimensions.thickness` in the `.chiplet` file is a component's body, Si +
BEOL, in µm (`0` = no data). When it is set, it is compared with the stackup's resolved
dielectrics without AIR, after overrides; a difference of more than 1 nm is a warning. It is
never used as a value: the stackup variable that holds the thickness has no fixed name across
technologies.

A die's thermal tables (`<Tables>/<Table>`, used by `ThermalConductivityTable=`) are copied
into the combined file's `<Tables>`:

- **Same table already there:** a table with the same points under the same name, or under a
  renamed copy of it (`Name_2`, ...), is shared. Two dies of the same technology need the silicon
  table only once.
- **Name taken by a different table:** the die's table gets a new name (`Name_2`, ...), the same
  rule as for the other names, and the die's materials point at the new name.

The interposer's own tables stay as they are.

### Why a fresh chain, not a rewritten one

An earlier, hand-written prototype tried a different approach:
`gds2palace_ihp_sg13g2/test_data/chiplet/merge_chiplet_stackup.py`. It preserved each
dielectric's original Reference target and rewrote it.

That approach needed just as much bookkeeping as re-deriving the chain from scratch. It also
had more ways to go subtly wrong. An original Reference target might itself have been renamed.
It might no longer exist after AIR-stripping. It might point at the wrong side after
z-reversal.

`parse_substrate()` already gives every element's fully-resolved name, material, and thickness.
This holds regardless of how the element was originally positioned. Building a brand-new
bottom-to-top chain from that resolved data is simpler. It is also strictly more robust. It
does not depend on the original XML's own Reference graph at all.

### Chiplet detection: branch points, not just attachments

`detect_chiplet_groups()`, in `gds2palace.stackup_reader`, defines a "chiplet" in a specific
way. Any Dielectric that is the Reference target of 2 or more other Dielectrics counts as a
chiplet root. So does every one of those referrers' transitive subtree. One referrer alone is
not enough - it's an ordinary attachment, not a branch.

This has a direct, sometimes surprising consequence: attaching a single die to an interposer
Dielectric that nothing else references yet detects **zero chiplets**, even though a die was
just bonded on. This is exactly what happens in this repo's own examples 1 and 2 (see
[`USAGE.md`](USAGE.md)): `die1_bondline` is `Passive`'s only referrer, so `Passive` never
becomes a branch point, and `chiplet_groups.chiplets` comes back empty. This is not a bug -
`detect_chiplet_groups()` is answering "does this stackup branch into multiple chiplets
anywhere," and a single attached die, on its own, doesn't.

Attach a *second* die to the same Dielectric, as example 3 does, and `Passive` gets a second
referrer. It becomes a branch point, and both referrers - `die1_bondline` and `die2_bondline` -
get reported as chiplets.

The same rule can also produce a *third*, unexpected chiplet, if the interposer's own stackup
XML already has some other Dielectric referencing the same attach point before you ever run
this tool. That pre-existing Dielectric becomes a chiplet root too, purely because it now shares
a branch point with your new bondline - not because anything about it changed. The interposer's
own outer `AIR` is the common case, and `compose()` removes it (see
[Pipeline, one die at a time](#pipeline-one-die-at-a-time)). Anything else that your interposer
file Reference-chains onto the Dielectric you pass to `--attach` still becomes a chiplet root.

Either way, chiplet detection only affects the Stackup Editor's preview and chiplet-switcher UI.
It never affects meshing or simulation correctness.

### Built-in post-write verification

This verification happens in the CLI's `main()`, not in `compose()` itself. The Python API just
returns the tree and does nothing else.

`main()` re-parses the file it just wrote. It then prints:

- How many chiplets `detect_chiplet_groups()` found, and their ids
- Every warning from `find_z_overlaps()`. This flags two same-scope dielectrics whose resolved
  z-ranges genuinely overlap.
- Every warning from `find_missing_chiplet_boundary_warnings()`. This flags a chiplet branch
  point or root Dielectric with no `Boundary=`.

Both checks are already shipped, unmodified, in gds2palace's Stackup Editor. They are the same
checks a user would see opening the output there. Printing them immediately means a scripted or
CI use of this tool never needs to open a GUI to know whether the result is clean.

### Known v1 limitations

- No GDSII geometry is read, placed, rotated, or mirrored. `position`, `rotation`, and `anchor`
  are ignored entirely. See [`CHIPLET_FORMAT.md`](CHIPLET_FORMAT.md).
- Exactly one `interposer` component is supported per assembly. There is no die-on-die stacking
  and no support for multiple interposers.
- `type: substrate` is not modeled at all.
- `type: die_array` is treated as a single instance. The array isn't expanded, since there's no
  XY placement to expand it across.
- A connection-stack via is a single `Boundary=`-restricted Layer per bump/pillar type. It is
  not real per-bump-position geometry. `diameter` and pad pitch/positions are not used. This is
  a uniform approximation, not a literal instance-per-bump layout.

---

## For AI agents

Ordered algorithm, one die at a time (matches the narrative above 1:1; use this if you're
tracing an unexpected output back to a specific step):

```
parse interposer stackup XML -> base_root
apply variable_overrides[interposer.id] to base_root's <Variable Value=>   # ComposeError on an
                                                                         # unknown name
(base_materials, base_dielectrics, base_metals) = parse_substrate(base_root)
    # parsed once, before any die is processed - every die's own connection stack anchors to
    # this same original parse, never to a metal an earlier die in this run already added
strip_interposer_outer_air(base_root, base_dielectrics)
    # removes the <Dielectric> elements that are topmost/bottommost, Material AIR, and not the
    # Reference= of any other element
check_body_thickness(interposer, base_dielectrics)   # warning only
clean_interposer_variables(base_root, interposer.id)
    # used = names in every "=" attribute outside <Variables>, closed over the Variables'
    # own "=" Values; drop every other Variable (and <Variables> if empty); rename each kept
    # one to <id>_<name> (first free variant) in its Name= and in every "=" attribute
renamer = NameRenamer(base_root)   # seeds name sets + used GDS layers from base_root
added_connection_material_names = {}   # shared across every die in this compose() run

# topmost_metal(metals)/bottommost_metal(metals), called below:
#   candidates = [m for m in metals.metals if not m.is_sheet]   # Type="sheet" is never a real pad
#   raise ComposeError if candidates is empty
#   return max(candidates, key=zmax) / min(candidates, key=zmin)

# ensure_connection_material(name), called below:
#   if name in added_connection_material_names: return          # already synthesized, reuse it
#   if name in renamer.material_names: raise ComposeError        # collision with a --stackup Material
#   entry = connection_materials["materials"].get(name)
#   if entry is None: raise ComposeError                         # not defined in --connection-materials
#   build <Material Name=name Type=entry.type ...> from entry, append to base_root's <Materials>
#   renamer.material_names.add(name); added_connection_material_names.add(name)

for each die component (in .chiplet components[] order):
    chip_root = parse(stackup_map[die.technology])
    apply variable_overrides[die.id] to chip_root's <Variable Value=>   # before any resolving
    (materials, dielectrics, metals) = parse_substrate(chip_root)

    strip_outer_air_dielectrics(dielectrics)
    check_body_thickness(die, dielectrics)   # warning only
    # removes dielectrics.dielectrics entries where (is_top or is_bottom) and material == "AIR"
    # no-op if none qualify - an outer AIR dielectric was never required to exist, only removed
    # if present, so the flip_chip branch below never checks or depends on this having done
    # anything

    if die.orientation not in (None, "face_up", "flip_chip"): raise ComposeError
    if die.orientation == "flip_chip":
        pivot = max(d.zmax for d in dielectrics.dielectrics)
        reverse_stackup_z_order(dielectrics, metals, pivot)
        # new_zmin = 2*pivot - old_zmax ; new_zmax = 2*pivot - old_zmin, for every
        # dielectric AND every metal; thickness is invariant under this transform

    renamer.start_chiplet(chip_root)                        # computes + reserves gds_offset
    current_ref, current_edge = attach_map[die.id], "Top"

    if die.connection:
        stack = resolve_connection_stack(interconnect_methods, die.connection)
        total_height = sum(layer.height for layer in stack)
        if total_height > 0:
            boundary = boundary_layer_map[die.id]            # KeyError -> ComposeError
            if connection_materials is None:                 # loaded lazily, only here - a
                                                               # connection_stack resolving to
                                                               # zero height never reaches this
                                                               # line and never needs the file
                if connection_materials_path is None: raise ComposeError
                connection_materials = load(connection_materials_path)
            ensure_connection_material(bondline_material)    # see below; ComposeError on collision
                                                               # or missing --connection-materials entry
            # z-mounting per coord_frame_contract.md 3.1: stack starts on the interposer's top
            # metal, die body starts at that metal + total_height - see "Connection-stack
            # z-mounting" above
            attach_dielectric = base_dielectrics.get_by_name(current_ref)  # ComposeError if None
            interposer_top_metal = topmost_metal(base_metals)              # excludes Type="sheet"
            chip_bottom_metal = bottommost_metal(metals)                   # this die's own, post-flip
            gap_below_attach = attach_dielectric.zmax - interposer_top_metal.zmax
            metal_offset_in_die = chip_bottom_metal.zmin - min(dielectrics.dielectrics, key=zmin).zmin
            if total_height - gap_below_attach <= 0: raise ComposeError     # die inside interposer
            bondline = new Dielectric(Reference=current_ref, ReferenceEdge="Top",
                                       Thickness=total_height - gap_below_attach,
                                       Boundary=boundary, Material=bondline_material)

            offset = -gap_below_attach                       # relative to the bondline's Bottom
            for i, layer in enumerate(stack):
                ensure_connection_material(layer.material)   # see below
                zmin = offset
                offset += layer.height
                zmax = offset + (metal_offset_in_die if i == len(stack) - 1 else 0)
                new Layer(Type="VIA", Material=layer.material, Layer=layer.gds_layer,
                          Reference=bondline, ReferenceEdge="Bottom", Zmin=zmin, Zmax=zmax)
            current_ref, current_edge = bondline, "Top"

    chip_variables = resolved <Variables> of chip_root   # never copied to base_root
    insert <!-- die.id: file resolved with its Variables name=value, ... --> after <Variables>
    for each <Table> in chip_root's <Tables>:
        resolve its "=" expressions with chip_variables
        if base_root has a Table with the same points named Name or Name_<digits>: share it
        else: append it to base_root's <Tables> (created if missing), renamed on collision

    copy every <Material> from chip_root -> base_root, renaming on collision, its "="
        expressions resolved with chip_variables, ThermalConductivityTable= set to the table's
        name in base_root

    for dielectric in sorted(dielectrics.dielectrics, key=zmin):        # bottom-to-top
        new Dielectric(Reference=current_ref, ReferenceEdge=current_edge,
                        Thickness=dielectric.thickness,
                        Boundary=dielectric.gdsboundary + gds_offset if set)
        current_ref, current_edge = <this new Dielectric's name>, "Top"

    for metal in sorted(metals.metals, key=zmin):                       # bottom-to-top
        if first metal:
            owner = dielectric whose [zmin, zmax) contains metal.zmin (fallback: lowest one)
            Reference=owner, ReferenceEdge="Bottom", base=owner.zmin
        else:
            Reference=<previous metal>, ReferenceEdge="Top", base=<previous metal>.zmax
        new Layer(Type=metal.type, Material=metal.material, Layer=metal.layernum + gds_offset,
                  Reference=..., ReferenceEdge=..., Zmin=metal.zmin - base, Zmax=metal.zmax - base)

    for each DerivedLayer in chip_root:
        copy it, "=" expressions resolved with chip_variables, Layer += gds_offset, every Operand/Layer += gds_offset, rename on collision

return base_root (as an ElementTree)
```

### Failure points, mapped to the step that raises them

| Step | Raises `ComposeError` when |
|---|---|
| Technology lookup (any component) | `stackup_map` has no entry for `component.technology` |
| Attach lookup (each die) | `attach_map` has no entry for `component.id` |
| `variable_overrides` (before anything else) | a key is not the interposer's or a die's id, a variable name is not in that component's stackup, or a value is not a number |
| Orientation (each die) | `orientation` is neither `face_up` (or absent) nor `flip_chip` |
| `connection:` resolution | `die.connection` not in `interconnect_methods["methods"]`, or a `connection_stack` layer name not in `interconnect_methods["layer_registry"]` |
| Bondline construction | `total_height > 0` and `boundary_layer_map` has no entry for `component.id` |
| Bondline construction | `total_height` is not more than the interposer's dielectric between its top metal and `--attach`'s top (the die would sit inside the interposer) |
| Pad-to-pad anchor lookup | `--attach`'s target isn't a real `<Dielectric>` in the interposer's own stackup, or either `topmost_metal()`/`bottommost_metal()` finds zero non-sheet metals in the interposer's or this die's own stackup |
| Connection-material check (bondline and each via layer) | the named material has no entry in `connection_materials["materials"]`, or it collides with a same-named `<Material>` already present from a `--stackup` input |
| `--connection-materials` requirement (lazy - first time any component's own `connection_stack` resolves to nonzero height) | `--connection-materials` was not given. **Not** raised just because some component declares `connection:` - a zero-height one never reaches this check. |
| Component selection (once, before the loop) | `components[]` has zero or 2+ entries with `type == "interposer"` |

Nothing else in the pipeline raises `ComposeError` - nested XML/YAML/JSON structural problems
surface as whatever exception the underlying parser (`xml.etree.ElementTree`, `chiplet_format_io`,
`json`) raises for malformed input, except `.chiplet` load failures specifically, which
`_load_chiplet_file()` catches and re-raises as `ComposeError`.

### Idempotency and side effects

- `compose()` opens and reads every input file exactly once each; it never writes to them.
- `compose()` never writes to disk - only the caller's own explicit `.write()` (Python API) or
  the CLI's `main()` produces an output file.
- Running the same inputs through `compose()` twice produces byte-identical output (no random
  ordering, no timestamps, no reliance on filesystem iteration order - element order follows
  `.chiplet`'s own `components[]` order, and within a die, resolved-`zmin` order).
