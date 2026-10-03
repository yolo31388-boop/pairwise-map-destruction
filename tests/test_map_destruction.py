"""
地图破坏系统 - 验收测试

运行方式：python -m pytest tests/test_map_destruction.py -q
共 15 个测试用例：
01-03 apply_damage 伤害应用（阈值检查 / 伤害类型区分）
04-06 destroy_wall 墙壁破坏（移除碰撞体 / 生成碎片 / 破坏传播）
07-09 debris_physics 碎片物理（受力 / 地面碰撞 / 数量上限）
10-12 structural_integrity 结构完整性（悬空坍塌 / 应力薄弱先断 / 破坏后重算）
13-15 destruction_effect 破坏效果（裂痕变形 / 类型区分 / LOD）
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from map.destruction import MapDestruction, Wall, Debris


class TestMapDestruction:
    """地图破坏系统测试套件"""

    def setup_method(self):
        """每个测试前的初始化"""
        self.system = MapDestruction()

    # ---------- apply_damage：伤害应用 ----------

    def test_case_01(self):
        """伤害正确扣血且返回实际伤害值（子弹倍率 1.0）"""
        self.system.add_wall("w1", health=100.0)
        result = self.system.apply_damage("w1", 30.0, damage_type="bullet")
        assert result["damage"] == pytest.approx(30.0)
        assert result["remaining_health"] == pytest.approx(70.0)
        assert self.system.get_wall("w1").health == pytest.approx(70.0)

    def test_case_02(self):
        """血量到达破坏阈值后墙壁真正被破坏，不再保持完整"""
        self.system.add_wall("w1", health=50.0)
        result = self.system.apply_damage("w1", 60.0, damage_type="bullet")
        assert result["destroyed"] is True
        wall = self.system.get_wall("w1")
        assert wall.destroyed is True
        assert wall.health == 0.0

    def test_case_03(self):
        """不同伤害类型有不同倍率，子弹与爆炸伤害不等价"""
        self.system.add_wall("bullet_w", health=100.0)
        self.system.add_wall("blast_w", health=100.0)
        bullet_result = self.system.apply_damage("bullet_w", 20.0,
                                                 damage_type="bullet")
        blast_result = self.system.apply_damage("blast_w", 20.0,
                                                damage_type="explosive")
        assert blast_result["damage"] > bullet_result["damage"]
        # 爆炸 2.5 倍 -> 50 伤害；子弹 1 倍 -> 20 伤害
        assert blast_result["damage"] == pytest.approx(50.0)
        assert bullet_result["damage"] == pytest.approx(20.0)

    # ---------- destroy_wall：墙壁破坏 ----------

    def test_case_04(self):
        """墙壁破坏后碰撞体被移除，玩家可以通过洞口"""
        self.system.add_wall("w1", health=10.0)
        self.system.apply_damage("w1", 100.0, damage_type="explosive")
        wall = self.system.get_wall("w1")
        assert wall.has_collider is False
        assert wall.visible is False

    def test_case_05(self):
        """墙壁破坏后必须生成碎片，而不是直接凭空消失"""
        self.system.add_wall("w1", health=10.0)
        result = self.system.apply_damage("w1", 100.0, damage_type="explosive")
        assert len(result["debris"]) > 0
        assert len(self.system.debris) > 0
        assert all(d.wall_id == "w1" for d in self.system.debris)

    def test_case_06(self):
        """破坏向相邻墙壁传播，相邻墙壁受到溅射伤害"""
        self.system.add_wall("a", health=100.0, adjacent=["b"])
        self.system.add_wall("b", health=100.0, adjacent=["a"])
        result = self.system.apply_damage("a", 100.0, damage_type="explosive")
        assert "b" in result["collateral"]
        assert self.system.get_wall("b").health < 100.0

    # ---------- debris_physics：碎片物理 ----------

    def test_case_07(self):
        """碎片生成后被施加初速度，不会原地不动"""
        self.system.add_wall("w1", health=1.0,
                             position=(0.0, 5.0, 0.0))
        self.system.apply_damage("w1", 100.0, damage_type="explosive",
                                 position=(-5.0, 5.0, 0.0))
        assert len(self.system.debris) > 0
        before = [d.position for d in self.system.debris]
        self.system.update(0.1)
        after = [d.position for d in self.system.debris]
        assert any(b != a for b, a in zip(before, after))
        assert all((vx != 0.0 or vy != 0.0 or vz != 0.0)
                   for vx, vy, vz in (d.velocity for d in self.system.debris))

    def test_case_08(self):
        """碎片拥有碰撞体，受重力下落且不会穿过地面"""
        self.system.add_wall("w1", health=1.0, position=(0.0, 10.0, 0.0))
        self.system.apply_damage("w1", 100.0, damage_type="explosive")
        assert all(d.has_collider for d in self.system.debris)
        for _ in range(200):
            self.system.update(0.05)
        assert all(d.position[1] >= 0.0 for d in self.system.debris)

    def test_case_09(self):
        """碎片总数受配置上限限制，避免一次破坏产生上千碎片"""
        system = MapDestruction({"max_debris": 40, "debris_per_wall": 10})
        for i in range(100):
            system.add_wall(f"w{i}", health=1.0, position=(0.0, 0.0, 0.0))
        for i in range(100):
            system.apply_damage(f"w{i}", 100.0, damage_type="explosive")
        assert len(system.debris) <= 40

    # ---------- structural_integrity：结构完整性 ----------

    def test_case_10(self):
        """悬空且无支撑的墙壁在结构检查后会掉落，而不是飘在空中"""
        # 上层墙由下层墙支撑，且不落地
        self.system.add_wall("ground_floor", health=100.0,
                             position=(0.0, 0.0, 0.0))
        self.system.add_wall("upper_floor", health=100.0,
                             position=(0.0, 3.0, 0.0),
                             supported_by=["ground_floor"])
        # 直接破坏下层墙并跳过结构重算，再手动触发检查
        result = self.system.structural_integrity()
        assert all(w.supported for w in self.system.walls.values())
        self.system.get_wall("ground_floor").destroyed = True
        self.system.get_wall("ground_floor").has_collider = False
        result = self.system.structural_integrity()
        assert "upper_floor" in result["collapsed"]
        assert self.system.get_wall("upper_floor").destroyed is True

    def test_case_11(self):
        """应力集中时承载能力最薄弱（应力比最高）的墙先断裂"""
        # 一根柱子承载全部上层重量
        self.system.add_wall("pillar", health=1000.0, strength=5.0,
                             weight=1.0, position=(0.0, 0.0, 0.0))
        self.system.add_wall("heavy_top", health=1000.0, strength=1000.0,
                             weight=100.0, position=(0.0, 3.0, 0.0),
                             supported_by=["pillar"])
        result = self.system.structural_integrity()
        stresses = result["peak_stresses"]
        assert stresses["pillar"] > 1.0
        assert "pillar" in result["collapsed"]

    def test_case_12(self):
        """墙被炸掉后重新计算支撑，上方楼板不再飘着（端到端）"""
        self.system.add_wall("support", health=10.0, position=(0.0, 0.0, 0.0))
        self.system.add_wall("slab", health=1000.0, position=(0.0, 3.0, 0.0),
                             supported_by=["support"])
        self.system.apply_damage("support", 100.0, damage_type="explosive")
        assert self.system.get_wall("slab").destroyed is True
        assert self.system.get_wall("slab").has_collider is False

    # ---------- destruction_effect：破坏效果 ----------

    def test_case_13(self):
        """破坏效果包含裂痕与变形，而不是只有粒子"""
        self.system.add_wall("w1", health=100.0)
        effect = self.system.destruction_effect("w1", damage_type="explosive")
        assert "cracks" in effect
        assert "deformation" in effect
        assert effect["cracks"] >= 0.0
        # 受创后裂痕程度应增长
        self.system.apply_damage("w1", 60.0, damage_type="explosive")
        effect = self.system.destruction_effect("w1", damage_type="explosive")
        assert effect["cracks"] > 0.0
        assert effect["deformation"] > 0.0

    def test_case_14(self):
        """效果随伤害类型变化：火烧与爆炸的组成不同"""
        self.system.add_wall("fire_w", health=100.0)
        self.system.add_wall("blast_w", health=100.0)
        fire = self.system.destruction_effect("fire_w", damage_type="fire")
        blast = self.system.destruction_effect("blast_w",
                                               damage_type="explosive")
        assert fire["components"] != blast["components"]
        assert "flames" in fire["components"]
        assert "shockwave" in blast["components"]

    def test_case_15(self):
        """效果根据距离做 LOD，远处使用低精度、近处使用高精度"""
        self.system.add_wall("w1", health=100.0)
        near = self.system.destruction_effect("w1", damage_type="explosive",
                                              distance=2.0)
        mid = self.system.destruction_effect("w1", damage_type="explosive",
                                             distance=20.0)
        far = self.system.destruction_effect("w1", damage_type="explosive",
                                             distance=100.0)
        assert near["lod"] == "high"
        assert mid["lod"] == "medium"
        assert far["lod"] == "low"
        assert near["particles"] > mid["particles"] > far["particles"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
