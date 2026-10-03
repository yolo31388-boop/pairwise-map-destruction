"""
地图破坏系统 - 验收测试

运行方式：python -m pytest tests/test_map_destruction.py -q
共 15 个测试用例
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from map.destruction import (
    MapDestruction, DEFAULT_MAX_DEBRIS, DEBRIS_PER_WALL,
)


class TestMapDestruction:
    """地图破坏系统测试套件"""

    def setup_method(self):
        """每个测试前的初始化"""
        self.system = MapDestruction()

    # ---- 伤害应用 ----
    def test_case_01(self):
        """apply_damage 按倍率扣除墙壁血量"""
        self.system.add_wall("w1", health=100.0)
        result = self.system.apply_damage("w1", 30.0, "bullet")
        assert result["applied"] == 30.0
        assert self.system.walls["w1"].health == 70.0
        assert result["destroyed"] is False

    def test_case_02(self):
        """伤害类型有不同倍率：爆炸 > 子弹 > 火焰"""
        self.system.add_wall("a", health=1000.0)
        self.system.add_wall("b", health=1000.0)
        self.system.add_wall("c", health=1000.0)
        self.system.apply_damage("a", 20.0, "fire")
        self.system.apply_damage("b", 20.0, "bullet")
        self.system.apply_damage("c", 20.0, "explosion")
        assert self.system.walls["c"].health < self.system.walls["b"].health
        assert self.system.walls["b"].health < self.system.walls["a"].health

    def test_case_03(self):
        """血量降到 0 时检查破坏阈值并触发破坏"""
        self.system.add_wall("w", health=50.0)
        result = self.system.apply_damage("w", 50.0, "bullet")
        assert result["destroyed"] is True
        assert self.system.walls["w"].destroyed is True
        assert self.system.walls["w"].health == 0.0

    # ---- 墙壁破坏 ----
    def test_case_04(self):
        """破坏后移除碰撞体，玩家可以穿过洞口"""
        self.system.add_wall("w", health=50.0)
        assert self.system.can_pass("w") is False
        self.system.apply_damage("w", 100.0, "explosion")
        assert self.system.walls["w"].has_collider is False
        assert self.system.can_pass("w") is True

    def test_case_05(self):
        """墙壁破坏后生成碎片，不再是直接消失"""
        self.system.add_wall("w", health=50.0)
        result = self.system.destroy_wall("w")
        assert result["debris"] > 0
        assert len(self.system.debris) == result["debris"]

    def test_case_06(self):
        """爆炸破坏向相邻墙壁传播溅射伤害"""
        self.system.add_wall("a", health=100.0,
                             neighbors=["b"], position=(0, 0, 0))
        self.system.add_wall("b", health=100.0, position=(2, 0, 0))
        result = self.system.destroy_wall("a", damage_type="explosion")
        assert "b" in result["propagated"]
        assert self.system.walls["b"].health < 100.0

    # ---- 碎片物理 ----
    def test_case_07(self):
        """碎片生成后被施加初速度，不会原地不动"""
        self.system.add_wall("w", health=50.0)
        self.system.destroy_wall("w")
        before = [list(p.position) for p in self.system.debris]
        self.system.debris_physics(0.016)
        after = [list(p.position) for p in self.system.debris]
        assert before != after

    def test_case_08(self):
        """碎片与地面碰撞，长时间模拟后不会穿过地板"""
        self.system.add_wall("w", health=50.0)
        self.system.destroy_wall("w")
        for _ in range(600):
            self.system.debris_physics(0.05)
        assert all(p.position[1] >= 0.0 for p in self.system.debris)

    def test_case_09(self):
        """碎片数量有全局上限，不会一次生成上千个拖垮帧率"""
        system = MapDestruction(config={"max_debris": 30})
        for i in range(20):
            system.add_wall(f"w{i}", health=1.0)
        for i in range(20):
            system.destroy_wall(f"w{i}")
        assert len(system.debris) <= 30
        assert DEBRIS_PER_WALL <= DEFAULT_MAX_DEBRIS

    # ---- 结构完整性 ----
    def test_case_10(self):
        """无任何地面支撑的悬空墙在完整性检查时坍塌"""
        self.system.add_wall("floating", position=(0, 10, 0), health=200.0)
        collapsed = self.system.structural_integrity()
        assert "floating" in collapsed
        assert self.system.walls["floating"].destroyed is True

    def test_case_11(self):
        """应力集中：残血的承重墙应力高于满血同位置墙"""
        self.system.add_wall("ground", position=(0, 0, 0), health=100.0)
        self.system.add_wall("top", position=(0, 3, 0), health=100.0,
                             neighbors=["ground"])
        self.system.add_wall("g2", position=(5, 0, 0), health=100.0)
        self.system.add_wall("t2", position=(5, 3, 0), health=100.0,
                             neighbors=["g2"])
        self.system.apply_damage("ground", 80.0, "bullet")
        stress = self.system.compute_stress()
        assert stress["ground"] > stress["g2"]

    def test_case_12(self):
        """承重墙被炸后重新计算支撑，上方楼板坍塌而不是继续飘着"""
        self.system.add_wall("support", position=(0, 0, 0), health=20.0)
        self.system.add_wall("floor", position=(0, 3, 0), health=500.0,
                             neighbors=["support"])
        assert "floor" in self.system._supported_ids()
        self.system.destroy_wall("support", damage_type="explosion")
        assert self.system.walls["floor"].destroyed is True

    # ---- 破坏效果 ----
    def test_case_13(self):
        """破坏效果同时包含裂痕和变形，不再只有粒子"""
        effect = self.system.destruction_effect("w", damage_type="explosion")
        assert effect["cracks"]["intensity"] > 0
        assert effect["cracks"]["count"] > 0
        assert effect["deformation"]["amount"] > 0
        assert effect["particles"]["count"] > 0

    def test_case_14(self):
        """效果随伤害类型变化：火烧与爆炸的裂痕/粒子风格不同"""
        fire = self.system.destruction_effect("w", damage_type="fire")
        explosion = self.system.destruction_effect("w", damage_type="explosion")
        assert fire["cracks"]["pattern"] != explosion["cracks"]["pattern"]
        assert fire["particles"]["kind"] != explosion["particles"]["kind"]
        assert fire["deformation"]["kind"] != explosion["deformation"]["kind"]

    def test_case_15(self):
        """破坏效果按距离做 LOD，远处播放低分辨率效果"""
        near = self.system.destruction_effect("w", distance=5.0)
        mid = self.system.destruction_effect("w", distance=30.0)
        far = self.system.destruction_effect("w", distance=100.0)
        assert near["lod"] == "high"
        assert mid["lod"] == "medium"
        assert far["lod"] == "low"
        assert near["particles"]["count"] > mid["particles"]["count"]
        assert mid["particles"]["count"] > far["particles"]["count"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
