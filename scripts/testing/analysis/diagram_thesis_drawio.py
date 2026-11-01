#!/usr/bin/env python3
"""Thesis figure family (drawio) -- sources in tese/images/src/, PNG exports in tese/images/.

Visual system follows the reference figure style: red client actors, strong-blue
controller, light-blue data plane, green storage, amber telemetry, grey compute,
Times New Roman labels, no figure titles, explicit arrow labels, and an inline
line-sample key instead of a boxed legend. Output files always use a distinct
name so that existing images are never overwritten.
Regenerate with:  python source/scripts/testing/analysis/diagram_thesis_drawio.py
"""
from __future__ import annotations

import base64
import subprocess
import sys
from html import escape
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
OUT_DIO = REPO / "tese" / "images" / "src"
OUT_PNG = REPO / "tese" / "images"
DRAWIO = r"C:\Program Files\draw.io\draw.io.exe"

FONT = "Helvetica"

# ── standard (see tese/images/src/STYLE.md) ──────────────────────────
STD = {
    "frame": "#F4FFFF", "data": "#000000", "telemetry": "#FF8000",
    "control": "#007FFF", "client": "#FF6666", "label": "#3F3F3F",
}
ICON = {
    "dataplane": "mxgraph.citrix.switch",
    "servers": "mxgraph.citrix.license_server",
    "storage": "mxgraph.networks.storage",
    "mongo": "mxgraph.weblogos.mongodb",
    "aggregator": "mxgraph.office.servers.monitoring_sql_reporting_services",
    "wan": "mxgraph.citrix.router",
}
CTRL_ASSET = OUT_DIO / "assets" / "controller.png"
if (OUT_DIO / "assets" / "sdn_controller_logo.png").exists():
    CTRL_ASSET = OUT_DIO / "assets" / "sdn_controller_logo.png"

# ── palette ─────────────────────────────────────────────────────────
# solid shapes carry the saturated family colour, grouping frames a pale tint
P = {
    "controller": ("#2C6EA8", "#17456E"),   # strong blue
    "client":     ("#F5E6E6", "#8B3A3A"),   # red
    "dataplane":  ("#E8F2FA", "#6FA3C7"),   # light blue
    "storage":    ("#E0ECE0", "#4F7D4F"),   # green
    "telemetry":  ("#F4ECDA", "#8A6D2A"),   # amber
    "compute":    ("#F0F0F0", "#3A3A3A"),   # grey
    "neutral":    ("#FFFFFF", "#8A8A8A"),
}

# label colour inside a solid shape (dark fills need light text)
TEXT = {"controller": "#FFFFFF"}

LIGHT = {k: v[0] for k, v in P.items()} | {
    "controller": "#EAF3FB", "client": "#FBF1F1", "dataplane": "#F1F8FD",
    "storage": "#F1F8F1", "telemetry": "#FBF6EC", "compute": "#F7F7F7",
}

S_DATA = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;"
          "strokeColor=#333333;strokeWidth=1.6;fontFamily=%s;fontSize=11;fontColor=#333333;" % FONT)
S_REPL = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;"
          "strokeColor=#4F7D4F;strokeWidth=1.4;dashed=1;fontFamily=%s;fontSize=11;fontColor=#3F6B3F;" % FONT)
S_HB = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;startArrow=oval;endArrow=oval;"
        "startFill=1;endFill=1;strokeColor=#8A8A8A;strokeWidth=1.2;dashed=1;dashPattern=1 3;"
        "fontFamily=%s;fontSize=10;fontColor=#6B6B6B;" % FONT)
S_FRAME = ("rounded=1;whiteSpace=wrap;html=1;fillColor=none;strokeColor=#9A9A9A;"
           "strokeWidth=1.4;dashed=1;fontFamily=%s;fontSize=12;fontColor=#6B6B6B;"
           "verticalAlign=top;spacingTop=6;" % FONT)
S_CTRL = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;"
          "strokeColor=#2C5F8A;strokeWidth=1.4;dashed=1;fontFamily=%s;fontSize=11;"
          "fontColor=#2C5F8A;" % FONT)
S_SUM = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;"
         "strokeColor=#B39A2E;strokeWidth=1.5;dashed=1;fontFamily=%s;fontSize=11;"
         "fontColor=#8A7620;" % FONT)
S_OBS = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;"
         "strokeColor=#C9AE45;strokeWidth=1.3;dashed=1;dashPattern=1 3;fontFamily=%s;"
         "fontSize=10;fontColor=#8A7620;" % FONT)
S_BOTH = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;startArrow=block;endArrow=block;"
          "startFill=1;endFill=1;strokeColor=#17456E;strokeWidth=1.4;dashed=1;fontFamily=%s;"
          "fontSize=11;fontColor=#17456E;" % FONT)
S_OPLOG = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;"
           "strokeColor=#8A6D2A;strokeWidth=1.3;dashed=1;fontFamily=%s;fontSize=11;"
           "fontColor=#8A6D2A;" % FONT)


class Doc:
    def __init__(self, pw: int, ph: int):
        self.pw, self.ph = pw, ph
        self.cells: list[str] = []
        self._id = 2

    def _nid(self) -> str:
        self._id += 1
        return str(self._id)

    def cell(self, value: str, x, y, w, h, style: str) -> str:
        cid = self._nid()
        # escape first, then turn the pipe shorthand into an *escaped* break so the
        # attribute stays well-formed XML; drawio renders it as a line break (html=1)
        val = escape(value, quote=True).replace("|", "&lt;br&gt;")
        self.cells.append(
            f'<mxCell id="{cid}" value="{val}" style="{style}" '
            f'vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" '
            f'as="geometry"/></mxCell>'
        )
        return cid

    def edge(self, src: str, tgt: str, style: str, value: str = "", points=None) -> str:
        eid = self._nid()
        val = escape(value, quote=True).replace("|", "&lt;br&gt;")
        geo = '<mxGeometry relative="1" as="geometry">'
        if points:
            geo += '<Array as="points">' + "".join(
                f'<mxPoint x="{px}" y="{py}"/>' for px, py in points) + '</Array>'
        geo += "</mxGeometry>"
        self.cells.append(
            f'<mxCell id="{eid}" value="{val}" style="{style}" edge="1" '
            f'parent="1" source="{src}" target="{tgt}">{geo}</mxCell>'
        )
        return eid

    def write(self, name: str) -> Path:
        """Write the source, unless it already exists.

        A source on disk may have been edited by hand in drawio; overwriting it
        would discard that work. Pass --force to regenerate it from scratch.
        Existing sources are kept and still exported, so this script doubles as
        the PNG exporter for hand-edited figures.
        """
        path = OUT_DIO / name
        if path.exists() and "--force" not in sys.argv:
            print(f"keep   {name} (on disk; not overwritten)")
            return path
        body = "\n".join("      " + c for c in self.cells)
        path.write_text(
            '<mxfile host="app.diagrams.net">\n  <diagram name="Page-1" id="d1">\n'
            f'    <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" guides="1" '
            f'tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
            f'pageWidth="{self.pw}" pageHeight="{self.ph}" math="0" shadow="0" '
            'background="#FFFFFF">\n      <root>\n        <mxCell id="0"/>\n'
            '        <mxCell id="1" parent="0"/>\n' + body + '\n      </root>\n'
            '    </mxGraphModel>\n  </diagram>\n</mxfile>\n',
            encoding="utf-8",
        )
        return path


# ── shapes ──────────────────────────────────────────────────────────
def box(d: Doc, value, x, y, w, h, family="neutral", fs=12, bold=1, rounded=1,
        stroke_w=1.4) -> str:
    fill, stroke = P[family]
    b = 1 if bold else 0
    return d.cell(value, x, y, w, h,
                  f"rounded={rounded};whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
                  f"strokeWidth={stroke_w};fontFamily={FONT};fontSize={fs};fontStyle={b};"
                  f"fontColor={TEXT.get(family, '#222222')};verticalAlign=middle;")


def text(d: Doc, value, x, y, w, h, fs=11, bold=0, color="#444444", align="center",
         rot=0) -> str:
    b = 1 if bold else 0
    r = f"rotation={rot};" if rot else ""
    return d.cell(value, x, y, w, h,
                  f"text;html=1;align={align};verticalAlign=middle;fontFamily={FONT};"
                  f"fontSize={fs};fontStyle={b};fontColor={color};{r}")


def frame_box(d: Doc, label, x, y, w, h, family="neutral", fs=12) -> str:
    """Grouping frame: dashed family stroke over a lighter tint of the same family."""
    fill, stroke = LIGHT[family], P[family][1]
    return d.cell(label, x, y, w, h,
                  f"rounded=0;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
                  f"strokeWidth=1.1;dashed=1;dashPattern=7 5;verticalAlign=top;align=left;"
                  f"spacingLeft=12;spacingTop=6;fontFamily={FONT};fontSize={fs};"
                  f"fontColor={stroke};")


def server_glyph(d: Doc, cx, cy, s=42, family="compute") -> None:
    """Rack-unit mark: body + two bays + two status lamps."""
    fill, stroke = P[family]
    body_w, body_h = s * 1.2, s
    d.cell("", cx - body_w / 2, cy - body_h / 2, body_w, body_h,
           f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
           f"strokeWidth=1.3;")
    for k in range(2):
        by = cy - body_h * 0.22 + k * body_h * 0.34
        d.cell("", cx - body_w * 0.36, by, body_w * 0.5, s * 0.1,
               f"rounded=0;whiteSpace=wrap;html=1;fillColor={stroke};strokeColor=none;")
    for k in range(2):
        d.cell("", cx + body_w * 0.22, cy - body_h * 0.22 + k * body_h * 0.34, s * 0.11,
               s * 0.11,
               f"ellipse;whiteSpace=wrap;html=1;fillColor={'#6FA96F' if k else '#C9A227'};"
               f"strokeColor=none;")


def xor_glyph(d: Doc, cx, cy, s=30, color="#6B6B6B") -> None:
    """Crossed-path mark standing for a programmable fabric element."""
    for rot in (35, -35):
        d.cell("", cx - s * 0.5, cy - s * 0.09, s, s * 0.18,
               f"rounded=1;whiteSpace=wrap;html=1;fillColor={color};strokeColor=none;"
               f"rotation={rot};")
    for sx in (-1, 1):
        d.cell("", cx + sx * s * 0.44 - s * 0.07, cy - s * 0.28, s * 0.14, s * 0.14,
               f"rounded=0;whiteSpace=wrap;html=1;fillColor=none;strokeColor={color};"
               f"strokeWidth=1.6;")


def cylinder(d: Doc, value, x, y, w, h, family="storage", fs=12, stroke_w=1.4) -> str:
    fill, stroke = P[family]
    return d.cell(value, x, y, w, h,
                  f"shape=cylinder3;whiteSpace=wrap;html=1;boundedLbl=1;backgroundOutline=1;"
                  f"size=14;fillColor={fill};strokeColor={stroke};strokeWidth={stroke_w};"
                  f"fontFamily={FONT};fontSize={fs};fontColor=#222222;")


def leaf(d: Doc, cx, cy, s=34, fill="#4F7D4F") -> None:
    """MongoDB-style leaf mark (blade + midrib), drawn from primitives."""
    d.cell("", cx - s / 2, cy - s / 2, s, s * 0.62,
           f"ellipse;whiteSpace=wrap;html=1;fillColor={fill};strokeColor=none;rotation=45;")
    d.cell("", cx - 1.5, cy - s * 0.62, 3, s * 0.95,
           f"rounded=0;whiteSpace=wrap;html=1;fillColor={fill};strokeColor=none;")


def user_icon(d: Doc, cx, cy, s=34, fill="#C97B2E") -> None:
    d.cell("", cx - s * 0.22, cy - s * 0.62, s * 0.44, s * 0.44,
           f"ellipse;whiteSpace=wrap;html=1;fillColor={fill};strokeColor=none;")
    d.cell("", cx - s * 0.42, cy - s * 0.12, s * 0.84, s * 0.6,
           f"shape=mxgraph.basic.arc?;rounded=1;whiteSpace=wrap;html=1;fillColor={fill};"
           f"strokeColor=none;")


def rack_glyph(d: Doc, cx, cy, family="compute", bars=2, bw=56, bh=8, gap=12) -> None:
    """Component mark in the reference idiom: stacked bays plus status lamps."""
    fill, stroke = P[family]
    total = bars * bh + (bars - 1) * gap
    y0 = cy - total / 2
    for k in range(bars):
        by = y0 + k * (bh + gap)
        d.cell("", cx - bw / 2, by, bw, bh,
               f"rounded=0;whiteSpace=wrap;html=1;fillColor={stroke};strokeColor=none;")
        d.cell("", cx + bw / 2 - 15, by - 1, 10, 10,
               f"ellipse;whiteSpace=wrap;html=1;strokeColor={stroke};strokeWidth=1.5;"
               f"fillColor={'#7DBE7D' if k % 2 == 0 else '#D95B5B'};")


def actor(d: Doc, cx, cy, s=42, family="client") -> str:
    """Client workload mark (reference idiom: red actor silhouette). Returns its id."""
    fill, stroke = P[family]
    return d.cell("", cx - s / 2, cy - s, s, s * 2,
                  f"shape=actor;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
                  f"strokeWidth=1.4;")


def legend_row(d: Doc, x, y, rows: list[tuple[str, str]]) -> None:
    """Inline key: a short line sample followed by its label, reference style.

    rows: [(label, kind)] with kind in {solid, repl, control, obs, heartbeat}.
    """
    style = {"solid": "strokeColor=#333333;strokeWidth=1.7;",
             "repl": "strokeColor=#8A6D2A;strokeWidth=1.3;dashed=1;",
             "control": "strokeColor=#2C6EA8;strokeWidth=1.4;dashed=1;",
             "obs": "strokeColor=#8A6D2A;strokeWidth=1.3;dashed=1;dashPattern=1 3;",
             "heartbeat": "strokeColor=#8A8A8A;strokeWidth=1.2;dashed=1;dashPattern=1 3;"}
    cx = x
    for label, kind in rows:
        d.cell("", cx, y + 12, 44, 3,
               f"rounded=0;whiteSpace=wrap;html=1;fillColor=none;{style[kind]}")
        text(d, label, cx + 52, y, int(7.4 * len(label)) + 12, 26, fs=12,
             color="#333333", align="left")
        cx += 52 + int(7.4 * len(label)) + 46


def note(d: Doc, value, x, y, w, h, fs=11) -> str:
    return d.cell(value, x, y, w, h,
                  f"shape=note;whiteSpace=wrap;html=1;size=14;fillColor=#FFFFFF;"
                  f"strokeColor=#8A8A8A;strokeWidth=1.2;fontFamily={FONT};fontSize={fs};"
                  f"fontColor=#444444;align=left;spacingLeft=8;")


def legend(d: Doc, x, y, rows: list[tuple[str, str]]) -> None:
    """rows: [(label, line_style_kind)] with kind in {solid, dashed, dotted}."""
    w = 300
    h = 26 + 22 * len(rows)
    d.cell("", x, y, w, h, f"rounded=0;whiteSpace=wrap;html=1;fillColor=#FFFFFF;"
                           f"strokeColor=#B0B0B0;strokeWidth=1.0;")
    text(d, "Legend", x + 10, y + 4, 80, 18, fs=11, bold=1, color="#333333", align="left")
    for i, (label, kind) in enumerate(rows):
        ly = y + 26 + i * 22
        style = {"solid": "strokeColor=#333333;strokeWidth=1.6;",
                 "dashed": "strokeColor=#4F7D4F;strokeWidth=1.4;dashed=1;",
                 "ctrl": "strokeColor=#2C5F8A;strokeWidth=1.4;dashed=1;",
                 "amber": "strokeColor=#B39A2E;strokeWidth=1.5;dashed=1;",
                 "obs": "strokeColor=#C9AE45;strokeWidth=1.3;dashed=1;dashPattern=1 3;",
                 "dotted": "strokeColor=#8A8A8A;strokeWidth=1.2;dashed=1;dashPattern=1 3;"}[kind]
        d.cell("", x + 12, ly + 8, 40, 4,
               f"rounded=0;whiteSpace=wrap;html=1;fillColor=none;{style}")
        text(d, label, x + 60, ly, w - 70, 20, fs=10, color="#444444", align="left")


def s_frame(d: Doc, x, y, w, h, title: str, tw=420, th=30, fs=18) -> str:
    """Group frame plus its title, standard section 2. Returns the frame id.

    The frame is emitted first: drawio paints later cells on top, so a title
    written before the frame would be hidden by the frame's fill. The title is
    centred so that mirrored domains stay congruent.
    """
    fid = d.cell("", x, y, w, h,
                 f"rounded=1;whiteSpace=wrap;html=1;dashed=1;fillColor={STD['frame']};"
                 f"fontFamily={FONT};fontSize={fs};")
    d.cell(title, x + int((w - tw) / 2), y + 8, tw, th,
           f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};"
           f"fontSize={fs};fontStyle=1;fontColor={STD['label']};")
    return fid


def s_icon(d: Doc, x, y, w, h, kind: str, fill=None, stroke="none") -> str:
    """Stencil icon, standard section 3."""
    style = (f"shape={ICON[kind]};sketch=0;aspect=fixed;html=1;align=center;"
             f"outlineConnect=0;strokeColor={stroke};verticalLabelPosition=bottom;"
             f"verticalAlign=top;fontFamily={FONT};fontSize=18;fontStyle=1;"
             f"fontColor={STD['label']};")
    if fill:
        style += f"fillColor={fill};"
    return d.cell("", x, y, w, h, style)


def s_actor(d: Doc, x, y, w, h, label: str) -> str:
    """Client mark; the actor carries its own label, standard section 3."""
    return d.cell(label, x, y, w, h,
                  f"shape=actor;whiteSpace=wrap;html=1;fillColor={STD['client']};"
                  f"verticalLabelPosition=bottom;verticalAlign=top;fontFamily={FONT};"
                  f"fontSize=18;fontStyle=1;fontColor={STD['label']};")


def s_image(d: Doc, x, y, w, h) -> str:
    """Embedded artwork (the SDN controller), standard section 3."""
    b64 = base64.b64encode(CTRL_ASSET.read_bytes()).decode("ascii")
    return d.cell("", x, y, w, h,
                  f"shape=image;imageAspect=0;aspect=fixed;html=1;strokeColor=none;"
                  f"image=data:image/png,{b64};")


def s_label(d: Doc, text: str, cx, y, w=260, fs=18, h=30) -> str:
    """Component label beneath an icon, standard section 3."""
    return d.cell(text, cx - w / 2, y, w, h,
                  f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};"
                  f"fontSize={fs};fontStyle=1;fontColor={STD['label']};")


def s_edge_style(kind: str) -> str:
    colour = {"data": STD["data"], "flow": STD["telemetry"], "ctrl": STD["control"]}[kind]
    dash = "" if kind == "data" else "dashed=1;"
    label_colour = {"data": STD["label"], "flow": STD["telemetry"],
                    "ctrl": STD["control"]}[kind]
    return (f"edgeStyle=none;html=1;endArrow=classic;startArrow=classic;"
            f"strokeColor={colour};{dash}fontFamily={FONT};fontSize=18;fontStyle=1;"
            f"fontColor={label_colour};labelBackgroundColor=#FFFFFF;")


def s_edge(d: Doc, src, tgt, kind: str, label: str = "", points=None) -> str:
    return d.edge(src, tgt, s_edge_style(kind), label, points)


def s_abs_edge(d: Doc, x1, y1, x2, y2, kind: str, label: str = "", points=None) -> None:
    """Edge between positions rather than shapes (an endpoint is a group)."""
    geo = (f'<mxPoint x="{x1}" y="{y1}" as="sourcePoint"/>'
           f'<mxPoint x="{x2}" y="{y2}" as="targetPoint"/>')
    if points:
        geo += ('<Array as="points">'
                + "".join(f'<mxPoint x="{px}" y="{py}"/>' for px, py in points)
                + '</Array>')
    val = escape(label, quote=True).replace("|", "&lt;br&gt;")
    d.cells.append(
        f'<mxCell id="{d._nid()}" value="{val}" '
        f'style="{s_edge_style(kind)}" edge="1" parent="1">'
        f'<mxGeometry relative="1" as="geometry">{geo}</mxGeometry></mxCell>')


def fig_architecture_overview() -> None:
    """Two congruent network domains joined by the emulated wide-area link.

    Domain 2 is domain 1 mirrored by position only, so the halves are congruent by
    construction (standard sections 1 and 2). The controller artwork is embedded
    from src/assets/ (sdn_controller_logo.png preferred, controller.png as
    fallback), as in the reference figure.
    """
    FW, FH = 1111, 700
    FX, FY = {0: 40, 1: 1441}, 60
    d = Doc(2592, 880)

    def rx(x, w=0, dom=0):
        return x if dom == 0 else FW - (x + w)

    def ax(x, w=0, dom=0):
        return FX[dom] + rx(x, w, dom)

    def ay(y):
        return int(FY + y)

    ctrl = {}
    for dom in (0, 1):
        s_frame(d, FX[dom], FY, FW, FH, f"Network domain {dom + 1} (LAN {dom + 1})")

        cl = s_actor(d, ax(40, 60, dom), ay(43), 60, 70, "Client")

        sw = s_icon(d, ax(470, 111, dom), ay(60), 111, 50, "dataplane")
        s_label(d, "OVS Switch", ax(470, 111, dom) + 56, ay(118))

        srv = s_icon(d, ax(850, 73, dom), ay(40), 73, 95, "servers")
        s_label(d, "Containerised Edge Servers", ax(850, 73, dom) + 37, ay(143))

        sto = s_icon(d, ax(470, 95, dom), ay(300), 95, 90, "storage",
                     fill="#99FF99", stroke="#6881B3")
        s_icon(d, ax(505, 30, dom), ay(320), 30, 70, "mongo")
        s_label(d, "MongoDB Edge Storage", ax(470, 95, dom) + 48, ay(398))

        agg = s_icon(d, ax(841, 70, dom), ay(300), 70, 85, "aggregator",
                     fill=STD["telemetry"])
        s_label(d, "Telemetry Aggregator", ax(841, 70, dom) + 35, ay(393))

        ctrl[dom] = s_image(d, ax(470, 97, dom), ay(545), 97, 90)
        s_label(d, "SDN Controller", ax(470, 97, dom) + 49, ay(643))

        s_edge(d, cl, sw, "data", "http requests")
        s_edge(d, sw, srv, "data", "data")
        s_edge(d, sw, sto, "data", "data")
        s_edge(d, srv, agg, "flow", "metrics")
        s_edge(d, sto, agg, "flow", "metrics")
        s_edge(d, agg, ctrl[dom], "flow", "windowed summaries",
               points=[(ax(876, 0, dom), ay(570))])
        s_edge(d, ctrl[dom], sw, "ctrl", "OpenFlow",
               points=[(ax(390, 0, dom), ay(590)), (ax(390, 0, dom), ay(88))])
        s_edge(d, ctrl[dom], srv, "ctrl", "lifecycle management",
               points=[(ax(980, 0, dom), ay(590)), (ax(980, 0, dom), ay(100))])
        s_edge(d, ctrl[dom], sto, "ctrl", "lifecycle management")

    # emulated wide-area link, centred in the gap, on the servers' axis
    wx, wy, ww, wh = 1236, ay(88) - 40, 120, 80
    s_icon(d, wx, wy, ww, wh, "wan")
    s_label(d, "Emulated Wide-Area Link|(data path only)", wx + ww / 2, wy + wh + 8,
            w=300, h=56)
    s_abs_edge(d, ax(923, 0, 0), ay(88), wx, ay(88), "data", "data")
    s_abs_edge(d, wx + ww, ay(88), ax(923, 0, 1), ay(88), "data", "data")

    # peer topology exchange, routed below both domains
    s_edge(d, ctrl[0], ctrl[1], "ctrl", "peer topology exchange (host-local)",
           points=[(ax(518.5, 0, 0), 810), (ax(518.5, 0, 1), 810)])

    # cross-domain telemetry: each aggregator's summaries also reach the peer controller
    s_abs_edge(d, 1046, 468, 2034, 733, "flow", "windowed summaries (host-local)",
               points=[(1180, 468), (1180, 745), (2034, 745)])
    s_abs_edge(d, 1546, 468, 559, 733, "flow", "windowed summaries (host-local)",
               points=[(1412, 468), (1412, 745), (559, 745)])

    dio = d.write("architecture_overview_v2.drawio")
    export(dio, OUT_PNG / "architecture_overview_v2.png")


def export(dio_path: Path, png_path: Path, scale: int = 2) -> None:
    subprocess.run([DRAWIO, "-x", "-f", "png", "-s", str(scale),
                    "-o", str(png_path), str(dio_path)], check=False)
    print("exported", png_path.name, png_path.exists())


def bracket(d: Doc, cx, y, w=150, h=28, color="#2C5F8A") -> None:
    """Square bracket marking an interface on the chain."""
    bar = f"rounded=0;whiteSpace=wrap;html=1;fillColor={color};strokeColor=none;"
    d.cell("", cx - w / 2, y + h - 3, w, 3, bar)
    d.cell("", cx - w / 2, y, 3, h, bar)
    d.cell("", cx + w / 2 - 3, y, 3, h, bar)


def vaxis(d: Doc, x, y0, y1, label, color="#6B6B6B") -> None:
    """Vertical axis with a head at the top and a rotated label."""
    d.cell("", x - 1.5, y0 + 14, 3, y1 - y0 - 14,
           f"rounded=0;whiteSpace=wrap;html=1;fillColor={color};strokeColor=none;")
    d.cell("", x - 9, y0, 18, 16,
           f"triangle;whiteSpace=wrap;html=1;fillColor={color};strokeColor=none;")
    text(d, label, x - 44, (y0 + y1) / 2 - 30, 34, 60, fs=12, color=color, rot=-90)


# ── standard figures ────────────────────────────────────────────────
def std_box(d: Doc, x, y, w, h, label: str, fill="#FFFFFF", stroke=STD["label"]) -> str:
    """Plain rounded node for a stage that has no component icon."""
    return d.cell(label, x, y, w, h,
                  f"rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
                  f"strokeWidth=1.5;fontFamily={FONT};fontSize=18;fontStyle=1;"
                  f"fontColor={STD['label']};")


def std_component(d: Doc, x, y, w, h, kind, label, ly=None, lw=300, lh=30,
                  fill=None, stroke="none") -> str:
    """Icon plus its label beneath it, standard section 3.

    Returns the *label* cell: an arrow pointing at a component attaches to its
    label, not to the artwork (standard section 6).
    """
    s_icon(d, x, y, w, h, kind, fill, stroke)
    return s_label(d, label, int(x + w / 2), ly if ly is not None else int(y + h + 8),
                   w=lw, h=lh)


def std_controller(d: Doc, x, y, label: str, lw=300) -> str:
    """Controller artwork plus its label; returns the label cell, as above."""
    s_image(d, x, y, 97, 90)
    return s_label(d, label, int(x + 48), int(y + 98), w=lw)


def std_demand_to_capacity_chain() -> None:
    """The chain from demand shift to usable capacity, with the three interfaces.

    The backend pool is drawn as a group of members with a free slot: the
    ready unit is admitted into it, and the clock marks the Interface 3 (RQ3)
    window -- the time between provisioning and the unit's entry into the
    pool, which is the point at which the capacity becomes usable. A fan of
    guide lines with a magnifier marks the pool as the controller's own route
    state, shown magnified.
    """
    d = Doc(2000, 700)
    demand = s_actor(d, 100, 150, 60, 70, "Demand shift")
    obs = std_component(d, 560, 140, 70, 85, "aggregator",
                        "Telemetry observation|and delivery", ly=233, lh=56,
                        fill=STD["telemetry"])
    dec = std_controller(d, 1010, 140, "Scaling decision")
    act = std_box(d, 1440, 145, 300, 80, "Capacity action")
    rdy = std_component(d, 1510, 430, 73, 95, "servers", "Backend readiness", ly=533)
    use = std_box(d, 80, 500, 300, 80, "Usable capacity", fill="#99FF99",
                  stroke="#6881B3")

    pool = s_frame(d, 560, 390, 580, 270, "Backend pool")
    std_component(d, 640, 460, 73, 95, "servers", "Edge server 1", ly=563, lw=240)
    std_component(d, 810, 460, 73, 95, "servers", "Edge server 2", ly=563, lw=240)
    slot = std_box(d, 980, 463, 90, 90, "+", fill="#FFFFFF")

    s_edge(d, demand, obs, "data", "demand")
    s_edge(d, obs, dec, "flow", "Interface 1 (RQ1)")
    s_edge(d, dec, act, "ctrl", "Interface 2 (RQ2)")
    s_edge(d, act, rdy, "data", "provisioning", points=[(1700, 505)])
    s_edge(d, rdy, slot, "ctrl", "Interface 3 (RQ3)")
    s_edge(d, pool, use, "data", "usable capacity")

    # zoom-in callout: the pool frame is the controller's own route state,
    # shown magnified, so a fan of thin guide lines links the decision step
    # to it (thin stroke-only bars, so they can carry a diagonal)
    d.cell("", 962, 268, 192, 1,
           f"rounded=0;whiteSpace=wrap;html=1;fillColor=none;strokeColor=#3F3F3F;"
           f"strokeWidth=1.5;fontFamily={FONT};")
    d.cell("", 558, 333, 414, 1,
           f"rounded=0;whiteSpace=wrap;html=1;fillColor=none;strokeColor=#3F3F3F;"
           f"strokeWidth=1.5;rotation=161.6;fontFamily={FONT};")
    d.cell("", 1076, 333, 133, 1,
           f"rounded=0;whiteSpace=wrap;html=1;fillColor=none;strokeColor=#3F3F3F;"
           f"strokeWidth=1.5;rotation=100;fontFamily={FONT};")
    d.cell("", 1040, 283, 36, 36,
           f"ellipse;whiteSpace=wrap;html=1;fillColor=none;strokeColor=#3F3F3F;"
           f"strokeWidth=3;fontFamily={FONT};")
    d.cell("", 1068, 318, 16, 3,
           f"rounded=0;whiteSpace=wrap;html=1;fillColor=none;strokeColor=#3F3F3F;"
           f"strokeWidth=3;rotation=45;fontFamily={FONT};")

    # interface mark: the clock sits on the admission transition, whose
    # duration is what Interface 3 (RQ3) is about
    d.cell("", 1211, 458, 44, 44,
           f"ellipse;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#007FFF;"
           f"strokeWidth=2;fontFamily={FONT};")
    d.cell("", 1232, 467, 2, 13,
           f"line;html=1;strokeColor=#007FFF;strokeWidth=2;fontFamily={FONT};")
    d.cell("", 1233, 479, 11, 2,
           f"line;html=1;strokeColor=#007FFF;strokeWidth=2;fontFamily={FONT};")

    export(d.write("demand_to_capacity_chain.drawio"),
           OUT_PNG / "demand_to_capacity_chain_v8.png")


def std_elastic_allocation() -> None:
    """Two congruent tiers, each scaled by the controller on its own signal."""
    d = Doc(1650, 900)
    ctrl = std_controller(d, 150, 390, "SDN Controller")

    FW, FH = 1060, 240
    f1 = s_frame(d, 480, 80, FW, FH, "Service tier — VIP_SERVER")
    f2 = s_frame(d, 480, 520, FW, FH, "Data tier — VIP_DATA")

    for i, x in enumerate((560, 760)):
        std_component(d, x, 150, 73, 95, "servers", f"Edge server {i + 1}", ly=253,
                      lw=220)
    std_box(d, 960, 155, 90, 90, "+", fill="#FFFFFF")

    for i, x in enumerate((560, 760)):
        std_component(d, x, 590, 95, 90, "storage", f"Storage member {i + 1}",
                      ly=690, lw=240, fill="#99FF99", stroke="#6881B3")
        s_icon(d, x + 32, 610, 30, 70, "mongo")
    std_box(d, 960, 595, 90, 90, "+", fill="#FFFFFF")

    s_edge(d, ctrl, f1, "ctrl", "lifecycle management|spawn / drain",
           points=[(380, 300)])
    s_edge(d, ctrl, f2, "ctrl", "lifecycle management|rs.add / rs.remove",
           points=[(380, 590)])
    s_edge(d, f1, ctrl, "flow", "metrics", points=[(430, 380)])
    s_edge(d, f2, ctrl, "flow", "metrics", points=[(430, 680)])

    export(d.write("elastic_allocation.drawio"), OUT_PNG / "elastic_allocation.png")


def std_sdn_architecture() -> None:
    """Control plane decoupled from the data plane, programming it over OpenFlow."""
    d = Doc(1700, 900)
    s_frame(d, 400, 60, 900, 220, "Control plane")
    ctrl = std_controller(d, 800, 110, "SDN Controller")
    s_frame(d, 100, 420, 1500, 300, "Data plane")

    sw = [std_component(d, x, 480, 111, 50, "dataplane", f"OVS Switch {i + 1}",
                        ly=538, lw=220)
          for i, x in enumerate((220, 700, 1180))]
    hosts = [std_box(d, x, 790, 130, 60, "Hosts") for x in (250, 730, 1210)]

    for i in range(2):
        s_abs_edge(d, 331 + i * 480, 505, 700 + i * 480, 505, "data", "data")
    for i, h in enumerate(hosts):
        s_edge(d, h, sw[i], "data", "data")
    for s in sw:
        s_edge(d, ctrl, s, "ctrl", "OpenFlow")

    export(d.write("sdn_architecture.drawio"), OUT_PNG / "sdn_architecture_v3.png")


def std_sdn_load_balancing() -> None:
    """The controller installs the rules that steer each flow to a chosen backend."""
    d = Doc(1800, 840)
    cl = s_actor(d, 120, 340, 60, 70, "Client")
    ctrl = std_controller(d, 640, 80, "SDN Controller")
    sw = std_component(d, 520, 330, 111, 50, "dataplane", "OVS Switch", ly=388)
    s_frame(d, 1150, 150, 560, 560, "Backend pool")
    pool = [std_component(d, 1240, y, 73, 95, "servers", f"Edge server {i + 1}",
                          ly=y + 103, lw=240)
            for i, y in enumerate((210, 400, 590))]

    s_edge(d, cl, sw, "data", "http requests")
    for b in pool:
        s_edge(d, sw, b, "data", "data")
    s_edge(d, ctrl, sw, "ctrl", "OpenFlow")

    export(d.write("sdn_load_balancing.drawio"), OUT_PNG / "sdn_load_balancing_v3.png")


def std_telemetry_delivery_paths() -> None:
    """Observations reach the aggregator; its summaries reach both controllers."""
    d = Doc(2000, 820)
    es = std_component(d, 120, 140, 73, 95, "servers", "Containerised Edge Servers",
                       ly=243)
    st = std_component(d, 120, 420, 95, 90, "storage", "MongoDB Edge Storage", ly=518,
                       fill="#99FF99", stroke="#6881B3")
    s_icon(d, 152, 440, 30, 70, "mongo")
    agg = std_component(d, 620, 270, 70, 85, "aggregator", "Telemetry Aggregator",
                        ly=363, fill=STD["telemetry"])
    s_frame(d, 900, 120, 260, 560, "Configurable|delivery interface", tw=240, th=56)
    c1 = std_controller(d, 1400, 180, "SDN Controller|(domain 1)")
    c2 = std_controller(d, 1400, 520, "SDN Controller|(domain 2)")

    s_edge(d, es, agg, "flow", "metrics")
    s_edge(d, st, agg, "flow", "metrics")
    s_edge(d, agg, c1, "flow", "windowed summaries")
    s_edge(d, agg, c2, "flow", "windowed summaries")

    export(d.write("telemetry_delivery_paths.drawio"),
           OUT_PNG / "telemetry_delivery_paths_v2.png")


def std_mongodb_replica_set() -> None:
    """Members of a replica set, the client paths, replication and heartbeats."""
    d = Doc(1500, 820)
    s_frame(d, 420, 120, 980, 480, "MongoDB replica set")
    cl = s_actor(d, 120, 320, 60, 70, "Client")

    members = [("Primary", 520, 220), ("Secondary", 1150, 180), ("Secondary", 1150, 400)]
    ids = []
    for label, x, y in members:
        ids.append(std_component(d, x, y, 95, 90, "storage", label, ly=y + 98,
                                 lw=220, fill="#99FF99", stroke="#6881B3"))
        s_icon(d, x + 32, y + 20, 30, 70, "mongo")
    prim, sec_a, sec_b = ids

    s_edge(d, cl, prim, "data", "writes")
    s_edge(d, cl, sec_b, "data", "reads", points=[(300, 700), (1197, 700)])
    s_edge(d, prim, sec_a, "flow", "replicates oplog")
    s_edge(d, prim, sec_b, "flow", "replicates oplog")
    s_edge(d, sec_a, sec_b, "ctrl", "heartbeats", points=[(1330, 225), (1330, 445)])

    export(d.write("mongodb_replica_set.drawio"), OUT_PNG / "mongodb_replica_set_v2.png")


def fig_demand_to_capacity_chain() -> None:
    """Chapter opening figure: the chain, its six stages and the three interfaces."""
    d = Doc(1520, 700)
    y1, y2, hw, hh = 150, 420, 280, 110

    b1 = box(d, "Demand shift", 80, y1, hw, hh, "client", fs=13)
    b2 = box(d, "Telemetry observation|and delivery", 420, y1, hw, hh, "telemetry", fs=12)
    b3 = box(d, "Scaling decision", 760, y1, hw, hh, "controller", fs=13)
    b4 = box(d, "Capacity action", 1100, y1, hw, hh, "compute", fs=13)

    b5 = box(d, "Backend readiness", 1100, y2, hw, hh, "compute", fs=13)
    b6 = box(d, "Routing admission", 760, y2, hw, hh, "controller", fs=13)
    b7 = box(d, "Usable capacity", 420, y2, hw, hh, "storage", fs=13, stroke_w=2.4)

    for a, b in ((b1, b2), (b2, b3), (b3, b4), (b4, b5), (b5, b6), (b6, b7)):
        d.edge(a, b, S_DATA)

    bracket(d, 730, 282)
    bracket(d, 1070, 282)
    bracket(d, 1070, 562)
    text(d, "Interface 1 (RQ1)|how demand evidence reaches the decision",
         600, 326, 260, 58, fs=11, color="#2C5F8A")
    text(d, "Interface 2 (RQ2)|which capacity action the decision|hands to the infrastructure",
         930, 320, 290, 74, fs=11, color="#2C5F8A")
    text(d, "Interface 3 (RQ3)|when a ready backend is admitted to traffic",
         930, 604, 290, 58, fs=11, color="#2C5F8A")
    vaxis(d, 1455, 140, 540, "time")
    note(d, "the chain is traversed in the direction of the arrows; each interface is a"
            " hand-off between components that run their own control loops",
         80, 604, 560, 62)

    dio = d.write("demand_to_capacity_chain.drawio")
    export(dio, OUT_PNG / "demand_to_capacity_chain_v3.png")


def fig_elastic_allocation() -> None:
    """Both tiers scale independently, on their own observation signal."""
    d = Doc(1480, 820)

    a1 = box(d, "spawn edge|replica", 90, 150, 210, 84, "controller", fs=12)
    text(d, "compute pressure", 90, 244, 210, 26, fs=11, color="#8A8171")
    frame_box(d, "edge server pool — VIP_SERVER", 440, 110, 560, 170, "compute")
    for x in (490, 610, 730):
        server_glyph(d, x + 45, 178, s=40)
    text(d, "…", 820, 155, 40, 40, fs=18, bold=1, color="#8A8171")
    box(d, "+", 880, 150, 56, 56, "compute", fs=20, bold=0)
    a2 = box(d, "drain and|remove replica", 1130, 150, 210, 84, "controller", fs=12)
    text(d, "sustained idleness", 1130, 244, 210, 26, fs=11, color="#8A8171")

    d.edge(a1, a2, S_DATA)

    b1 = box(d, "rs.add|member", 90, 530, 210, 84, "controller", fs=12)
    text(d, "data-access pressure", 90, 624, 210, 26, fs=11, color="#8A8171")
    frame_box(d, "MongoDB replica set — VIP_DATA", 440, 490, 560, 170, "storage")
    for x in (490, 610, 730):
        cylinder(d, "", x, 520, 90, 72)
        leaf(d, x + 45, 505, s=24)
    text(d, "…", 820, 535, 40, 40, fs=18, bold=1, color="#4F7D4F")
    box(d, "+", 880, 530, 56, 56, "storage", fs=20, bold=0)
    b2 = box(d, "rs.remove|member", 1130, 530, 210, 84, "controller", fs=12)
    text(d, "sustained idleness", 1130, 624, 210, 26, fs=11, color="#8A8171")

    d.edge(b1, b2, S_DATA)

    note(d, "scale-out adds capacity to the tier under sustained pressure, and scale-in|"
            "removes only capacity that the platform itself added",
         160, 706, 1160, 60)

    dio = d.write("elastic_allocation.drawio")
    export(dio, OUT_PNG / "elastic_allocation.png")


def fig_sdn_architecture() -> None:
    """Control plane decoupled from the data plane, programming it over OpenFlow."""
    d = Doc(1400, 800)
    frame_box(d, "control plane", 340, 80, 720, 170, "controller")
    ctrl = box(d, "SDN controller", 560, 118, 280, 92, "controller", fs=14)

    frame_box(d, "data plane", 100, 430, 1200, 230, "dataplane")
    sw = []
    for i, x in enumerate((200, 620, 1040)):
        b = box(d, "", x, 500, 150, 80, "dataplane")
        xor_glyph(d, x + 75, 526)
        text(d, f"switch {i + 1}", x, 546, 150, 24, fs=11, bold=1, color="#5A5A5A")
        sw.append(b)

    plain = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=none;"
             "strokeColor=#8A8A8A;strokeWidth=1.4;") + f"fontFamily={FONT};fontSize=11;"
    d.edge(sw[0], sw[1], plain)
    d.edge(sw[1], sw[2], plain)

    for i, x in enumerate((230, 650, 1070)):
        host = box(d, "hosts", x, 700, 120, 60, "neutral", fs=11, bold=0)
        d.edge(host, sw[i], S_DATA)
        d.edge(ctrl, sw[i], S_CTRL, "OpenFlow" if i == 1 else "",
               points=[(x + 60, 300)])

    legend(d, 30, 80, [("data flow", "solid"), ("OpenFlow control channel", "ctrl")])

    dio = d.write("sdn_architecture.drawio")
    export(dio, OUT_PNG / "sdn_architecture_v3.png")


def fig_sdn_load_balancing() -> None:
    """The controller installs the rules that steer each flow to a chosen backend."""
    d = Doc(1420, 740)
    cl = box(d, "", 50, 280, 180, 120, "client")
    user_icon(d, 140, 320)
    text(d, "client", 60, 356, 160, 26, fs=13, bold=1, color="#8A4E12")

    ctrl = box(d, "SDN controller", 560, 70, 250, 88, "controller", fs=13)
    note(d, "the controller chooses a backend for each new flow", 860, 70, 340, 88)

    sw = box(d, "", 430, 280, 200, 130, "dataplane")
    xor_glyph(d, 530, 322)
    text(d, "OVS switch", 430, 360, 200, 26, fs=12, bold=1, color="#5A5A5A")

    frame_box(d, "backend pool", 880, 170, 420, 440, "compute")
    backends = []
    for i, y in enumerate((200, 320, 440)):
        b = box(d, "", 930, y, 320, 95, "compute")
        server_glyph(d, 985, y + 47, s=38)
        text(d, f"edge server {i + 1}", 1030, y + 32, 200, 30, fs=12, bold=1,
             color="#6E6658", align="left")
        backends.append(b)

    d.edge(cl, sw, S_DATA, "requests")
    for i, b in enumerate(backends):
        d.edge(sw, b, S_DATA, "data flow|(selected backend)" if i == 1 else "")
    d.edge(ctrl, sw, S_BOTH, "OpenFlow", points=[(685, 200), (530, 200)])
    legend(d, 50, 470, [("client requests and selected-backend data flow", "solid"),
                        ("OpenFlow control channel", "ctrl")])

    dio = d.write("sdn_load_balancing.drawio")
    export(dio, OUT_PNG / "sdn_load_balancing_v3.png")


def fig_telemetry_delivery_paths() -> None:
    """Observations reach the aggregator; its summaries reach both controllers."""
    d = Doc(1420, 820)
    es = box(d, "", 90, 140, 210, 110, "compute")
    server_glyph(d, 140, 195, s=38)
    text(d, "edge servers", 178, 168, 120, 26, fs=12, bold=1, color="#6E6658",
         align="left")
    st = box(d, "", 90, 330, 210, 110, "storage")
    cylinder(d, "", 115, 355, 60, 60)
    text(d, "storage members", 183, 358, 115, 26, fs=12, bold=1, color="#3F6B3F",
         align="left")

    agg = box(d, "telemetry aggregator", 400, 225, 230, 130, "telemetry", fs=12)
    text(d, "windowed summaries", 400, 305, 230, 24, fs=11, color="#8A7620")

    frame_box(d, "", 700, 90, 90, 600, "client")
    text(d, "configurable delivery interface", 610, 58, 270, 26, fs=11, color="#C97B2E")
    text(d, "event-preserving · delayed · latest-state", 692, 300, 30, 200, fs=10,
         color="#C97B2E", rot=-90)

    c1 = box(d, "SDN controller|domain 1", 960, 170, 300, 110, "controller", fs=12)
    c2 = box(d, "SDN controller|domain 2", 960, 440, 300, 110, "controller", fs=12)

    d.edge(es, agg, S_OBS, "ZMQ observations", points=[(340, 195), (340, 265)])
    d.edge(st, agg, S_OBS, "", points=[(380, 385), (380, 315)])
    d.edge(agg, c1, S_SUM, "windowed summaries",
           points=[(660, 265), (900, 265), (900, 225)])
    d.edge(agg, c2, S_SUM, "", points=[(660, 335), (900, 335), (900, 495)])

    legend(d, 90, 620, [("ZMQ observations", "obs"), ("windowed summaries", "amber")])
    text(d, "edge servers and storage members publish observations over ZMQ; the aggregator"
            " turns them into windowed summaries",
         300, 706, 900, 26, fs=11, color="#777777")
    text(d, "each domain's aggregator publishes to both controllers; only this"
            " aggregation-to-controller link is configurable",
         300, 736, 900, 26, fs=11, color="#777777")

    dio = d.write("telemetry_delivery_paths.drawio")
    export(dio, OUT_PNG / "telemetry_delivery_paths_v2.png")


def std_vip_routing_sequence() -> None:
    """The double-VIP request path: service routing surface, then data routing surface.

    Interaction figure (STYLE.md section 8): icon headers with lifelines, one band
    per routing surface - the data surface carries the storage tint - and numbered
    messages in request-path order. The two footer
    lines state return-path semantics the arrows cannot carry.
    """
    W, H = 2000, 1720
    C, O, S, E, ST = 170, 560, 980, 1400, 1760
    d = Doc(W, H)

    def msg(x1, y1, x2, y2, kind, label=""):
        colour, dash = {"data": ("#000000", ""), "ctrl": ("#007FFF", "dashed=1;"),
                        "ret": ("#000000", "dashed=1;")}[kind]
        style = (f"edgeStyle=none;html=1;endArrow=classic;startArrow=none;strokeColor={colour};{dash}"
                 f"fontFamily={FONT};fontSize=24;fontStyle=1;fontColor={colour};labelBackgroundColor=#FFFFFF;")
        d.cells.append(
            f'<mxCell id="{d._nid()}" value="{escape(label, quote=True)}" style="{style}" edge="1" parent="1">'
            f'<mxGeometry relative="1" as="geometry">'
            f'<mxPoint x="{x1}" y="{y1}" as="sourcePoint"/><mxPoint x="{x2}" y="{y2}" as="targetPoint"/>'
            f'</mxGeometry></mxCell>')

    def band(x, y, w, h, title, fill, stroke):
        st = (f"rounded=1;whiteSpace=wrap;html=1;dashed=1;fillColor={fill};genre=interaction;"
              f"fontFamily={FONT};fontSize=26;")
        if stroke:
            st += f"strokeColor={stroke};"
        d.cell("", x, y, w, h, st)
        d.cell(title, x, y + 8, w, 36,
               f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};fontSize=26;"
               f"fontStyle=1;fontColor={STD['label']};")

    def icon(kind, x, y, w, h, fill=None, stroke="none"):
        st = (f"shape={kind};sketch=0;aspect=fixed;html=1;align=center;outlineConnect=0;"
              f"strokeColor={stroke};verticalLabelPosition=bottom;verticalAlign=top;"
              f"fontFamily={FONT};fontSize=24;fontStyle=1;fontColor={STD['label']};")
        if fill:
            st += f"fillColor={fill};"
        d.cell("", x, y, w, h, st)

    def column_label(text, cx, y, w=300, h=34, fs=24):
        d.cell(text, cx - w // 2, y, w, h,
               f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};"
               f"fontSize={fs};fontStyle=1;fontColor={STD['label']};")

    def chip(text, x, y, w=300, h=48):
        d.cell(text, x, y, w, h,
               f"shape=note;whiteSpace=wrap;html=1;size=14;fillColor=#FFFFFF;strokeColor=#8A8A8A;"
               f"strokeWidth=1.2;fontFamily={FONT};fontSize=16;fontColor=#444444;"
               f"align=left;spacingLeft=8;")

    band(80, 230, 1760, 490, "VIP_SERVER — service routing surface", "#F4FFFF", None)
    band(80, 750, 1760, 560, "VIP_DATA — data routing surface", "#F1F8F1", "#4F7D4F")

    for c in (C, O, S, E, ST):
        d.cell("", c - 1, 210, 2, 1310,
               "rounded=0;whiteSpace=wrap;html=1;fillColor=#B9B9B9;strokeColor=none;")

    d.cell("", 140, 60, 60, 70,
           "shape=actor;whiteSpace=wrap;html=1;fillColor=#FF6666;strokeColor=#3F3F3F;strokeWidth=1.4;")
    icon("mxgraph.citrix.switch", 504, 60, 111, 50)
    s_image(d, 932, 60, 97, 90)
    icon("mxgraph.citrix.license_server", 1364, 60, 73, 95)
    icon("mxgraph.networks.storage", 1712, 60, 95, 90, fill="#99FF99", stroke="#6881B3")
    icon("mxgraph.weblogos.mongodb", 1745, 80, 30, 70)
    column_label("Client", C, 168)
    column_label("OVS Switch", O, 168)
    column_label("SDN Controller", S, 168)
    column_label("Edge server", E, 168)
    column_label("Storage member|(MongoDB)", ST, 160, w=360, h=66)

    chip("no flow rule → punt to controller", 575, 325)
    chip("no flow rule → punt to controller", 575, 885)
    for text, y in (("3 · select_server():|lowest WSM cost over the edge pool", 470),
                    ("8 · select_storage():|lowest WSM cost over the replica set", 1020)):
        d.cell(text, 995, y, 390, 64,
               f"rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#8A8A8A;"
               f"strokeWidth=1.4;fontFamily={FONT};fontSize=20;fontColor=#333333;"
               f"align=left;spacingLeft=12;verticalAlign=middle;")

    msg(C, 300, O, 300, "data", "1 · HTTP request to VIP_SERVER")
    msg(O, 420, S, 420, "ctrl", "2 · Packet-In (first packet)")
    msg(S, 580, O, 580, "ctrl", "4 · install DNAT / SNAT rules")
    msg(O, 665, E, 665, "data", "5 · forwarded to the selected edge server")
    msg(E, 860, O, 860, "data", "6 · MongoDB query to VIP_DATA")
    msg(O, 970, S, 970, "ctrl", "7 · Packet-In (first packet)")
    msg(S, 1130, O, 1130, "ctrl", "9 · install DNAT rules")
    msg(O, 1215, ST, 1215, "data", "10 · forwarded to the selected member")
    msg(ST, 1350, O, 1350, "ret", "result (SNAT rewrites member → VIP_DATA)")
    msg(O, 1395, E, 1395, "ret", "response")
    msg(E, 1440, O, 1440, "ret", "response (SNAT rewrites edge → VIP_SERVER)")
    msg(O, 1485, C, 1485, "ret", "response")

    d.cell("return path: SNAT rules rewrite backend addresses back to the virtual addresses, "
           "so clients and replicas never learn backend identities", 80, 1560, 1760, 52,
           f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};fontSize=18;fontColor=#444444;")
    d.cell("flow-based forwarding: subsequent packets of the same flow are handled in the "
           "data plane, without controller intervention", 80, 1622, 1760, 52,
           f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};fontSize=18;fontColor=#444444;")

    dio = d.write("vip_routing_sequence.drawio")
    export(dio, OUT_PNG / "vip_routing_sequence_v2.png")


def std_control_workflow() -> None:
    """The demand-to-capacity chain across the four execution contexts.

    Laid out as two stacked rows of two contexts, so that all eight
    instrumented events stay in one figure at a legible size.

    Process figure (STYLE.md section 9): one frame per context, numbered
    instrumented events, guard conditions on the decision branches, and one
    compact notation key. Capacity removal follows the same steps in reverse.
    """
    W, H = 1200, 1385
    GREEN, AMBER = "#2E7D32", "#8A6D2A"
    d = Doc(W, H)

    def edge(src, tgt, label="", colour="#000000", dashed=False, fs=13, points=None,
             exitxy=None, entryxy=None):
        st = (f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=classic;startArrow=none;"
              f"strokeColor={colour};strokeWidth=1.3;{'dashed=1;' if dashed else ''}"
              f"fontFamily={FONT};fontSize={fs};fontColor={colour};labelBackgroundColor=#FFFFFF;")
        if exitxy:
            st += f"exitX={exitxy[0]};exitY={exitxy[1]};exitDx=0;exitDy=0;"
        if entryxy:
            st += f"entryX={entryxy[0]};entryY={entryxy[1]};entryDx=0;entryDy=0;"
        geo = '<mxGeometry relative="1" as="geometry">'
        if points:
            geo += '<Array as="points">' + "".join(
                f'<mxPoint x="{px}" y="{py}"/>' for px, py in points) + '</Array>'
        geo += "</mxGeometry>"
        d.cells.append(f'<mxCell id="{d._nid()}" value="{escape(label, quote=True)}" style="{st}" '
                       f'edge="1" parent="1" source="{src}" target="{tgt}">{geo}</mxCell>')

    def frame(x, y, w, h, title, subtitle):
        d.cell("", x, y, w, h,
               f"rounded=1;whiteSpace=wrap;html=1;dashed=1;fillColor=#F4FFFF;genre=process;"
               f"fontFamily={FONT};fontSize=18;")
        d.cell(title, x, y + 8, w, 28,
               f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};"
               f"fontSize=22;fontStyle=1;fontColor=#3F3F3F;")
        d.cell(subtitle, x, y + 38, w, 24,
               f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};"
               f"fontSize=15;fontColor=#6B6B6B;")

    def action_box(text, x, y, w, h, fs=17):
        return d.cell(text, x, y, w, h,
                      f"rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#3F3F3F;"
                      f"strokeWidth=1.4;fontFamily={FONT};fontSize={fs};fontColor=#222222;")

    def diamond(label, x, y, w, h, fs=14):
        return d.cell(label, x, y, w, h,
                      f"rhombus;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor={AMBER};"
                      f"strokeWidth=1.5;fontFamily={FONT};fontSize={fs};fontColor=#5C4400;")

    def obj(text, x, y, w, h, fs=16):
        return d.cell(text, x, y, w, h,
                      f"rounded=1;whiteSpace=wrap;html=1;fillColor=#F0F0F0;strokeColor=#8A8A8A;"
                      f"strokeWidth=1.3;fontFamily={FONT};fontSize={fs};fontColor=#333333;")

    def chip(text, x, y, w, h):
        return d.cell(text, x, y, w, h,
                      f"shape=note;whiteSpace=wrap;html=1;size=14;fillColor=#FFFFFF;"
                      f"strokeColor=#8A8A8A;strokeWidth=1.2;fontFamily={FONT};fontSize=15;"
                      f"fontColor=#444444;align=left;spacingLeft=8;")

    def badge(n, x, y):
        return d.cell(str(n), x, y, 26, 26,
                      f"ellipse;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor={GREEN};"
                      f"strokeWidth=2;fontFamily={FONT};fontSize=15;fontStyle=1;fontColor={GREEN};")

    def final_activity(x, y):
        d.cell("", x, y, 20, 20,
               f"ellipse;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor={GREEN};strokeWidth=2.5;")
        d.cell("", x + 6, y + 6, 8, 8, "ellipse;whiteSpace=wrap;html=1;fillColor=#000000;strokeColor=none;")

    def final_flow(cx, cy):
        d.cell("", cx - 10, cy - 10, 20, 20,
               "ellipse;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#000000;strokeWidth=2;")
        for rot in (45, -45):
            d.cell("", cx - 7, cy - 1, 14, 2,
                   f"rounded=0;whiteSpace=wrap;html=1;fillColor=#000000;strokeColor=none;rotation={rot};")

    # Two stacked rows of two execution contexts. The top row holds the two
    # sparse contexts -- the clients that generate the demand and the packet
    # context that steers requests -- and the bottom row the two dense ones, the
    # observation context that decides and the action context that executes.
    # Read clockwise from the top left: the demand enters (1), the summary
    # travels down into the observation context, the action context admits the
    # new backend on the right, and the packet context routes the first request
    # back up to the clients (8).
    def guard(text, x, y, w, align="center"):
        return d.cell(text, x, y, w, 22,
                      f"text;html=1;align={align};verticalAlign=middle;fontFamily={FONT};"
                      f"fontSize=11;fontColor=#5C4400;")

    frame(20, 20, 560, 450, "Clients and service replicas", "users and running service")
    frame(600, 20, 560, 450, "Packet context", "handles each request's routing; never blocks")
    frame(20, 490, 560, 790, "Observation context", "time-driven; evaluates summaries")
    frame(600, 490, 560, 790, "Action context", "slow path — queued and prioritised")

    # -- clients and service replicas: 1 and 8 ------------------------------
    start = d.cell("", 291, 96, 18, 18,
                   "ellipse;whiteSpace=wrap;html=1;fillColor=#000000;strokeColor=none;")
    badge(1, 315, 92)
    d.cell("demand shift", 180, 122, 240, 26,
           f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};fontSize=18;"
           f"fontStyle=1;fontColor=#3F3F3F;")
    rep = action_box("replicas serve requests", 140, 158, 320, 66, fs=18)
    first = action_box("first successful request", 140, 300, 320, 62)
    badge(8, 474, 305)
    final_activity(291, 384)
    d.cell("usable capacity", 180, 410, 240, 26,
           f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};fontSize=18;"
           f"fontStyle=1;fontColor=#3F3F3F;")
    edge(start, rep)
    edge(first, d.cell("", 291, 384, 20, 20,
                       "ellipse;whiteSpace=wrap;html=1;fillColor=none;strokeColor=none;"))

    # -- packet context -----------------------------------------------------
    chip("handles every request independently;|never blocks on provisioning", 620, 92, 500, 58)
    steer = action_box("steer request to replica", 690, 190, 380, 66, fs=18)
    newpkt = action_box("new packet routed to the new replica", 690, 330, 380, 66)
    edge(steer, rep, "request / response", points=[(590, 223), (590, 191)])
    edge(newpkt, steer)
    d.cell("steer new connections", 890, 268, 170, 24,
           f"text;html=1;align=left;verticalAlign=middle;fontFamily={FONT};fontSize=13;"
           f"fontColor=#555555;")
    edge(newpkt, first, "first request", fs=14, points=[(590, 363), (590, 331)])

    # -- observation context: 2, 3 and 4 ------------------------------------
    summary = obj("telemetry summary", 140, 560, 320, 60)
    d.cell("emit observations", 105, 236, 200, 22,
           f"text;html=1;align=left;verticalAlign=middle;fontFamily={FONT};fontSize=12;"
           f"fontColor=#FF8000;")
    edge(rep, summary, colour=STD["telemetry"], dashed=True,
         points=[(90, 191), (90, 590)])
    delivered = action_box("summary delivered to controller", 140, 650, 320, 60)
    badge(2, 470, 650)
    score = action_box("evaluate degradation score", 140, 740, 320, 60)
    over = diamond("overload sustained?", 140, 820, 320, 96)
    badge(3, 470, 825)
    tier = diamond("which tier is the bottleneck?", 130, 950, 340, 106)
    badge(4, 470, 1000)
    rq_compute = action_box("request compute scale-up", 70, 1110, 180, 58, fs=14)
    rq_storage = action_box("request storage scale-up", 350, 1110, 180, 58, fs=14)
    guard("[compute-bound]", 30, 1082, 120, align="right")
    guard("[data-access-bound]", 450, 1082, 120, align="left")
    queue = obj("action queue", 190, 1200, 220, 56)
    edge(summary, delivered)
    edge(delivered, score)
    edge(score, over, "[sustained]", fs=12)
    edge(over, delivered, "[not sustained]", fs=12, points=[(540, 868), (540, 700)])
    edge(over, tier)
    edge(tier, rq_compute)
    edge(tier, rq_storage)
    edge(rq_compute, queue)
    edge(rq_storage, queue)

    # -- action context: 5, 6 and 7 -----------------------------------------
    chip("runs mutations in its own context;|detection is never blocked by execution",
         620, 560, 500, 56)
    execute = action_box("execute action request", 660, 620, 400, 60, fs=18)
    badge(5, 1065, 625)
    edge(queue, execute, points=[(590, 1223), (590, 650)])
    sc_compute = action_box("scale up compute", 660, 720, 190, 56, fs=16)
    sc_storage = action_box("scale up storage", 870, 720, 190, 56, fs=16)
    guard("[compute-bound]", 630, 694, 120, align="right")
    guard("[data-access-bound]", 975, 694, 180, align="left")
    edge(execute, sc_compute)
    edge(execute, sc_storage)
    wait = action_box("wait until ready", 690, 830, 340, 56)
    badge(6, 1035, 835)
    edge(sc_compute, wait)
    edge(sc_storage, wait)
    ready = diamond("ready?", 730, 930, 260, 96, fs=15)
    edge(wait, ready)
    abandon = action_box("abandon unit", 620, 1080, 180, 56, fs=15)
    edge(ready, abandon, "[timeout]", fs=12)
    final_flow(710, 1180)
    edge(abandon, d.cell("", 700, 1170, 20, 20,
                         "ellipse;whiteSpace=wrap;html=1;fillColor=none;strokeColor=none;"))
    admit = action_box("admit to routing pool", 850, 1080, 210, 56, fs=15)
    edge(ready, admit, "[ready]", fs=12)
    badge(7, 812, 1085)
    pool = obj("routing pool (eligible backends)", 740, 1200, 240, 56, fs=15)
    edge(admit, pool)
    edge(pool, newpkt, colour=GREEN, dashed=True, points=[(1105, 1228), (1105, 363)])
    d.cell("new replica admitted", 940, 895, 155, 22,
           f"text;html=1;align=right;verticalAlign=middle;fontFamily={FONT};fontSize=11;"
           f"fontColor=#2E7D32;")

    # compact notation key (STYLE.md section 9)
    d.cell("", 20, 1300, 1120, 60,
           "rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#B0B0B0;strokeWidth=1.0;")

    def s_dot(x):
        d.cell("", x, 1320, 16, 16, "ellipse;whiteSpace=wrap;html=1;fillColor=#000000;strokeColor=none;")

    def s_fin(x):
        d.cell("", x, 1320, 16, 16,
               f"ellipse;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor={GREEN};strokeWidth=2.5;")
        d.cell("", x + 5, 1325, 6, 6, "ellipse;whiteSpace=wrap;html=1;fillColor=#000000;strokeColor=none;")

    def s_flow(x):
        d.cell("", x, 1320, 16, 16,
               "ellipse;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#000000;strokeWidth=2;")
        for rot in (45, -45):
            d.cell("", x + 1, 1327, 14, 2,
                   f"rounded=0;whiteSpace=wrap;html=1;fillColor=#000000;strokeColor=none;rotation={rot};")

    def s_action(x):
        d.cell("", x, 1320, 22, 16,
               "rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#3F3F3F;strokeWidth=1.3;")

    def s_dec(x):
        d.cell("", x, 1314, 24, 24,
               f"rhombus;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor={AMBER};strokeWidth=1.5;")

    def s_obj(x):
        d.cell("", x, 1320, 22, 16,
               "rounded=1;whiteSpace=wrap;html=1;fillColor=#F0F0F0;strokeColor=#8A8A8A;strokeWidth=1.2;")

    def s_note(x):
        d.cell("", x, 1316, 20, 20,
               "shape=note;whiteSpace=wrap;html=1;size=7;fillColor=#FFFFFF;strokeColor=#8A8A8A;")

    def s_evt(x):
        d.cell("1", x, 1314, 24, 24,
               f"ellipse;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor={GREEN};"
               f"strokeWidth=2;fontFamily={FONT};fontSize=13;fontStyle=1;fontColor={GREEN};")

    items = [
        (s_dot, "start node"),
        (s_fin, "activity final"),
        (s_flow, "flow final"),
        (s_action, "action"),
        (s_dec, "decision (guard)"),
        (s_obj, "object node"),
        (s_note, "note"),
        (s_evt, "event badge"),
    ]
    x = 40
    for draw, label in items:
        draw(x)
        w = 14 + int(6.8 * len(label))
        d.cell(label, x + 32, 1314, w, 26,
               f"text;html=1;align=left;verticalAlign=middle;fontFamily={FONT};"
               f"fontSize=12;fontColor=#444444;")
        x += 32 + w

    dio = d.write("control_workflow.drawio")
    render = REPO / "temp" / "control_workflow_render.png"
    render.parent.mkdir(parents=True, exist_ok=True)
    export(dio, render)
    subprocess.run([sys.executable, str(REPO / "tools" / "build_editable_png.py"),
                    str(render), str(dio),
                    str(OUT_DIO / "control_workflow.drawio.png")], check=False)


def std_decision_engine_flow() -> None:
    """The per-window evaluation of the decision engine as a numbered
    activity diagram, in the shape of the house reference theses: start node,
    action boxes, decision rhombi with bracketed guards, a shared next-window
    loop.

    Process figure (STYLE.md section 9). The numbered steps (1)-(4) follow
    the fixed evaluation order of the scaling policy; two request emissions
    leave the figure towards the action queue.
    """
    W, H = 2160, 1440
    AMBER = "#8A6D2A"
    d = Doc(W, H)

    def edge(src, tgt, label="", fs=18, points=None):
        st = (f"edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=classic;startArrow=none;"
              f"strokeColor=#000000;strokeWidth=1.3;fontFamily={FONT};fontSize={fs};"
              f"fontColor=#3F3F3F;labelBackgroundColor=#FFFFFF;")
        geo = '<mxGeometry relative="1" as="geometry">'
        if points:
            geo += '<Array as="points">' + "".join(
                f'<mxPoint x="{px}" y="{py}"/>' for px, py in points) + '</Array>'
        geo += "</mxGeometry>"
        d.cells.append(f'<mxCell id="{d._nid()}" value="{escape(label, quote=True)}" style="{st}" '
                       f'edge="1" parent="1" source="{src}" target="{tgt}">{geo}</mxCell>')

    def frame(x, y, w, h, title, subtitle):
        d.cell("", x, y, w, h,
               f"rounded=1;whiteSpace=wrap;html=1;dashed=1;fillColor=#F4FFFF;genre=process;"
               f"fontFamily={FONT};fontSize=18;")
        d.cell(title, x, y + 10, w, 38,
               f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};"
               f"fontSize=24;fontStyle=1;fontColor=#3F3F3F;")
        d.cell(subtitle, x, y + 46, w, 28,
               f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};"
               f"fontSize=18;fontColor=#3F3F3F;")

    def action_box(text, x, y, w, h, fs=22):
        return d.cell(text, x, y, w, h,
                      f"rounded=1;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor=#3F3F3F;"
                      f"strokeWidth=1.4;fontFamily={FONT};fontSize={fs};fontColor=#222222;")

    def diamond(label, x, y, w, h, fs=18):
        return d.cell(label, x, y, w, h,
                      f"rhombus;whiteSpace=wrap;html=1;fillColor=#FFFFFF;strokeColor={AMBER};"
                      f"strokeWidth=1.5;fontFamily={FONT};fontSize={fs};fontColor=#5C4400;")

    def obj(text, x, y, w, h, fs=20):
        return d.cell(text, x, y, w, h,
                      f"rounded=1;whiteSpace=wrap;html=1;fillColor=#F0F0F0;strokeColor=#8A8A8A;"
                      f"strokeWidth=1.3;fontFamily={FONT};fontSize={fs};fontColor=#333333;")

    def chip(text, x, y, w, h, fs=16):
        return d.cell(text, x, y, w, h,
                      f"shape=note;whiteSpace=wrap;html=1;size=14;fillColor=#FFFFFF;"
                      f"strokeColor=#8A8A8A;strokeWidth=1.2;fontFamily={FONT};fontSize={fs};"
                      f"fontColor=#444444;align=left;spacingLeft=8;")

    def tag(text, x, y, w=160):
        """Detached guard annotation: on the shared next-window loop the
        router cannot place the bracket reliably, so the guard sits at the
        branch stub."""
        return d.cell(text, x, y, w, 26,
                      f"text;html=1;align=center;verticalAlign=middle;fontFamily={FONT};"
                      f"fontSize=18;fontColor=#3F3F3F;")

    frame(440, 70, 1300, 1330, "Per-window evaluation", "each tier, every telemetry window")

    start = d.cell("", 991, 171, 18, 18,
                   f"ellipse;whiteSpace=wrap;html=1;fillColor=#000000;strokeColor=#000000;"
                   f"fontFamily={FONT};")
    d1 = diamond("(1) eligible to act?", 810, 240, 380, 120)
    chip("skipped when:|an infrastructure operation is in progress,|the tier is at its cap, or a "
         "scale-up|cooldown is active", 1280, 240, 440, 90)
    a2 = action_box("(2) compute the degradation score s(w)|from the normalised signals",
                    740, 420, 520, 110)
    d3 = diamond("(3) degradation sustained?|(\u2265 R of the last W eligible|windows reach "
                 "\u03b8(k))", 790, 590, 420, 150)
    e_up = action_box("emit scale-up request|(tier \u00b7 action \u00b7 evidence)",
                      1350, 620, 300, 90, fs=18)
    d4 = diamond("(4) idle conditions?|(both signals below their|idle thresholds)",
                 790, 810, 420, 150)
    chip("indeterminate windows|(latency above the ceiling)|do not count toward idleness",
         1270, 830, 420, 80)
    a4b = action_box("track idleness across the|scale-down window", 740, 1030, 520, 100)
    d4c = diamond("idleness sustained?|(spanning R\u2193 of the last|W\u2193 windows)",
                  790, 1200, 420, 150)
    e_dn = action_box("emit scale-down request|(tier \u00b7 action \u00b7 evidence)",
                      1350, 1230, 300, 90, fs=18)
    queue = obj("action queue", 1820, 880, 300, 90)

    edge(start, d1)
    edge(d1, a2, "[eligible]")
    edge(a2, d3)
    edge(d3, e_up, "[sustained]")
    edge(d3, d4, "[not sustained]")
    edge(d4, a4b, "[idle]")
    edge(a4b, d4c)
    edge(d4c, e_dn, "[sustained]")
    edge(e_up, queue, points=[(1720, 665), (1720, 925)])
    edge(e_dn, queue, points=[(1720, 1275), (1720, 925)])
    edge(d1, start, points=[(620, 300), (620, 180)])
    edge(d4, start, points=[(620, 885), (620, 180)])
    edge(d4c, start, "next window", points=[(620, 1275), (620, 180)])
    tag("[ineligible]", 640, 266)
    tag("[not idle]", 640, 851)
    tag("[not sustained]", 640, 1241, w=170)

    dio = d.write("decision_engine_flow.drawio")
    export(dio, OUT_PNG / "decision_engine_flow_v2.png")


if __name__ == "__main__":
    OUT_DIO.mkdir(parents=True, exist_ok=True)
    fig_architecture_overview()
    std_demand_to_capacity_chain()
    std_elastic_allocation()
    std_sdn_architecture()
    std_sdn_load_balancing()
    std_telemetry_delivery_paths()
    std_mongodb_replica_set()
    std_vip_routing_sequence()
    std_control_workflow()
    std_decision_engine_flow()
