from dataclasses import dataclass
import csv


@dataclass
class Layer:
    name: str
    purpose: str
    layer: int
    data_type: int
    description: str


class LayerStore:
    def __init__(self, path: str) -> None:
        with open(path, 'r') as fin:
            reader = csv.DictReader(fin)

            self.layers = {
                (200, 0): Layer(
                    name='easteregg',
                    purpose='',
                    layer=200,
                    data_type=0,
                    description='morse code easter egg layer'
                ),
                # undocumented in the csv
                (236, 0): Layer(
                    name='outline',
                    purpose='',
                    layer=236,
                    data_type=0,
                    description='Sky130 outline layer'
                )
            }

            for row in reader:
                if row['GDS layer:datatype'] == '':
                    continue
                layer, data_type = map(int, row['GDS layer:datatype'].split(':'))
                assert (layer, data_type) not in self.layers
                self.layers[(layer, data_type)] = Layer(
                    name=row['Layer name'],
                    purpose=row['Purpose'],
                    layer=layer,
                    data_type=data_type,
                    description=row['Description']
                )

    def find(self, layer: int, data_type: int) -> Layer:
        return self.layers[(layer, data_type)]

    def by_name(self, name: str) -> Layer:
        for l in self.layers.values():
            if l.name == name:
                return l
        assert False, f'Layer {name} not found'
