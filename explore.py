import gdsii.tags
from gdsii.library import Library
from gdsii.structure import Structure
from gdsii.record import Record
from gdsii.elements import _Base as ElementBase
from gdsii.elements import Text, Path, Boundary, SRef
from dataclasses import dataclass
import csv

import layerdata

"""
sky130 fd_sc_hd pdk (high density digital standard cells)

gdsii elements: https://www.artwork.com/gdsii/gdsii/
Used element types are Text, Path, Boundary, SRef

boundary:
    - elflags, plex, properties always always null or empty
    - used types [0, 4, 15, 16, 20, 23, 44]
path:
    - used types [20]
text:
    - types [5, 44, 59]
    - 64:5: sc power VPB
    - 64:59: sc power VNB
    - 67:5: sc pin labels
    - 68:5: sc power VGND/VPWR
    - 70:5: chip pin labels
    - 71:5: met4 power labels
    - 72:5: met5 power labels
    - 83:44: sc cell names

types:
    - 0: text, mask
    - 4: boundary (e.g. standard cell identifier boundary)
    - 15:
    - 16: pin
    - 20: drawing
    - 23: diode identifier, text
    - 44: drawing (contacts)

200:0 morse code easter egg (likely using INTERNAL_3 and INTERNAL_7 as blocks)

io pads on 70:16, text 70:5

layer/type definitions (gds_layers.csv): https://skywater-pdk.readthedocs.io/en/main/rules/layers.html


puzzle only uses these layers
Layer(name='met1', purpose='drawing, text', layer=68, data_type=20, description='Metal 1')
Layer(name='met2', purpose='drawing, text', layer=69, data_type=20, description='Metal 2')
Layer(name='met3', purpose='pin', layer=70, data_type=16, description='(Text and data)')
Layer(name='met3', purpose='drawing, text', layer=70, data_type=20, description='Metal 3')
Layer(name='met4', purpose='pin', layer=71, data_type=16, description='(Text and data)')
Layer(name='met4', purpose='drawing, text', layer=71, data_type=20, description='Metal 4')
Layer(name='met5', purpose='pin', layer=72, data_type=16, description='(Text and data)')
Layer(name='met5', purpose='drawing, text', layer=72, data_type=20, description='Metal 5')
Layer(name='prBndry', purpose='boundary', layer=235, data_type=4, description='')

vias use
VIA_M1M2_PR_MR
Layer(name='met1', purpose='drawing, text', layer=68, data_type=20, description='Metal 1')
Layer(name='via', purpose='drawing', layer=68, data_type=44, description='Contact from metal 1 to metal 2')
Layer(name='met2', purpose='drawing, text', layer=69, data_type=20, description='Metal 2')
VIA_M3M4_PR
Layer(name='met3', purpose='drawing, text', layer=70, data_type=20, description='Metal 3')
Layer(name='via3', purpose='drawing', layer=70, data_type=44, description='Contact from metal 3 to metal 4')
Layer(name='met4', purpose='drawing, text', layer=71, data_type=20, description='Metal 4')
VIA_M2M3_PR
Layer(name='met2', purpose='drawing, text', layer=69, data_type=20, description='Metal 2')
Layer(name='via2', purpose='drawing', layer=69, data_type=44, description='Contact from metal 2 to metal 3')
Layer(name='met3', purpose='drawing, text', layer=70, data_type=20, description='Metal 3')
VIA_M1M2_PR
Layer(name='met1', purpose='drawing, text', layer=68, data_type=20, description='Metal 1')
Layer(name='via', purpose='drawing', layer=68, data_type=44, description='Contact from metal 1 to metal 2')
Layer(name='met2', purpose='drawing, text', layer=69, data_type=20, description='Metal 2')
VIA_L1M1_PR_MR
Layer(name='li1', purpose='drawing, text', layer=67, data_type=20, description='Local interconnect')
Layer(name='mcon', purpose='drawing', layer=67, data_type=44, description='Contact from local interconnect to metal1')
Layer(name='met1', purpose='drawing, text', layer=68, data_type=20, description='Metal 1')
VIA_via2_3_2000_480_1_6_320_320
Layer(name='met1', purpose='drawing, text', layer=68, data_type=20, description='Metal 1')
Layer(name='via', purpose='drawing', layer=68, data_type=44, description='Contact from metal 1 to metal 2')
Layer(name='met2', purpose='drawing, text', layer=69, data_type=20, description='Metal 2')
VIA_via3_4_2000_480_1_5_400_400
Layer(name='met2', purpose='drawing, text', layer=69, data_type=20, description='Metal 2')
Layer(name='via2', purpose='drawing', layer=69, data_type=44, description='Contact from metal 2 to metal 3')
Layer(name='met3', purpose='drawing, text', layer=70, data_type=20, description='Metal 3')
VIA_via4_5_2000_480_1_5_400_400
Layer(name='met3', purpose='drawing, text', layer=70, data_type=20, description='Metal 3')
Layer(name='via3', purpose='drawing', layer=70, data_type=44, description='Contact from metal 3 to metal 4')
Layer(name='met4', purpose='drawing, text', layer=71, data_type=20, description='Metal 4')
VIA_via5_6_2000_2000_1_1_1600_1600
Layer(name='met4', purpose='drawing, text', layer=71, data_type=20, description='Metal 4')
Layer(name='via4', purpose='drawing', layer=71, data_type=44, description='Contact from metal 4 to metal 5')
Layer(name='met5', purpose='drawing, text', layer=72, data_type=20, description='Metal 5')


"""

with open('asic-puzzle-2026/warmup/04_final.gds', 'rb') as fin:
    lib = Library.load(fin)
# with open('asic-puzzle-2026/puzzle.gds', 'rb') as fin:
#     lib = Library.load(fin)
# with open('patched.gds', 'rb') as fin:
#     lib = Library.load(fin)


LAYERS = layerdata.LayerStore('gds_layers.csv')


TYPE_PIN = 16
SC_PIN_LABEL_LAYER = (67, 5)
SC_POWER_LABL_LAYERS = [
    (64, 5),
    (64, 59),
    (68, 5)
]
SC_CELL_LABEL_LAYER = (83, 44)


def show_boundary(e: Boundary) -> str:
    layer = LAYERS.find(e.layer, e.data_type)
    return f'Boundary {e.layer}:{e.data_type} {layer.name} ' + \
        (f'elflags={e.elflags} ' if e.elflags is not None else '') + \
        (f'plex={e.plex} ' if e.plex is not None else '') + \
        (f'properties={e.properties} ' if e.properties is not None and len(e.properties) > 0 else '') + \
        f'xy={e.xy}'

def show_text(e: Text) -> str:
    return f'Text "{e.string.decode()}" xy={e.xy}'

def show_sref(e: SRef) -> str:
    assert e.elflags is None
    assert e.properties is None or len(e.properties) == 0
    assert e.mag is None
    # assert e.strans is None
    assert e.angle is None or e.angle == 180.0
    return f'SRef {e.struct_name.decode()} xy={e.xy} strans={e.strans}, mag={e.mag}, angle={e.angle}'


SHOW_FUNCS: dict = {
    Boundary: show_boundary,
    Text: show_text,
    SRef: show_sref,
}

for s in lib:
    s: Structure
    print(s.name.decode())

    # don't care
    # if s.name.decode() in {'sky130_fd_sc_hd__tapvpwrvgnd_1'}:
    #     continue

    # if s.name.startswith(b'sky130'):
    #     sc = parse_standard_cell(s)
    #     print(sc)

    if s.name.decode().startswith('adder_demo'):
        seen = set()
        for e in s:
            e: ElementBase

            if isinstance(e, Path) or isinstance(e, Boundary):
                seen.add((e.layer, e.data_type))
            # func = SHOW_FUNCS.get(type(e), lambda x: str(x))
            # print('  ', func(e))
        # print(sorted(seen))
        for (l,t) in sorted(seen):
            print(LAYERS.find(l,t))


# x = Structure(name=b"testing")
# x.append(Boundary(layer=72, data_type=20, xy=[(0, 0), (0, 10000), (10000, 5000), (10000, 0), (0, 0)]))
# x.append(SRef(struct_name=b"puzzle", xy=[(0,0)]))

# lib.append(x)
# with open('patched.gds', 'wb') as fout:
#     lib.save(fout)
