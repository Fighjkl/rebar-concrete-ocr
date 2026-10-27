"""Adapters are discovered from a simple registry and optional entry-point plugins."""
from dataclasses import asdict
from importlib.metadata import entry_points
from material_ocr.materials.base import MaterialAdapter, MaterialInfo
from material_ocr.materials.rebar.adapter import RebarAdapter


class MaterialRegistry:
    def __init__(self, discover_plugins=True):
        self.adapters = {}
        self.planned = [MaterialInfo('steel_beam', '钢梁', False, '扩展示例：需接入截面、长度/重量与炉批号规则'),
                        MaterialInfo('concrete', '混凝土', False, '扩展示例：需接入强度、方量与生产批次规则')]
        self.register(RebarAdapter())
        if discover_plugins:
            for plugin in entry_points(group='material_ocr.materials'):
                self.register(plugin.load()())

    def register(self, adapter: MaterialAdapter):
        if adapter.info.id in self.adapters:
            raise ValueError('Duplicate material id: ' + adapter.info.id)
        self.adapters[adapter.info.id] = adapter

    def get(self, material_id):
        adapter = self.adapters.get(material_id)
        if adapter is None or not adapter.info.enabled:
            raise ValueError('该材料尚未接入识别和匹配：' + material_id)
        return adapter

    def list(self):
        known = [a.info for a in self.adapters.values()]
        return [asdict(x) for x in known + [p for p in self.planned if p.id not in self.adapters]]
