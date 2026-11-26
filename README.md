# chiplet_xml_composer

Stitches several per-chiplet [gds2palace](https://github.com/VolkerMuehlhaus/gds2palace_ihp_sg13g2)
stackup XML files into one combined stackup XML, driven by an IHP
[`.chiplet`](https://github.com/IHP-GmbH/chiplet-system-spec) assembly file.

The `.chiplet` format and its Python reader (`chiplet-format-io`) are maintained in
[IHP-GmbH/chiplet-system-spec](https://github.com/IHP-GmbH/chiplet-system-spec), as part of the
IHP Open Assembly Design Kit ([documentation](https://ihp-open-adk-docs.readthedocs.io/en/latest/formats.html);
the ADK's assembly checks are in [IHP-GmbH/IHP-Open-ADK](https://github.com/IHP-GmbH/IHP-Open-ADK)).

Scope (v1): Z-stack merge only - it produces one combined stackup XML for gds2palace/openEMS
simulation, not a merged GDSII layout.

## Documentation

- [`doc/USAGE.md`](doc/USAGE.md) - command-line reference, worked examples, variable overrides,
  matching the layout's GDS layer numbers, Python API, error messages.
- [`doc/CHIPLET_FORMAT.md`](doc/CHIPLET_FORMAT.md) - exactly which `.chiplet`/
  `interconnect_methods.json`/`connection_materials.json` fields are read, which are ignored, and
  what's missing from all three that has to come from the command line instead.
- [`doc/HOW_IT_WORKS.md`](doc/HOW_IT_WORKS.md) - the merge algorithm itself (attachment via a
  bridging Dielectric + connection-stack vias, flip-chip z-reversal, outer-AIR stripping,
  name/GDS-layer collision avoidance across chiplets), plus known v1 limitations.

Each of the three has two parts, addressed separately: "For human readers" (narrative) and "For
AI agents" (compact, structured reference for constructing an invocation or tracing behavior
programmatically).

## Examples

[`examples/`](examples/) has three worked examples, built on the real stackups in
[`test_data/`](test_data/) (`interposer_IntM4TM2.xml`, an IHP interposer technology, and
`SG13G2_die.xml`, an IHP SG13G2 die). Each example folder has its own `.chiplet` input, its own
already-generated `combined.xml` output, and a `combined_preview.png` stackup cross-section
image, so you can inspect a real result without running anything yourself:

- [`examples/01_direct_bond/`](examples/01_direct_bond/) - a die bonded directly to the
  interposer, no bump/pillar stack.
- [`examples/02_flip_chip_cupillar/`](examples/02_flip_chip_cupillar/) - a die flip-chip mounted
  through a real two-layer Cu-pillar/SnAg-cap bump stack.
- [`examples/03_two_dies/`](examples/03_two_dies/) - two dies of the identical technology on one
  interposer, demonstrating automatic name/GDS-layer collision avoidance.

See [`doc/USAGE.md`](doc/USAGE.md) for the full walkthrough of each one.

## Install (development)

```bash
pip install -e .
```

Depends on `gds2palace` (0.13.0 or newer) and `chiplet-format-io` (the latter installed from a pinned commit of
[IHP-GmbH/chiplet-system-spec](https://github.com/IHP-GmbH/chiplet-system-spec), since it isn't on PyPI yet).

## Tests

```bash
python -m pytest
```

Needs `gds2palace` and `chiplet-format-io` (as installed above); about a second.

## Usage

```bash
chiplet_xml_composer --chiplet-file assembly.chiplet \
    --stackup tech_a=stackup_a.xml --stackup tech_b=stackup_b.xml \
    --attach die1=Passive --attach die2=Passive \
    --interconnect-methods interconnect_methods.json \
    --connection-materials connection_materials.json \
    --boundary-layer die1=235 --boundary-layer die2=236 \
    -o combined.xml
```

Or from Python:

```python
from chiplet_xml_composer import compose

tree = compose(
    chiplet_path="assembly.chiplet",
    stackup_map={"tech_a": "stackup_a.xml"},
    attach_map={"die1": "Passive"},
)
```
