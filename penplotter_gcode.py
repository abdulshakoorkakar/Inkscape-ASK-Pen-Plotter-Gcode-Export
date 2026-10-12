#!/usr/bin/env python3
"""
ASK Pen Plotter G-code Export - Inkscape 1.x extension.

Converts visible paths/shapes to G-code using configurable pen up / pen down
commands (e.g. "M3 S0" / "M3 S180" for a servo pen lift).

Modes:
  none    - Pen Plotter: follow the path exactly
  inside  - Pen Plotter: closed paths offset inward by half the pen width
  outside - Pen Plotter: closed paths offset outward by half the pen width
  fill    - Pen Plotter: closed paths hatched with parallel lines
  knife   - Drag Knife: corner swivel compensation + overcut
"""

import math
import os

import inkex
from inkex import Group, Transform, Use, ShapeElement, TextElement
from inkex.bezier import cspsubdiv

SKIP_TAGS = ("defs", "clipPath", "mask", "marker", "pattern", "symbol",
             "metadata", "namedview")

MODE_NAMES = {
    "knife": "Drag Knife: Cutout",
    "none": "Pen Plotter: (no offset)",
    "inside": "Pen Plotter: (path inside)",
    "outside": "Pen Plotter: (path outside)",
    "fill": "Pen Plotter: (lines fill)",
}

KNIFE_SWIVEL_MIN = math.radians(20)   # corners sharper than this get a swivel
KNIFE_ARC_STEP = math.radians(15)     # resolution of swivel arcs


# ----------------------------------------------------------------------
# Geometry helpers (all in machine millimetres)
# ----------------------------------------------------------------------
def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def clean(pts, eps=1e-6):
    """Remove consecutive duplicate points."""
    out = []
    for p in pts:
        if not out or dist(p, out[-1]) > eps:
            out.append(p)
    return out


def is_closed(pts):
    return len(pts) >= 4 and dist(pts[0], pts[-1]) < 1e-4


def point_in_poly(pt, poly):
    x, y = pt
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            if x < xi + (y - yi) * (xj - xi) / (yj - yi):
                inside = not inside
        j = i
    return inside


def offset_closed(pts, d):
    """Offset a closed polyline by d mm (positive = outward, negative = inward)."""
    ring = clean(pts[:-1])
    if len(ring) > 1 and dist(ring[0], ring[-1]) < 1e-6:
        ring.pop()
    n = len(ring)
    if n < 3:
        return pts
    area = 0.0
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    sgn = 1.0 if area > 0 else -1.0

    normals = []
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy) or 1.0
        normals.append((dy / length * sgn, -dx / length * sgn))

    out = []
    for i in range(n):
        n1 = normals[i - 1]
        n2 = normals[i]
        px, py = ring[i]
        den = 1.0 + n1[0] * n2[0] + n1[1] * n2[1]
        if den < 0.3:  # very sharp corner: bevel instead of a long spike
            out.append((px + d * n1[0], py + d * n1[1]))
            out.append((px + d * n2[0], py + d * n2[1]))
        else:
            out.append((px + d * (n1[0] + n2[0]) / den,
                        py + d * (n1[1] + n2[1]) / den))
    out.append(out[0])
    return out


def hatch(polys, spacing, angle_deg):
    """Even-odd hatch fill of closed polylines with parallel lines."""
    if not polys:
        return []
    spacing = max(spacing, 0.05)
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)

    edges = []
    ymin = ymax = None
    for poly in polys:
        rot = [(x * c + y * s, -x * s + y * c) for x, y in poly]
        for i in range(len(rot) - 1):
            x1, y1 = rot[i]
            x2, y2 = rot[i + 1]
            if y1 != y2:
                edges.append((x1, y1, x2, y2))
        for _x, ry in rot:
            ymin = ry if ymin is None else min(ymin, ry)
            ymax = ry if ymax is None else max(ymax, ry)
    if not edges:
        return []

    lines = []
    y = ymin + spacing / 2.0
    while y < ymax:
        xs = []
        for x1, y1, x2, y2 in edges:
            if (y1 <= y < y2) or (y2 <= y < y1):
                xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        for k in range(0, len(xs) - 1, 2):
            xa, xb = xs[k], xs[k + 1]
            if xb - xa > 1e-6:
                lines.append([(xa * c - y * s, xa * s + y * c),
                              (xb * c - y * s, xb * s + y * c)])
        y += spacing
    return lines


def drag_knife(pts, off, overcut):
    """Convert a blade-tip path into a knife-pivot path with corner swivels."""
    pts = clean(pts)
    if off <= 0 or len(pts) < 2:
        return pts
    closed = is_closed(pts)

    dirs = []
    lens = []
    for i in range(len(pts) - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        length = math.hypot(dx, dy) or 1.0
        dirs.append((dx / length, dy / length))
        lens.append(length)

    def swivel(out, centre, d1, d2):
        a1 = math.atan2(d1[1], d1[0])
        a2 = math.atan2(d2[1], d2[0])
        da = (a2 - a1 + math.pi) % (2 * math.pi) - math.pi
        if abs(da) < KNIFE_SWIVEL_MIN:
            return
        steps = max(2, int(math.ceil(abs(da) / KNIFE_ARC_STEP)))
        for k in range(1, steps + 1):
            ang = a1 + da * k / steps
            out.append((centre[0] + off * math.cos(ang),
                        centre[1] + off * math.sin(ang)))

    d0 = dirs[0]
    out = [(pts[0][0] + off * d0[0], pts[0][1] + off * d0[1])]
    for i, d in enumerate(dirs):
        end = pts[i + 1]
        out.append((end[0] + off * d[0], end[1] + off * d[1]))
        if i + 1 < len(dirs):
            swivel(out, end, d, dirs[i + 1])
        elif closed:
            swivel(out, end, d, d0)

    if closed and overcut > 0:
        extra = min(overcut, lens[0])
        out.append((pts[0][0] + (off + extra) * d0[0],
                    pts[0][1] + (off + extra) * d0[1]))
    return out


def order_paths(paths):
    """Greedy nearest-neighbour ordering; paths may be reversed."""
    remaining = list(paths)
    ordered = []
    pos = (0.0, 0.0)
    while remaining:
        best_i, best_rev, best_d = 0, False, None
        for i, p in enumerate(remaining):
            d = dist(pos, p[0])
            if best_d is None or d < best_d:
                best_i, best_rev, best_d = i, False, d
            d = dist(pos, p[-1])
            if d < best_d:
                best_i, best_rev, best_d = i, True, d
        p = remaining.pop(best_i)
        if best_rev:
            p.reverse()
        ordered.append(p)
        pos = p[-1]
    return ordered


# ----------------------------------------------------------------------
class PenPlotterGcode(inkex.EffectExtension):

    def add_arguments(self, pars):
        pars.add_argument("--tab", default="pen")
        pars.add_argument("--output_file", default="")
        pars.add_argument("--pen_up", default="M3 S0")
        pars.add_argument("--pen_down", default="M3 S180")
        pars.add_argument("--pen_delay", type=int, default=200)
        pars.add_argument("--dwell_units", default="s")
        pars.add_argument("--draw_speed", type=int, default=1500)
        pars.add_argument("--travel_speed", type=int, default=4000)
        # mode
        pars.add_argument("--mode", default="none")
        pars.add_argument("--pen_width", type=float, default=0.5)
        pars.add_argument("--fill_spacing", type=float, default=0.5)
        pars.add_argument("--fill_angle", type=float, default=45.0)
        pars.add_argument("--fill_outline", type=inkex.Boolean, default=True)
        pars.add_argument("--knife_offset", type=float, default=0.25)
        pars.add_argument("--knife_overcut", type=float, default=1.0)
        # geometry
        pars.add_argument("--selection_only", type=inkex.Boolean, default=True)
        pars.add_argument("--flip_y", type=inkex.Boolean, default=True)
        pars.add_argument("--optimize", type=inkex.Boolean, default=True)
        pars.add_argument("--return_home", type=inkex.Boolean, default=True)
        pars.add_argument("--tolerance", type=float, default=0.05)
        pars.add_argument("--scale", type=float, default=100.0)
        pars.add_argument("--offset_x", type=float, default=0.0)
        pars.add_argument("--offset_y", type=float, default=0.0)

    # ------------------------------------------------------------------
    def walk(self, elem, parent_tr, from_use=False):
        """Recursively collect shapes; each shape is a list of polylines
        (one per subpath) in SVG user units."""
        if not isinstance(elem, inkex.BaseElement):
            return  # comments etc.
        tag = getattr(elem, "TAG", "")
        if tag in SKIP_TAGS and not from_use:
            return
        try:
            if elem.style.get("display") == "none" or elem.get("display") == "none":
                return
        except Exception:
            pass

        tr = parent_tr @ elem.transform

        if isinstance(elem, Use):
            target = elem.href
            if target is not None:
                try:
                    dx = float(elem.get("x", 0))
                    dy = float(elem.get("y", 0))
                except ValueError:
                    dx = dy = 0.0
                self.walk(target, tr @ Transform(translate=(dx, dy)), from_use=True)
            return

        if isinstance(elem, Group) or tag == "symbol":
            for child in elem:
                self.walk(child, tr)
            return

        if isinstance(elem, TextElement) or tag in ("flowRoot",):
            self.text_count += 1
            return

        if tag == "image" or not isinstance(elem, ShapeElement):
            return

        try:
            path = elem.path
        except Exception:
            return
        if path is None or len(path) == 0:
            return

        csp = path.transform(tr).to_superpath()
        cspsubdiv(csp, self.flat)
        shape = []
        for sub in csp:
            pts = [(node[1][0], node[1][1]) for node in sub]
            if len(pts) >= 2:
                shape.append(pts)
        if shape:
            self.shapes.append(shape)

    # ------------------------------------------------------------------
    def effect(self):
        opt = self.options
        mode = opt.mode if opt.mode in MODE_NAMES else "none"

        out_path = os.path.expanduser((opt.output_file or "").strip())
        if not out_path:
            raise inkex.AbortExtension("Please choose an output file.")
        if not os.path.splitext(out_path)[1]:
            out_path += ".gcode"

        svg = self.svg
        uu_per_mm = svg.unittouu("1mm") or 1.0
        self.flat = max(opt.tolerance, 0.001) * uu_per_mm
        self.shapes = []
        self.text_count = 0

        # --- gather elements ---
        selection = svg.selection if hasattr(svg, "selection") else svg.selected
        selected = list(selection.values()) if opt.selection_only else []
        if selected:
            for elem in selected:
                parent = elem.getparent()
                ptr = parent.composed_transform() if parent is not None else Transform()
                self.walk(elem, ptr)
        else:
            for child in svg:
                self.walk(child, Transform())

        if not self.shapes:
            msg = "Nothing to export: no visible paths or shapes found."
            if self.text_count:
                msg += ("\nText objects were found - convert them with "
                        "Path > Object to Path first.")
            raise inkex.AbortExtension(msg)

        # --- convert to machine coordinates (mm) ---
        vb = svg.get_viewbox()
        if not vb or len(vb) < 4 or vb[3] == 0:
            vb = [0.0, 0.0, svg.unittouu(svg.get("width", "210mm")),
                  svg.unittouu(svg.get("height", "297mm"))]
        vb_x, vb_y, _vb_w, vb_h = vb
        s = opt.scale / 100.0

        def to_machine(pt):
            x = (pt[0] - vb_x) / uu_per_mm
            if opt.flip_y:
                y = (vb_y + vb_h - pt[1]) / uu_per_mm
            else:
                y = (pt[1] - vb_y) / uu_per_mm
            return (x * s + opt.offset_x, y * s + opt.offset_y)

        shapes_mm = []
        for shape in self.shapes:
            subs = []
            for poly in shape:
                pts = clean([to_machine(p) for p in poly])
                if len(pts) >= 2:
                    subs.append(pts)
            if subs:
                shapes_mm.append(subs)

        # --- apply mode ---
        paths = []
        open_skipped = 0
        half = max(opt.pen_width, 0.0) / 2.0
        for subs in shapes_mm:
            closed = [p for p in subs if is_closed(p)]
            if mode in ("inside", "outside"):
                for p in subs:
                    if not is_closed(p):
                        paths.append(p)
                        open_skipped += 1
                        continue
                    # holes (odd nesting depth) are offset the opposite way
                    depth = sum(1 for q in closed
                                if q is not p and point_in_poly(p[0], q))
                    d = half if mode == "outside" else -half
                    if depth % 2:
                        d = -d
                    paths.append(offset_closed(p, d))
            elif mode == "fill":
                for p in subs:
                    if opt.fill_outline or not is_closed(p):
                        paths.append(p)
                paths.extend(hatch(closed, opt.fill_spacing, opt.fill_angle))
            else:
                paths.extend(subs)

        if opt.optimize:
            paths = order_paths(paths)

        if mode == "knife":
            paths = [drag_knife(p, opt.knife_offset, opt.knife_overcut)
                     for p in paths]

        # round + drop degenerate paths
        final = []
        for p in paths:
            pts = []
            for x, y in p:
                m = (round(x, 3) + 0.0, round(y, 3) + 0.0)
                if not pts or m != pts[-1]:
                    pts.append(m)
            if len(pts) >= 2:
                final.append(pts)
        paths = final

        if not paths:
            raise inkex.AbortExtension("Nothing to export (all paths are zero length).")

        # --- build G-code ---
        if opt.pen_delay > 0:
            if opt.dwell_units == "ms":
                dwell = "G4 P%d" % opt.pen_delay
            else:
                dwell = "G4 P%.3f" % (opt.pen_delay / 1000.0)
        else:
            dwell = None

        pen_up = opt.pen_up.strip()
        pen_down = opt.pen_down.strip()
        tool = "knife" if mode == "knife" else "pen"

        g = []
        g.append("; Generated by Inkscape Pen Plotter G-code Export")
        g.append("; Mode: %s" % MODE_NAMES[mode])
        g.append("; Paths: %d" % len(paths))
        g.append("G21 ; units: mm")
        g.append("G90 ; absolute positioning")
        g.append("%s ; %s up" % (pen_up, tool))
        if dwell:
            g.append(dwell)

        pos = (0.0, 0.0)
        draw_len = travel_len = 0.0
        xs, ys = [], []

        for pts in paths:
            start = pts[0]
            travel_len += dist(pos, start)
            g.append("G0 X%.3f Y%.3f F%d" % (start[0], start[1], opt.travel_speed))
            g.append("%s ; %s down" % (pen_down, tool))
            if dwell:
                g.append(dwell)
            first = True
            prev = start
            for p in pts[1:]:
                if first:
                    g.append("G1 X%.3f Y%.3f F%d" % (p[0], p[1], opt.draw_speed))
                    first = False
                else:
                    g.append("G1 X%.3f Y%.3f" % p)
                draw_len += dist(prev, p)
                prev = p
            g.append("%s ; %s up" % (pen_up, tool))
            if dwell:
                g.append(dwell)
            pos = pts[-1]
            xs.extend(p[0] for p in pts)
            ys.extend(p[1] for p in pts)

        if opt.return_home:
            travel_len += dist(pos, (0.0, 0.0))
            g.append("G0 X0 Y0 F%d ; return home" % opt.travel_speed)
        g.append("; end")

        try:
            with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(g) + "\n")
        except OSError as err:
            raise inkex.AbortExtension("Could not write file:\n%s" % err)

        # --- report ---
        minutes = (draw_len / max(opt.draw_speed, 1)
                   + travel_len / max(opt.travel_speed, 1)
                   + len(paths) * 2 * opt.pen_delay / 60000.0)
        report = [
            "Saved: %s" % out_path,
            "Mode: %s" % MODE_NAMES[mode],
            "Paths: %d" % len(paths),
            "Drawing distance: %.0f mm" % draw_len,
            "Pen-up travel: %.0f mm" % travel_len,
            "Estimated time: %.1f min" % minutes,
            "Plot area: X %.1f to %.1f mm, Y %.1f to %.1f mm"
            % (min(xs), max(xs), min(ys), max(ys)),
        ]
        if min(xs) < 0 or min(ys) < 0:
            report.append("WARNING: some coordinates are negative - part of the "
                          "drawing is outside the page.")
        if open_skipped:
            report.append("NOTE: %d open path(s) cannot be offset and were "
                          "drawn on the line." % open_skipped)
        if self.text_count:
            report.append("WARNING: %d text object(s) skipped - use "
                          "Path > Object to Path first." % self.text_count)
        inkex.utils.errormsg("\n".join(report))


if __name__ == "__main__":
    PenPlotterGcode().run()
