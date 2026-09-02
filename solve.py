import gdsii.tags
from gdsii.library import Library
from gdsii.structure import Structure
from gdsii.record import Record
from gdsii.elements import _Base as ElementBase
from gdsii.elements import Text, Path, Boundary, SRef
from dataclasses import dataclass
from collections import defaultdict
import math
import shapely
from shapely import Polygon, MultiPolygon
from pprint import pprint
import numpy as np
import rtree
from tqdm import tqdm
import random
from enum import Enum
import glob
import json
import z3

import lefparser
import layerdata

random.seed(42)

LAYERS = layerdata.LayerStore('gds_layers.csv')
CELLS = lefparser.load_all_cells()

# with open('asic-puzzle-2026/warmup/04_final.gds', 'rb') as fin:
#     lib = Library.load(fin)
#     TOP_STRUCTURE = 'adder_demo'
#     CHIP_PINS = [
#         ['clk', 'rst_n', 'A', 'B', 'en'],
#         ['S']
#     ]
#     WIN_PIN = 'S'
with open('asic-puzzle-2026/puzzle.gds', 'rb') as fin:
    lib = Library.load(fin)
    TOP_STRUCTURE = 'puzzle'
    CHIP_PINS = [
        ['clk', 'rst_n', 'en', 'I'],
        ['success', 'O0', 'O1', 'O2', 'O3', 'O4', 'O5', 'O6', 'O7']
    ]
    WIN_PIN = 'success'

STRUCTURES: dict[str, Structure] = {s.name.decode():s for s in lib}


class UnionFind:
    def __init__(self, N: int) -> None:
        self.N = N
        self.parent = [x for x in range(N)]

    def find(self, x: int) -> int:
        if self.parent[x] == x:
            return x
        p = self.find(self.parent[x])
        self.parent[x] = p
        return p

    def merge(self, a: int, b: int) -> None:
        a = self.find(a)
        b = self.find(b)
        if a != b:
            self.parent[b] = a


class Labeler:
    def __init__(self) -> None:
        self._next_id = 1

    def get_next(self) -> int:
        res = self._next_id
        self._next_id += 1
        return res

REGION_LABELER = Labeler()

class Remapper:
    def __init__(self) -> None:
        self._remap = {}

    def remap(self, old_label: int, new_label: int) -> None:
        assert old_label not in self._remap
        if old_label != new_label:
            self._remap[old_label] = new_label

    def get(self, label: int) -> int:
        orig_label = label
        while label in self._remap:
            label = self._remap[label]
        # cache result
        if label != orig_label:
            self._remap[orig_label] = label
        return label

REGION_LABEL_MAPPER = Remapper()

class RegionLookup:
    def __init__(self) -> None:
        self._map: dict[int, Region] = {}

    def register(self, r: Region) -> None:
        assert r.label not in self._map
        self._map[r.label] = r

    def get(self, label: int) -> Region:
        label = REGION_LABEL_MAPPER.get(label)
        return self._map[label]

    def num_active(self) -> int:
        ctr = 0
        for r in self._map.values():
            if not r.deleted:
                ctr += 1
        return ctr

REGION_LOOKUP = RegionLookup()



class Region:
    """
    A (multilayer) interconnected region carrying a single signal
    """
    def __init__(self) -> None:
        self.deleted = False
        self.label = REGION_LABELER.get_next()
        self.polygons: dict[str, shapely.MultiPolygon] = {}
        self.compute_bounds()
        REGION_LOOKUP.register(self)

    def add_polygon(self, layer: str, shape: shapely.MultiPolygon) -> None:
        if layer in self.polygons:
            self.polygons[layer] = shapely.union(self.polygons[layer], shape)
        else:
            self.polygons[layer] = shape
        self.compute_bounds()

    def absorb(self, others: list[Region]) -> None:
        all_layers = set(self.polygons.keys())
        for o in others:
            for layer in o.polygons.keys():
                if layer not in all_layers:
                    all_layers.add(layer)

        for layer in all_layers:
            to_merge = [o.polygons[layer] for o in others if layer in o.polygons]
            if layer in self.polygons:
                to_merge.append(self.polygons[layer])
            self.polygons[layer] = shapely.union_all(to_merge)

        self.compute_bounds()

        for o in others:
            REGION_LABEL_MAPPER.remap(o.label, self.label)
            o.deleted = True

    def intersects(self, other: Region) -> bool:
        for layer in self.polygons.keys():
            if layer in other.polygons:
                if shapely.intersects(self.polygons[layer], other.polygons[layer]):
                    return True
        return False

    def compute_bounds(self):
        if len(self.polygons) == 0:
            self.bounds = [math.nan, math.nan, math.nan, math.nan]
        else:
            bounds = [p.bounds for p in self.polygons.values()]
            self.bounds = [
                min(b[0] for b in bounds),
                min(b[1] for b in bounds),
                max(b[2] for b in bounds),
                max(b[3] for b in bounds),
            ]

    def __str__(self) -> str:
        return f'Region(label={self.label} deleted={self.deleted} polygons={self.polygons})'


METAL_LAYERS = {'li1', 'met1', 'met2', 'met3', 'met4', 'met5'}
VIA_LAYERS = {'mcon', 'via', 'via2', 'via3', 'via4'}


@dataclass
class Transform:
    reflect_x: bool
    rotate_180: bool
    translate: tuple[int, int]

    def apply(self, point: tuple[int, int]) -> tuple[int, int]:
        if self.reflect_x:
            point = (point[0], -point[1])
        if self.rotate_180:
            point = (-point[0], -point[1])
        point = (point[0]+self.translate[0], point[1]+self.translate[1])
        return point

    def apply_to_polygon(self, geometry: MultiPolygon) -> MultiPolygon:

        def transform_polygon(polygon: Polygon) -> Polygon:
            exterior = [
                self.apply((int(x), int(y)))
                for x, y in polygon.exterior.coords
            ]

            interiors = [
                [
                    self.apply((int(x), int(y)))
                    for x, y in ring.coords
                ]
                for ring in polygon.interiors
            ]

            return Polygon(exterior, interiors)

        return MultiPolygon(
            transform_polygon(polygon)
            for polygon in geometry.geoms
        )


def parse_via(s: Structure, transform: Transform) -> Region:
    res = Region()
    assert s.name.startswith(b'VIA')
    for e in s:
        e: ElementBase
        assert isinstance(e, Boundary)

        layer = LAYERS.find(e.layer, e.data_type)
        assert layer.name in METAL_LAYERS or layer.name in VIA_LAYERS

        # no need to track the vias, we have metal squares
        if layer.name in VIA_LAYERS:
            continue

        # rectangle
        assert len(e.xy) == 5
        assert e.xy[0] == e.xy[4]

        points = [transform.apply(p) for p in e.xy[:-1]]
        shape = shapely.MultiPolygon([[points]])
        res.add_polygon(layer.name, shape)

    return res


class CellRegistry:
    def __init__(self) -> None:
        self.cells: set[str] = set()
        # (cell; pin) -> region
        self.pins: dict[tuple[str, str], int] = {}
        # (region label; center coordinates)
        self.chip_pins: set[tuple[int, tuple[int, int]]] = set()
        self._cell_ctrs: dict[str, int] = {}

    def register_cell(self, base_name: str) -> str:
        ctr = self._cell_ctrs.get(base_name, 0)
        self._cell_ctrs[base_name] = ctr + 1
        name = f'{base_name}_{ctr}'
        self.cells.add(name)
        return name

    def register_pin(self, cell_name: str, pin_name: str, region: int):
        assert cell_name in self.cells
        assert (cell_name, pin_name) not in self.pins
        self.pins[(cell_name, pin_name)] = REGION_LABEL_MAPPER.get(region)

    def register_chip_pin(self, region: int, center: tuple[int, int]):
        self.chip_pins.add((region, center))

CELL_REGISTRY = CellRegistry()


def parse_standard_cell(s: Structure, transform: Transform):
    assert s.name.startswith(b'sky130')
    cell = CELLS[s.name.decode()]

    # decoupling and power tap, don't care
    if cell.name in ['sky130_fd_sc_hd__decap_3', 'sky130_fd_sc_hd__tapvpwrvgnd_1']:
        return

    cell_name = CELL_REGISTRY.register_cell(cell.name.removeprefix('sky130_fd_sc_hd__'))

    for pin in cell.pins:
        # include signals only
        # if pin.typ == lefparser.LEF.PinType.POWER or pin.typ == lefparser.LEF.PinType.GROUND:
        #     continue

        r = Region()
        for port in pin.ports:
            shape = transform.apply_to_polygon(port.polygon)
            r.add_polygon(port.layer, shape)

        CELL_REGISTRY.register_pin(cell_name, pin.label, r.label)


def parse_puzzle_boundary(e: Boundary) -> Region | None:
    layer = LAYERS.find(e.layer, e.data_type)
    if layer.name == 'prBndry':
        return None
    assert layer.name in METAL_LAYERS

    res = Region()
    shape = shapely.MultiPolygon([[e.xy[:-1]]])
    res.add_polygon(layer.name, shape)

    TYPE_PIN = 16
    if e.data_type == TYPE_PIN:
        assert len(e.xy) == 5
        xx = set(coord[0] for coord in e.xy)
        yy = set(coord[1] for coord in e.xy)
        assert len(xx) == 2 and len(yy) == 2
        assert (max(xx)-min(xx)) % 2 == 0
        assert (max(yy)-min(yy)) % 2 == 0
        xy = (
            min(xx)+round((max(xx)-min(xx))/2),
            min(yy)+round((max(yy)-min(yy))/2),
        )
        CELL_REGISTRY.register_chip_pin(res.label, xy)

    return res


def gds_path_to_polygon(xy: list[tuple[int, int]], pathtype: int, width: int, bgn_extn: int, end_extn: int):
    assert len(xy) >= 2
    assert pathtype in (0, 2, 4)
    assert width % 2 == 0

    half_width = width / 2.0
    if pathtype == 0:
        bgn, end = 0.0, 0.0
    elif pathtype == 2:
        bgn, end = half_width, half_width
    else: # pathtype == 4
        bgn, end = bgn_extn, end_extn

    pts = np.array(xy, dtype=float)

    if bgn != 0.0:
        v_start = pts[1] - pts[0]
        u_start = v_start / np.linalg.norm(v_start)
        pts[0] = pts[0] - u_start * bgn

    if end != 0.0:
        v_end = pts[-1] - pts[-2]
        u_end = v_end / np.linalg.norm(v_end)
        pts[-1] = pts[-1] + u_end * end

    return shapely.MultiPolygon([shapely.geometry.LineString(pts).buffer(
        distance=half_width,
        cap_style='flat',
        join_style='mitre'
    )])


def parse_puzzle_path(e: Path) -> Region | None:
    layer = LAYERS.find(e.layer, e.data_type)
    assert layer.name in METAL_LAYERS
    assert e.path_type in {0,2,4}, f"{e.path_type}"
    assert e.width is not None

    shape = gds_path_to_polygon(
        xy=e.xy,
        pathtype=e.path_type,
        width=e.width,
        bgn_extn=e.bgn_extn if e.bgn_extn is not None else 0,
        end_extn=e.end_extn if e.end_extn is not None else 0,
    )

    r = Region()
    r.add_polygon(layer.name, shape)
    return r


def parse_reflect_bit(strans: int | None) -> bool:
    return strans is not None and ((strans & 32768) != 0)


def parse_puzzle_sref(e: SRef):
    assert e.mag is None
    assert e.angle is None or e.angle == 180
    reflect = parse_reflect_bit(e.strans)

    structure = STRUCTURES[e.struct_name.decode()]
    transform = Transform(
        reflect_x=reflect,
        rotate_180=(e.angle == 180),
        translate=e.xy[0]
    )

    if structure.name.startswith(b'VIA'):
        parse_via(structure, transform)
    elif structure.name.startswith(b'sky130'):
        parse_standard_cell(structure, transform)
    elif structure.name.startswith(b'INTERNAL'):
        pass
    else:
        assert False, f"unknown structure {structure.name}"


def parse_puzzle(s: Structure):
    for e in s:
        e: ElementBase

        if isinstance(e, Boundary):
            parse_puzzle_boundary(e)
            pass
        elif isinstance(e, Path):
            parse_puzzle_path(e)
            pass
        elif isinstance(e, SRef):
            parse_puzzle_sref(e)
        elif isinstance(e, Text):
            pass # don't care
        else:
            assert False


def build_merge_groups(to_merge: list[tuple[int, int]]) -> list[list[int]]:
    uf = UnionFind(N=REGION_LABELER._next_id)

    all_labels = set()
    for a,b in to_merge:
        uf.merge(a, b)
        all_labels.add(a)
        all_labels.add(b)

    by_group = defaultdict(lambda: [])
    for label in all_labels:
        parent = uf.find(label)
        by_group[parent].append(label)

    return list(by_group.values())


def join_regions():
    idx = rtree.index.Index()
    for label, region in REGION_LOOKUP._map.items():
        if not region.deleted:
            idx.insert(label, region.bounds)

    to_merge = []
    for label, region in REGION_LOOKUP._map.items():
        if not region.deleted:
            for x in idx.intersection(region.bounds):
                other = REGION_LOOKUP.get(x)
                if region.intersects(other):
                    to_merge.append((label, x))

    merge_groups = build_merge_groups(to_merge)
    for group in tqdm(merge_groups, desc='Merging regions'):
        parent = min(group)
        others = [REGION_LOOKUP.get(x) for x in group if x != parent]
        REGION_LOOKUP.get(parent).absorb(others)


def identify_chip_pins() -> tuple[dict[str, int], list[int]]:
    """
    (name -> region id; power regions)
    """
    region_ctrs = defaultdict(lambda: 0)
    for region,_ in CELL_REGISTRY.chip_pins:
        r = REGION_LABEL_MAPPER.get(region)
        region_ctrs[r] += 1

    # power pins span all power distribution columns, remove them
    power_regions = [x for x in region_ctrs.keys() if region_ctrs[x] > 1]
    assert len(power_regions) == 2

    xx = []
    for region,center in CELL_REGISTRY.chip_pins:
        r = REGION_LABEL_MAPPER.get(region)
        if r not in power_regions:
            xx.append(center[0])

    # two columns
    xx = sorted(set(xx))
    assert len(xx) == 2

    yy = [[], []]
    for region,center in CELL_REGISTRY.chip_pins:
        r = REGION_LABEL_MAPPER.get(region)
        if r not in power_regions:
            col = xx.index(center[0])
            yy[col].append(center[1])

    yy[0].sort()
    yy[1].sort()
    assert len(yy[0]) == len(CHIP_PINS[0])
    assert len(yy[1]) == len(CHIP_PINS[1])

    res = {}
    for region,center in CELL_REGISTRY.chip_pins:
        r = REGION_LABEL_MAPPER.get(region)
        if r not in power_regions:
            col = xx.index(center[0])
            row = len(yy[col]) - 1 - yy[col].index(center[1])
            label = CHIP_PINS[col][row]

            print(f'Pin {label} at {col}:{row} {center} region {r}')
            res[label] = r

    return res, power_regions


class CellDefinitions:
    def __init__(self) -> None:
        self.defs = {}
        basepath = 'skywater-pdk-libs-sky130_fd_sc_hd'
        for path in glob.glob(f'./{basepath}/cells/*/definition.json'):
            cell = path.split('/')[-2]
            with open(path) as fin:
                self.defs[cell] = json.load(fin)

    def get(self, name: str) -> dict:
        name = name.removeprefix('sky130_fd_sc_hd__')
        name = name.rsplit('_', 1)[0]
        return self.defs[name]

CELL_DEFINITIONS = CellDefinitions()


class WireLabel(int):
    NULL: "WireLabel"

class NodeLabel(int):
    NULL: "NodeLabel"

WireLabel.NULL = WireLabel(0)
NodeLabel.NULL = NodeLabel(0)

class NodeType(Enum):
    STANDARD_CELL = 0
    IO_PIN = 1
    POWER_PIN = 2

@dataclass
class Node:
    label: NodeLabel
    typ: NodeType
    name: str
    pins: dict[str, WireLabel]

@dataclass
class Wire:
    label: WireLabel
    region: int
    name: str
    nodes: list[tuple[NodeLabel, str]]

class Circuit:
    def __init__(self) -> None:
        self.nodes: dict[NodeLabel, Node] = {}
        self.wires: dict[WireLabel, Wire] = {}
        self.power_region: int
        self.ground_region: int
        self._next_node = 1
        self._next_wire = 1
        self._nodes_by_name: dict[str, NodeLabel] = {}
        self._wires_by_region: dict[int, WireLabel] = {}
        self._wires_by_name: dict[str, WireLabel] = {}

    def add_node(self, typ: NodeType, name: str) -> None:
        assert name not in self._nodes_by_name
        node = Node(
            label=NodeLabel(self._next_node),
            typ=typ,
            name=name,
            pins={},
        )
        self._next_node += 1
        self._nodes_by_name[name] = node.label
        self.nodes[node.label] = node

    def add_wire(self, node_pins: list[tuple[str, str]], region: int, name: str) -> None:
        assert REGION_LABEL_MAPPER.get(region) == region
        assert region not in self._wires_by_region
        assert name == '' or name not in self._wires_by_name
        node_labels = [
            (self.nodes[self._nodes_by_name[name]].label, pin)
            for name,pin in node_pins
        ]
        wire = Wire(
            label=WireLabel(self._next_wire),
            region=region,
            name=name,
            nodes=node_labels,
        )
        self._next_wire += 1
        self._wires_by_region[region] = wire.label
        self._wires_by_name[name] = wire.label
        self.wires[wire.label] = wire

        for name,pin in node_pins:
            node = self.nodes[self._nodes_by_name[name]].label
            assert pin not in self.nodes[node].pins
            self.nodes[node].pins[pin] = wire.label

    def delete_wire(self, label: WireLabel) -> None:
        wire = self.wires[label]
        for x,pin in wire.nodes:
            node = self.nodes[x]
            assert node.pins[pin] == label
            del node.pins[pin]
        del self._wires_by_region[wire.region]
        if wire.name != '':
            del self._wires_by_name[wire.name]
        del self.wires[label]

    def delete_node(self, label: NodeLabel) -> None:
        node = self.nodes[label]
        for pin,x in node.pins.items():
            wire = self.wires[x]
            wire.nodes = [
                (node_label,node_pin)
                for node_label, node_pin in wire.nodes
                if node_label != label or node_pin != pin
            ]
        del self._nodes_by_name[node.name]
        del self.nodes[label]

    def node(self, label: NodeLabel) -> Node:
        return self.nodes[label]

    def wire(self, label: WireLabel) -> Wire:
        return self.wires[label]

    def node_by_name(self, name: str) -> Node:
        return self.nodes[self._nodes_by_name[name]]

    def wire_by_name(self, name: str) -> Wire:
        return self.wires[self._wires_by_name[name]]


def build_circuit(chip_pins: dict[int, str]) -> Circuit:
    c = Circuit()

    for cell in sorted(CELL_REGISTRY.cells):
        c.add_node(NodeType.STANDARD_CELL, cell)

    pins_by_region: dict[int, list[tuple[str, str]]] = defaultdict(lambda: [])
    power_region = None
    ground_region = None
    for cell,pin in CELL_REGISTRY.pins.keys():
        r = CELL_REGISTRY.pins[(cell, pin)]
        r = REGION_LABEL_MAPPER.get(r)

        if pin == 'VPWR':
            assert power_region is None or power_region == r
            power_region = r
        elif pin == 'VGND':
            assert ground_region is None or ground_region == r
            ground_region = r

        # no need to track direct power connections
        # if pin not in ['VPWR', 'VGND']:
        pins_by_region[r].append((cell, pin))

    assert power_region is not None
    assert ground_region is not None
    c.power_region = power_region
    c.ground_region = ground_region
    print(f'{power_region=} {ground_region=}')

    for region, pins in pins_by_region.items():
        signal_name = chip_pins.get(region, '')
        if region == power_region:
            signal_name = 'PWR'
        if region == ground_region:
            signal_name = 'GND'

        c.add_wire(pins, region, signal_name)

    return c


def collapse_clock_tree(c: Circuit):
    input_clk = c.wire_by_name('clk')
    q = [input_clk]
    clk_pins: list[tuple[str, str]] = []
    clk_bufs: list[NodeLabel] = []
    clk_wires: set[WireLabel] = {q[0].label}

    while len(q) > 0:
        u, q = q[0], q[1:]
        for label,pin in u.nodes:
            node = c.node(label)
            clk_pins.append((node.name, pin))
            if node.name.startswith('clkbuf_') and pin == 'A':
                clk_bufs.append(label)
                clk_wires.add(node.pins['A'])
                clk_wires.add(node.pins['X'])
                q.append(c.wire(node.pins['X']))

    input_region = REGION_LOOKUP.get(input_clk.region)
    other_regions = [
        REGION_LOOKUP.get(c.wire(x).region)
        for x in clk_wires
        if c.wire(x).region != input_region.label
    ]
    input_region.absorb(other_regions)

    clk_pins = [
        (name,pin) for name,pin in clk_pins
        if not name.startswith('clkbuf_')
    ]

    for wire in clk_wires:
        c.delete_wire(wire)
    for node in clk_bufs:
        c.delete_node(node)
    c.add_wire(clk_pins, input_region.label, 'clk')


def to_cell_name(node_name: str) -> str:
    """
    Remove the counter suffix
    """
    return node_name.rsplit('_', 1)[0]


def get_cell_inputs(cell_name: str) -> list[str]:
    res = []
    for port in CELL_DEFINITIONS.get(cell_name)["ports"]:
        if port[0] == "signal" and port[2] == "input":
            res.append(port[1])
    return res


def get_cell_outputs(cell_name: str) -> list[str]:
    res = []
    for port in CELL_DEFINITIONS.get(cell_name)["ports"]:
        if port[0] == "signal" and port[2] == "output":
            res.append(port[1])
    return res


def is_cell_output(cell_name: str, pin: str) -> bool:
    for port in CELL_DEFINITIONS.get(cell_name)["ports"]:
        if port[1] == pin:
            return port[2] == "output"
    assert False, f'Pin {pin} not found'


def get_wire_driver(c: Circuit, w: WireLabel) -> tuple[NodeLabel, str] | None:
    wire = c.wire(w)
    res = None
    for node,pin in wire.nodes:
        if is_cell_output(to_cell_name(c.node(node).name), pin):
            assert res is None
            res = (node, pin)
    return res


def is_memory(node: Node) -> bool:
    return 'CLK' in node.pins.keys()


def z3_node(node: Node, pin: str, timestep: int) -> z3.BoolRef:
    assert timestep >= 0
    return z3.Bool(f'C-{node.name}-{pin}-{timestep}')


def z3_pin(name: str, timestep: int) -> z3.BoolRef:
    return z3.Bool(f'PIN-{name}-{timestep}')


def get_z3_outputs(
    node: Node,
    inputs: dict[str, z3.BoolRef],
    inputs_dly: dict[str, z3.BoolRef],
) -> dict[str, z3.BoolRef]:
    cell_name = to_cell_name(node.name)
    i = inputs
    d = inputs_dly

    # cells from warmup, by hand
    match cell_name:
        case 'a21bo_2': return {'X': (i['A1'] & i['A2']) | ~i['B1_N']}
        case 'a21boi_2': return {'Y': ~((i['A1'] & i['A2']) | ~i['B1_N'])}
        case 'a21o_2': return {'X': (i['A1'] & i['A2']) | i['B1']}
        case 'a31o_2': return {'X': (i['A1'] & i['A2'] & i['A3']) | i['B1']}
        case 'and2_2': return {'X': i['A'] & i['B']}
        case 'and3_2': return {'X': i['A'] & i['B'] & i['C']}
        case 'and4bb_2': return {'X': ~i['A_N'] & ~i['B_N'] & i['C'] & i['D']}
        case 'dfrtp_2': return {'Q': d['RESET_B'] & d['D']}
        case 'mux2_1': return {'X': (i['A0'] & ~i['S']) | (i['A1'] & i['S'])}
        case 'nand2_2': return {'Y': ~(i['A'] & i['B'])}
        case 'nor2_2': return {'Y': ~(i['A'] | i['B'])}
        case 'o21bai_2': return {'Y': ~((i['A1'] | i['A2']) & ~i['B1_N'])}
        case 'or2_2': return {'X': i['A'] | i['B']}
        case 'xnor2_2': return {'Y': ~(i['A'] ^ i['B'])}
        case 'xor2_2': return {'X': i['A'] ^ i['B']}

    # more cells by hand
    match cell_name:
        case 'and2b_2': return {'X': ~i['A_N'] & i['B']}
        case 'and3b_2': return {'X': ~i['A_N'] & i['B'] & i['C']}
        case 'and4_2': return {'X': i['A'] & i['B'] & i['C'] & i['D']}
        case 'and4b_2': return {'X': ~i['A_N'] & i['B'] & i['C'] & i['D']}
        case 'buf_2': return {'X': i['A']}
        case 'conb_1': return {'LO': z3.BoolVal(False), 'HI': z3.BoolVal(True)}
        case 'diode_2': return {}
        case 'dfstp_2': return {'Q': ~d['SET_B'] | d['D']}
        case 'dfxtp_2': return {'Q': d['D']}
        case 'inv_2': return {'Y': ~i['A']}
        case 'or3_2': return {'X': i['A'] | i['B'] | i['C']}
        case 'or3b_2': return {'X': i['A'] | i['B'] | ~i['C_N']}
        case 'or4_2': return {'X': i['A'] | i['B'] | i['C'] | i['D']}
        case 'or4b_2': return {'X': i['A'] | i['B'] | i['C'] | ~i['D_N']}
        case 'or4bb_2': return {'X': i['A'] | i['B'] | ~i['C_N'] | ~i['D_N']}
        case 'nand2b_2': return {'Y': ~(~i['A_N'] & i['B'])}
        case 'nand3_2': return {'Y': ~(i['A'] & i['B'] & i['C'])}
        case 'nand3b_2': return {'Y': ~(~i['A_N'] & i['B'] & i['C'])}
        case 'nand4_2': return {'Y': ~(i['A'] & i['B'] & i['C'] & i['D'])}
        case 'nor4b_2': return {'Y': ~(i['A'] | i['B'] | i['C'] | ~i['D_N'])}

    # bunch of find replace from the sky130 equations
    match cell_name:
        case 'a2111oi_2':  return {'Y': ~((i['A1'] & i['A2']) | i['B1'] | i['C1'] | i['D1'])          }
        case 'a211o_2':    return {'X': ((i['A1'] & i['A2']) | i['B1'] | i['C1'])                }
        case 'a211oi_2':   return {'Y': ~((i['A1'] & i['A2']) | i['B1'] | i['C1'])               }
        case 'a21bo_2':    return {'X': ((i['A1'] & i['A2']) | (~i['B1_N']))                }
        case 'a21boi_2':   return {'Y': ~((i['A1'] & i['A2']) | (~i['B1_N']))               }
        case 'a21o_2':     return {'X': ((i['A1'] & i['A2']) | i['B1'])                     }
        case 'a21oi_2':    return {'Y': ~((i['A1'] & i['A2']) | i['B1'])                    }
        case 'a221o_2':    return {'X': ((i['A1'] & i['A2']) | (i['B1'] & i['B2']) | i['C1'])         }
        case 'a221oi_2':   return {'Y': ~((i['A1'] & i['A2']) | (i['B1'] & i['B2']) | i['C1'])        }
        case 'a22o_2':     return {'X': ((i['A1'] & i['A2']) | (i['B1'] & i['B2']))              }
        case 'a22oi_2':    return {'Y': ~((i['A1'] & i['A2']) | (i['B1'] & i['B2']))             }
        case 'a311o_2':    return {'X': ((i['A1'] & i['A2'] & i['A3']) | i['B1'] | i['C1'])           }
        case 'a31o_2':     return {'X': ((i['A1'] & i['A2'] & i['A3']) | i['B1'])                }
        case 'a31oi_2':    return {'Y': ~((i['A1'] & i['A2'] & i['A3']) | i['B1'])               }
        case 'a32o_2':     return {'X': ((i['A1'] & i['A2'] & i['A3']) | (i['B1'] & i['B2']))         }
        case 'a41oi_2':    return {'Y': ~((i['A1'] & i['A2'] & i['A3'] & i['A4']) | i['B1'])          }
        case 'nor3_2':     return {'Y': ~(i['A'] | i['B'] | i['C'])                    }
        case 'nor3b_2':    return {'Y': ~(i['A'] | i['B'] | ~i['C_N'])                      }
        case 'nor4_2':     return {'Y': ~(i['A'] | i['B'] | i['C'] | i['D'])                     }
        case 'o211a_2':    return {'X': ((i['A1'] | i['A2']) & i['B1'] & i['C1'])                }
        case 'o211ai_2':   return {'Y': ~((i['A1'] | i['A2']) & i['B1'] & i['C1'])               }
        case 'o21a_2':     return {'X': ((i['A1'] | i['A2']) & i['B1'])                     }
        case 'o21ai_2':    return {'Y': ~((i['A1'] | i['A2']) & i['B1'])                    }
        case 'o21ba_2':    return {'X': ((i['A1'] | i['A2']) & ~i['B1_N'])                  }
        case 'o21bai_2':   return {'Y': ~((i['A1'] | i['A2']) & ~i['B1_N'])                 }
        case 'o221a_2':    return {'X': ((i['A1'] | i['A2']) & (i['B1'] | i['B2']) & i['C1'])         }
        case 'o22a_2':     return {'X': ((i['A1'] | i['A2']) & (i['B1'] | i['B2']))              }
        case 'o22ai_2':    return {'Y': ~((i['A1'] | i['A2']) & (i['B1'] | i['B2']))             }
        # BRUH why are _N inputs not inverted here
        case 'o2bb2a_2':   return {'X': (~(i['A1_N'] & i['A2_N']) & (i['B1'] | i['B2']))             }
        case 'o311a_2':    return {'X': ((i['A1'] | i['A2'] | i['A3']) & i['B1'] & i['C1'])           }
        case 'o31a_2':     return {'X': ((i['A1'] | i['A2'] | i['A3']) & i['B1'])                }
        case 'o31ai_2':    return {'Y': ~((i['A1'] | i['A2'] | i['A3']) & i['B1'])               }
        case 'o32a_2':     return {'X': ((i['A1'] | i['A2'] | i['A3']) & (i['B1'] | i['B2']))         }
        case 'o32ai_2':    return {'Y': ~((i['A1'] | i['A2'] | i['A3']) & (i['B1'] | i['B2']))        }
        case 'xnor2_2':    return {'Y': ~(i['A'] ^ i['B'])                             }
        case 'xor2_2':     return {'X': i['A'] ^ i['B']                                }

    assert False, f'Unmatched cell {cell_name}'


def to_z3(c: Circuit, chip_pins: list[str], timestep: int) -> tuple[dict[str, z3.BoolRef], list[tuple[z3.BoolRef, z3.BoolRef]]]:
    """
    Returns list of equalities to satisfy
    """
    SKIP_WIRES = ['PWR', 'GND', 'clk']
    SKIP_PINS = ['CLK']

    res = []

    wire_drivers: dict[WireLabel, z3.BoolRef] = {}
    wire_drivers_dly: dict[WireLabel, z3.BoolRef] = {}
    for label,wire in c.wires.items():
        if wire.name in SKIP_WIRES:
            continue
        driver = get_wire_driver(c, label)
        if driver is not None:
            node, pin = c.node(driver[0]), driver[1]
            expr = z3_node(node, pin, timestep)
            expr_dly = z3_node(node, pin, timestep-1)
        else:
            assert wire.name != '', "Net without driver must be chip pin"
            expr = z3_pin(wire.name, timestep)
            expr_dly = z3_pin(wire.name, timestep-1)
        wire_drivers[label] = expr
        wire_drivers_dly[label] = expr_dly

    pin_exprs = {
        pin:wire_drivers[c.wire_by_name(pin).label]
        for pin in chip_pins
    }

    for label,node in c.nodes.items():
        input_names = get_cell_inputs(to_cell_name(node.name))
        inputs = {}
        inputs_dly = {}
        for pin,wire in node.pins.items():
            if pin in SKIP_PINS or pin not in input_names:
                continue
            inputs[pin] = wire_drivers[wire]
            inputs_dly[pin] = wire_drivers_dly[wire]
        # print(node.name, inputs)

        outputs = get_z3_outputs(node, inputs, inputs_dly)
        # print(node.name, outputs)

        for pin, expr in outputs.items():
            assert pin in node.pins
            pin_expr = z3_node(node, pin, timestep)
            res.append((pin_expr, expr))

    return pin_exprs, res


def to_z3_unrolled(s: z3.Solver, c: Circuit, nsteps: int):
    s.add(z3_pin('rst_n', 0) == False)
    s.add(z3_pin('en', 0) == False)

    win = []
    output_pins = CHIP_PINS[1]

    for timestep in range(1, nsteps):
        pin_exprs, exprs = to_z3(c, output_pins, timestep)

        # make pins queriable
        for name,e in pin_exprs.items():
            s.add(z3_pin(name, timestep) == e)
        # collect win conditions
        win.append(pin_exprs[WIN_PIN])

        for a,b in exprs:
            s.add(a == b)

        if timestep == 1:
            s.add(z3_pin('rst_n', timestep) == False)
        else:
            s.add(z3_pin('rst_n', timestep) == True)

        s.add(z3_pin('en', timestep) == True)

    s.add(z3.Or(win) == True)


def solve(c: Circuit):
    solvesteps = 150
    s = z3.Solver()
    to_z3_unrolled(s, c, nsteps=solvesteps)

    with open('expr.smt2', 'w') as fout:
        fout.write(s.sexpr())

    assert s.check() == z3.sat
    m = s.model()

    solution = [False] # first iteration doesn't matter, chip is in reset

    for timestep in range(1, solvesteps):
        # print(
        #     1 if m.eval(z3_pin('A', timestep), model_completion=True).py_value() else 0,
        #     1 if m.eval(z3_pin('B', timestep), model_completion=True).py_value() else 0,
        #     1 if m.eval(z3_pin('S', timestep), model_completion=True).py_value() else 0
        # )
        # print(
        #     1 if m.eval(z3_pin('I', timestep), model_completion=True).py_value() else 0,
        #     1 if m.eval(z3_pin('success', timestep), model_completion=True).py_value() else 0,
        # )
        solution.append(m.eval(z3_pin('I', timestep), model_completion=True).py_value())

    print('sol    ', ''.join(['1' if x else '0' for x in solution]))

    # now constrain the input and simulate to get the output bits
    # simsteps = 150
    # s = z3.Solver()
    # to_z3_unrolled(s, c, simsteps)

    # for timestep in range(simsteps):
    #     if timestep < len(solution):
    #         s.add(z3_pin('I', timestep) == solution[timestep])
    #     else:
    #         s.add(z3_pin('I', timestep) == False)

    # assert s.check() == z3.sat
    # m = s.model()

    output_pins = ['success', 'O0', 'O1', 'O2', 'O3', 'O4', 'O5', 'O6', 'O7']
    outputs = {}
    for pin in output_pins:
        x = []
        for timestep in range(solvesteps):
            x.append(1 if m.eval(z3_pin(pin, timestep), model_completion=True).py_value() else 0)
        print(f'{pin:7}', ''.join(str(y) for y in x))
        outputs[pin] = x

    flag = []
    for i in range(solvesteps):
        v = 0
        for pin in ['O0', 'O1', 'O2', 'O3', 'O4', 'O5', 'O6', 'O7'][::-1]:
            v = (v << 1) | outputs[pin][i]
        flag.append(v)
    print('Flag:', bytes(flag).strip().decode())


parse_puzzle(STRUCTURES[TOP_STRUCTURE])
# print(REGION_LOOKUP.num_active())
join_regions()
# print(REGION_LOOKUP.num_active())
chip_pins, power_regions = identify_chip_pins()
c = build_circuit({l:r for r,l in chip_pins.items()})
collapse_clock_tree(c)
# print(REGION_LOOKUP.num_active())
solve(c)


"""
sol     000000000101010000100000000000010101010000000000001010000001000001000000100000101000010000000100000010000010010001010000000000000000001000000000000000
success 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000011111111111111111111111111
O0      000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001101010100100000000000
O1      000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001001101001101000000000000
O2      000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000011100100000000000000000
O3      000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000011000100000001100000000000
O4      000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000011001101100000000000000
O5      000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000011100010000011100000000000
O6      000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000011101111100000000000000
O7      000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000
Flag: (* TWO STARS *)
"""


# for l,x in c.nodes.items():
#     print(l, x)
# for l,x in c.wires.items():
#     print(l, x)
#     print(f'  <- {get_wire_driver(c, l)}')


# for name in sorted(x.name for x in c.nodes.values()):
#     print(f'{name:10}', is_memory(c.node_by_name(name)))


# for cell_name in sorted(set(to_cell_name(x.name) for x in c.nodes.values())):
#     d = CELL_DEFINITIONS.get(cell_name)
#     p = ' '.join(x[1] for x in d['ports'] if x[0] == 'signal')
#     print(f'{cell_name:10} {p:20} {d.get("equation", "UNK"):40} {d.get("description", "UNK")}')
#     # print(get_cell_outputs(cell_name))



from renderer import draw_regions_svg

draw_regions_svg(
    REGION_LOOKUP,
    output_path="regions.svg",
    scale=20,
    opacity=0.4,
    color_by_layer=False,
    color_override_by_region={
        # c.wire(WireLabel(590)).region: (255, 255, 255)
        # x: (255,255,255) for x in power_regions
    }
)
