from dataclasses import dataclass
from enum import Enum
from pprint import pprint
import shapely

puzzle_structures = """
sky130_fd_sc_hd__a2111oi_2
sky130_fd_sc_hd__a211o_2
sky130_fd_sc_hd__a211oi_2
sky130_fd_sc_hd__a21bo_2
sky130_fd_sc_hd__a21boi_2
sky130_fd_sc_hd__a21o_2
sky130_fd_sc_hd__a21oi_2
sky130_fd_sc_hd__a221o_2
sky130_fd_sc_hd__a221oi_2
sky130_fd_sc_hd__a22o_2
sky130_fd_sc_hd__a22oi_2
sky130_fd_sc_hd__a311o_2
sky130_fd_sc_hd__a31o_2
sky130_fd_sc_hd__a31oi_2
sky130_fd_sc_hd__a32o_2
sky130_fd_sc_hd__a41oi_2
sky130_fd_sc_hd__and2_2
sky130_fd_sc_hd__and2b_2
sky130_fd_sc_hd__and3_2
sky130_fd_sc_hd__and3b_2
sky130_fd_sc_hd__and4_2
sky130_fd_sc_hd__and4b_2
sky130_fd_sc_hd__and4bb_2
sky130_fd_sc_hd__buf_2
sky130_fd_sc_hd__clkbuf_16
sky130_fd_sc_hd__clkbuf_4
sky130_fd_sc_hd__clkbuf_8
sky130_fd_sc_hd__conb_1
sky130_fd_sc_hd__decap_3
sky130_fd_sc_hd__dfrtp_2
sky130_fd_sc_hd__dfstp_2
sky130_fd_sc_hd__dfxtp_2
sky130_fd_sc_hd__diode_2
sky130_fd_sc_hd__inv_2
sky130_fd_sc_hd__mux2_1
sky130_fd_sc_hd__nand2_2
sky130_fd_sc_hd__nand2b_2
sky130_fd_sc_hd__nand3_2
sky130_fd_sc_hd__nand3b_2
sky130_fd_sc_hd__nand4_2
sky130_fd_sc_hd__nor2_2
sky130_fd_sc_hd__nor3_2
sky130_fd_sc_hd__nor3b_2
sky130_fd_sc_hd__nor4_2
sky130_fd_sc_hd__nor4b_2
sky130_fd_sc_hd__o211a_2
sky130_fd_sc_hd__o211ai_2
sky130_fd_sc_hd__o21a_2
sky130_fd_sc_hd__o21ai_2
sky130_fd_sc_hd__o21ba_2
sky130_fd_sc_hd__o21bai_2
sky130_fd_sc_hd__o221a_2
sky130_fd_sc_hd__o22a_2
sky130_fd_sc_hd__o22ai_2
sky130_fd_sc_hd__o2bb2a_2
sky130_fd_sc_hd__o311a_2
sky130_fd_sc_hd__o31a_2
sky130_fd_sc_hd__o31ai_2
sky130_fd_sc_hd__o32a_2
sky130_fd_sc_hd__o32ai_2
sky130_fd_sc_hd__or2_2
sky130_fd_sc_hd__or3_2
sky130_fd_sc_hd__or3b_2
sky130_fd_sc_hd__or4_2
sky130_fd_sc_hd__or4b_2
sky130_fd_sc_hd__or4bb_2
sky130_fd_sc_hd__tapvpwrvgnd_1
sky130_fd_sc_hd__xnor2_2
sky130_fd_sc_hd__xor2_2
"""

SCALING_FACTOR = 1000


@dataclass
class LEF:
    class Direction(Enum):
        INPUT = 0
        OUTPUT = 1
        BIDIR = 2

    class PinType(Enum):
        SIGNAL = 0
        CLOCK = 1
        POWER = 2
        GROUND = 3

    @dataclass
    class Port:
        layer: str
        polygon: shapely.MultiPolygon

    @dataclass
    class Pin:
        label: str
        typ: LEF.PinType
        direction: LEF.Direction
        ports: list[LEF.Port]

    name: str
    size: tuple[int, int]
    pins: list[Pin]


class LineReader:
    def __init__(self, path: str) -> None:
        with open(path, 'r') as fin:
            self.lines = fin.readlines()
        self.idx = 0

    def done(self) -> bool:
        return self.idx == len(self.lines)

    def peek(self) -> str:
        assert not self.done()
        return self.lines[self.idx]

    def next(self) -> str:
        res = self.peek()
        self.idx += 1
        return res


def is_comment(line: str) -> bool:
    return line.strip().startswith('#')


def parse_number(s: str) -> int:
    x = float(s.strip()) * SCALING_FACTOR
    res = round(x)
    assert abs(x - res) < 0.000001
    return res

assert parse_number('0.000000') == 0
assert parse_number('3.680000') == 3680
assert parse_number('-0.240000') == -240


IGNORE_LAYERS = ["nwell", "pwell"]
IGNORE_PINS = ["VNB", "VPB"]


def parse_lef(path: str) -> LEF:
    lines = LineReader(path)

    name = None
    size = None
    pins = []

    curr_pin = None
    curr_direction = None
    curr_ports = []
    curr_typ = None
    curr_parsing_port = False
    curr_parsing_obs = False
    curr_port_layer = None
    curr_port_rects = []

    def close_port():
        nonlocal curr_port_layer, curr_port_rects
        nonlocal curr_parsing_port, curr_ports
        assert curr_port_layer is not None
        assert len(curr_port_rects) > 0
        if curr_port_layer not in IGNORE_LAYERS:
            shape = shapely.set_precision(
                shapely.MultiPolygon(curr_port_rects),
                0.1,
            )

            if curr_port_layer == 'mcon':
                # this connects li1 and met1, just put them there
                target_layers = ['li1', 'met1']
            else:
                target_layers = [curr_port_layer]

            for layer in target_layers:
                curr_ports.append(LEF.Port(
                    layer=layer,
                    polygon=shape,
                ))
        curr_parsing_port = False
        curr_port_layer = None
        curr_port_rects = []

    while True:
        line = lines.next().strip()
        # print(line)
        if is_comment(line) or line == '':
            continue

        tok = line.split()

        match tok[0]:
            case 'VERSION' | 'NAMESCASESENSITIVE' | 'BUSBITCHARS' | 'DIVIDERCHAR':
                pass

            case 'MACRO':
                assert name is None
                name = tok[1]
            case 'CLASS' | 'SOURCE' | 'SYMMETRY' | 'SITE' | 'NOWIREEXTENSIONATPIN' | 'FOREIGN':
                pass
            case 'ORIGIN':
                x, y = parse_number(tok[1]), parse_number(tok[2])
                assert x == 0 and y == 0, f'Invalid origin {x} {y}'
            case 'SIZE':
                assert size is None
                x, y = parse_number(tok[1]), parse_number(tok[3])
                size = (x, y)

            case 'PIN':
                assert curr_pin is None
                curr_pin = tok[1]
                # print(f'Starting pin {curr_pin}')
            case 'ANTENNAGATEAREA' | 'ANTENNADIFFAREA' | 'SHAPE':
                pass
            case 'DIRECTION':
                assert curr_pin is not None and curr_direction is None
                curr_direction = {
                    'INPUT': LEF.Direction.INPUT,
                    'OUTPUT': LEF.Direction.OUTPUT,
                    'INOUT': LEF.Direction.BIDIR,
                }[tok[1]]
            case 'USE':
                curr_typ = {
                    'SIGNAL': LEF.PinType.SIGNAL,
                    'CLOCK': LEF.PinType.CLOCK,
                    'POWER': LEF.PinType.POWER,
                    'GROUND': LEF.PinType.GROUND,
                }[tok[1]]

            case 'PORT':
                curr_parsing_port = True
            case 'LAYER':
                if curr_parsing_obs:
                    continue
                assert curr_parsing_port
                if curr_port_layer is not None:
                    # print('force close')
                    close_port()
                    curr_parsing_port = True
                curr_port_layer = tok[1]
            case 'RECT':
                if curr_parsing_obs:
                    continue
                xa, ya, xb, yb = list(map(parse_number, tok[1:5]))
                assert xa < xb
                assert ya < yb
                points = [
                    (xa, ya),
                    (xb, ya),
                    (xb, yb),
                    (xa, yb),
                ]
                curr_port_rects.append(shapely.Polygon(points))

            case 'OBS':
                assert not curr_parsing_port
                assert curr_pin is None
                curr_parsing_obs = True

            case 'END':
                if curr_parsing_obs:
                    curr_parsing_obs = False

                elif curr_parsing_port:
                    close_port()

                elif curr_pin is not None:
                    assert tok[1] == curr_pin
                    assert curr_direction is not None
                    assert curr_typ is not None
                    if curr_pin not in IGNORE_PINS:
                        assert len(curr_ports) > 0
                        pins.append(LEF.Pin(
                            label=curr_pin,
                            typ=curr_typ,
                            direction=curr_direction,
                            ports=curr_ports,
                        ))
                    curr_pin = None
                    curr_direction = None
                    curr_ports = []
                    curr_typ = None

                else:
                    assert tok[1] in {name, 'LIBRARY'}
                    if tok[1] == 'LIBRARY':
                        break

            case _:
                assert False, f'Unknown token {tok[0]}'


    assert name is not None
    assert size is not None
    assert len(pins) > 0
    return LEF(name, size, pins)


def load_all_cells(basepath: str = 'skywater-pdk-libs-sky130_fd_sc_hd') -> dict[str, LEF]:
    res = {}
    for name in puzzle_structures.splitlines():
        if name != '':
            # print(name)
            cell_name = name.split('__')[1].rsplit('_', 1)[0]
            path = f'./{basepath}/cells/{cell_name}/{name}.magic.lef'
            res[name] = parse_lef(path)
    return res


if __name__ == '__main__':
    for x in sorted(puzzle_structures.splitlines()):
        print(x)
    # for name, cell in load_all_cells().items():
    #     print(name)
    #     pprint(cell)
