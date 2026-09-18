#!/usr/bin/env python
"""Render a stackup cross-section preview PNG for a gds2palace stackup XML file.

This reuses setupEM's own Stackup Preview layout/paint engine
(setupEM.setup_common.compute_stackup_layout / render_stackup_layout - the exact
functions setupEM.py's/stackupEditor.py's interactive "Stackup Preview" window calls),
painting onto an off-screen QPixmap instead of a shown window. It is not part of the
installed chiplet_xml_composer package - chiplet_xml_composer itself never depends on
setupEM or PySide6. This script is a one-off doc-generation tool: run it manually (in
the shared d:\\venv\\palace environment, which already has gds2palace and setupEM
editable-installed) whenever an example's input or combined XML changes, then check
the resulting PNG in, the same way examples/*/combined.xml is checked in rather than
regenerated at doc-build time.

Deliberately does NOT set QT_QPA_PLATFORM=offscreen: on this Qt/PySide6 build, that
platform plugin's font engine renders every drawText() call as blank "tofu" boxes
(shapes/lines are unaffected - only text). The default "windows" platform paints text
correctly onto a QPixmap without ever showing a window, so plain QApplication(...) with
no .exec() call is enough.

Usage:
  python tools/render_stackup_preview.py STACKUP.xml preview.png
  python tools/render_stackup_preview.py combined.xml die1_preview.png --chiplet die1_bondline
  python tools/render_stackup_preview.py combined.xml topology_preview.png --topology
"""

import argparse
import sys

from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from gds2palace import stackup_reader
from setupEM.setup_common import (
    compute_stackup_layout, compute_topology_overview_layout, render_stackup_layout,
    epsilon_to_color, default_stackup_dielectric_label, default_stackup_metal_label,
)

DEFAULT_WIDTH = 700
DEFAULT_HEIGHT = 900


def _dielectric_color(material):
    return epsilon_to_color(material.eps, 95)


def _metal_color(material):
    return None  # no override - compute_stackup_layout()'s default type-based color


def _dielectric_label(dielectric, material):
    return default_stackup_dielectric_label(dielectric, material)


def _metal_label(metal, material, is_sheet):
    return default_stackup_metal_label(metal, material, is_sheet)


def _via_label_suffix(metal, material):
    return ""


def render (xml_path, png_path, active_chiplet_id=None, topology=False,
            width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
  materials_list, dielectrics_list, metals_list = stackup_reader.read_substrate(xml_path)

  if topology:
    draw_calls, _ = compute_topology_overview_layout(
        dielectrics_list, materials_list, width, height, _dielectric_color, _dielectric_label)
  else:
    draw_calls, _ = compute_stackup_layout(
        materials_list, dielectrics_list, metals_list, width, height,
        _dielectric_color, _dielectric_label, _metal_label, _via_label_suffix, _metal_color,
        active_chiplet_id=active_chiplet_id)

  pixmap = QPixmap(width, height)
  painter = QPainter(pixmap)
  render_stackup_layout(draw_calls, painter, width, height)
  painter.end()
  pixmap.save(png_path, "PNG")
  print(f"wrote {png_path}")


def main():
  parser = argparse.ArgumentParser(description=__doc__,
                                    formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("xml_path", help="stackup XML file to render")
  parser.add_argument("png_path", help="output PNG path")
  parser.add_argument("--chiplet", default=None,
                       help="chiplet id to render (dielectrics_list.chiplet_groups.chiplets[i].id) "
                            "- default is the first detected chiplet, or the whole stackup if none "
                            "were detected")
  parser.add_argument("--topology", action="store_true",
                       help="render the topology overview sketch (all chiplets side by side on "
                            "the shared interposer base) instead of one chiplet's cross section - "
                            "only meaningful with 2+ detected chiplets")
  parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
  parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
  args = parser.parse_args()

  app = QApplication(sys.argv[:1])
  render(args.xml_path, args.png_path, active_chiplet_id=args.chiplet, topology=args.topology,
         width=args.width, height=args.height)


if __name__ == "__main__":
  main()
