"""
地图破坏系统 - 核心模块

功能：
- apply_damage(): 按伤害类型加权，检查破坏阈值
- destroy_wall(): 移除碰撞体、生成碎片、破坏传播
- debris_physics(): 碎片受力、地面碰撞、数量上限
- structural_integrity(): 支撑关系、应力集中、破坏后重算
- destruction_effect(): 裂痕/变形、按伤害类型区分、LOD
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import math


DEFAULT_CONFIG: Dict = {
    # 各伤害类型的伤害倍率
    "damage_multipliers": {
        "bullet": 1.0,
        "melee": 0.8,
        "fire": 1.5,
        "explosive": 2.5,
    },
    # 爆炸对相邻墙壁的传播伤害比例
    "blast_falloff": 0.5,
    # 碎片数量上限（防止一次破坏生成过多碎片拖垮帧率）
    "max_debris": 50,
    # 每面墙破坏时生成的碎片数
    "debris_per_wall": 8,
    # 碎片初速度
    "debris_speed": 6.0,
    "gravity": 9.8,
    "ground_y": 0.0,
    # 碎片落地反弹系数
    "debris_restitution": 0.3,
    # 应力/强度 超过该值时结构断裂
    "stress_limit": 1.0,
    # 破坏效果 LOD 距离分界（近/中，之外为远）
    "lod_distances": (10.0, 30.0),
}


@dataclass
class Wall:
    """一面可破坏的墙"""
    wall_id: str
    health: float = 100.0
    max_health: float = 100.0
    position: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    material: str = "concrete"
    strength: float = 100.0   # 承载能力
    weight: float = 10.0      # 自重（用于应力计算）
    destroyed: bool = False
    has_collider: bool = True
    visible: bool = True
    cracks: float = 0.0        # 裂痕程度 0..1
    deformation: float = 0.0   # 变形程度 0..1
    supported_by: List[str] = field(default_factory=list)  # 下方支撑墙
    adjacent: List[str] = field(default_factory=list)      # 相邻墙（破坏传播）
    supported: bool = True
    stress: float = 0.0


@dataclass
class Debris:
    """破坏产生的碎片"""
    debris_id: int
    wall_id: str
    position: Tuple[float, float, float]
    velocity: Tuple[float, float, float]
    has_collider: bool = True
    settled: bool = False


class MapDestruction:
    """地图破坏系统核心类"""

    def __init__(self, config: Optional[Dict] = None):
        merged = dict(DEFAULT_CONFIG)
        if config:
            merged.update(config)
        merged["damage_multipliers"] = dict(DEFAULT_CONFIG["damage_multipliers"])
        if config and "damage_multipliers" in config:
            merged["damage_multipliers"].update(config["damage_multipliers"])
        self.config = merged
        self.walls: Dict[str, Wall] = {}
        self.debris: List[Debris] = []
        self.effects: List[Dict] = []
        self._debris_seq = 0
        self._state = {}
        self._history = []

    # ------------------------------------------------------------------
    # 墙壁管理
    # ------------------------------------------------------------------
    def add_wall(self, wall_id: str, health: float = 100.0,
                 position: Tuple[float, float, float] = (0.0, 0.0, 0.0),
                 material: str = "concrete", strength: float = 100.0,
                 weight: float = 10.0,
                 supported_by: Optional[List[str]] = None,
                 adjacent: Optional[List[str]] = None) -> Wall:
        wall = Wall(
            wall_id=wall_id,
            health=health,
            max_health=health,
            position=position,
            material=material,
            strength=strength,
            weight=weight,
            supported_by=list(supported_by or []),
            adjacent=list(adjacent or []),
        )
        self.walls[wall_id] = wall
        return wall

    def get_wall(self, wall_id: str) -> Optional[Wall]:
        return self.walls.get(wall_id)

    # ------------------------------------------------------------------
    # 伤害应用：按类型加权 + 阈值检查
    # ------------------------------------------------------------------
    def apply_damage(self, wall_id: str, damage: float,
                     damage_type: str = "bullet",
                     position: Optional[Tuple[float, float, float]] = None) -> Dict:
        wall = self.walls.get(wall_id)
        if wall is None:
            raise KeyError(f"未知墙壁: {wall_id}")
        if wall.destroyed:
            return {"wall_id": wall_id, "damage": 0.0, "destroyed": True,
                    "remaining_health": 0.0}

        multiplier = self.config["damage_multipliers"].get(damage_type, 1.0)
        effective = damage * multiplier
        wall.health = max(0.0, wall.health - effective)

        # 受损产生裂痕与变形（爆炸变形更明显）
        damage_ratio = 1.0 - wall.health / wall.max_health if wall.max_health else 1.0
        wall.cracks = min(1.0, damage_ratio)
        deform_factor = 1.5 if damage_type == "explosive" else 1.0
        wall.deformation = min(1.0, damage_ratio * deform_factor)

        destroyed = False
        # 阈值检查：血量归零即触发破坏，并透传破坏结果（碎片/传播等）
        result = {"wall_id": wall_id, "damage": effective, "destroyed": False,
                  "remaining_health": wall.health}
        if wall.health <= 0.0:
            destroyed_result = self.destroy_wall(
                wall_id, damage_type=damage_type, position=position)
            result.update(destroyed_result)
            result["destroyed"] = True

        return result

    # ------------------------------------------------------------------
    # 墙壁破坏：移除碰撞体 + 生成碎片 + 传播 + 结构重算
    # ------------------------------------------------------------------
    def destroy_wall(self, wall_id: str, damage_type: str = "explosive",
                     position: Optional[Tuple[float, float, float]] = None,
                     _visited: Optional[set] = None) -> Dict:
        wall = self.walls.get(wall_id)
        if wall is None:
            raise KeyError(f"未知墙壁: {wall_id}")
        if _visited is None:
            _visited = set()
        if wall_id in _visited or wall.destroyed:
            return {"wall_id": wall_id, "debris": [], "effect": None,
                    "collateral": []}
        _visited.add(wall_id)

        # 关闭渲染的同时必须移除碰撞体，否则玩家仍被挡住
        wall.destroyed = True
        wall.visible = False
        wall.has_collider = False
        wall.health = 0.0
        wall.cracks = 1.0
        wall.deformation = 1.0

        # 生成碎片
        spawned = self._spawn_debris(wall, position)

        # 破坏效果
        effect = self.destruction_effect(wall_id, damage_type=damage_type)

        # 破坏传播：相邻墙壁受到溅射伤害
        collateral = []
        if damage_type == "explosive":
            splash = wall.max_health * self.config["blast_falloff"]
            for adj_id in wall.adjacent:
                adj = self.walls.get(adj_id)
                if adj is None or adj.destroyed or adj_id in _visited:
                    continue
                result = self.apply_damage(adj_id, splash, damage_type="bullet")
                collateral.append(adj_id)
                if result["destroyed"]:
                    # 连锁破坏时避免重复传播
                    _visited.add(adj_id)

        # 破坏后重新计算结构完整性（悬空结构会坍塌）
        integrity = self.structural_integrity(_visited=_visited)

        return {"wall_id": wall_id, "debris": [d.debris_id for d in spawned],
                "effect": effect, "collateral": collateral,
                "collapsed": integrity["collapsed"]}

    # ------------------------------------------------------------------
    # 碎片物理：受力 + 碰撞 + 数量限制
    # ------------------------------------------------------------------
    def _spawn_debris(self, wall: Wall,
                      blast_position: Optional[Tuple[float, float, float]]) -> List[Debris]:
        count = self.config["debris_per_wall"]
        speed = self.config["debris_speed"]
        origin = blast_position or wall.position
        spawned = []
        for i in range(count):
            # 确定性方向：从爆心向外散射
            angle = (2.0 * math.pi * i) / max(1, count)
            dx = wall.position[0] - origin[0] + math.cos(angle)
            dy = 1.0  # 向上抛起
            dz = wall.position[2] - origin[2] + math.sin(angle)
            norm = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
            velocity = (dx / norm * speed, dy / norm * speed, dz / norm * speed)
            self._debris_seq += 1
            debris = Debris(
                debris_id=self._debris_seq,
                wall_id=wall.wall_id,
                position=tuple(wall.position),
                velocity=velocity,
                has_collider=True,
            )
            self.debris.append(debris)
            spawned.append(debris)
        self._enforce_debris_limit()
        return spawned

    def _enforce_debris_limit(self):
        """限制碎片总数，超出时移除最老的碎片"""
        limit = self.config["max_debris"]
        if limit is not None and len(self.debris) > limit:
            del self.debris[: len(self.debris) - limit]

    def debris_physics(self, dt: float):
        """推进碎片物理：重力、速度积分、地面碰撞"""
        gravity = self.config["gravity"]
        ground_y = self.config["ground_y"]
        restitution = self.config["debris_restitution"]
        for debris in self.debris:
            if debris.settled:
                continue
            vx, vy, vz = debris.velocity
            # 应用重力
            vy -= gravity * dt
            x, y, z = debris.position
            x += vx * dt
            y += vy * dt
            z += vz * dt
            # 地面碰撞：碎片有碰撞体，不能穿过地板
            if y <= ground_y:
                y = ground_y
                vy = -vy * restitution
                vx *= 0.8
                vz *= 0.8
                if abs(vy) < 0.5:
                    vy = 0.0
                    vx = 0.0
                    vz = 0.0
                    debris.settled = True
            debris.position = (x, y, z)
            debris.velocity = (vx, vy, vz)

    # ------------------------------------------------------------------
    # 结构完整性：支撑关系 + 应力集中 + 破坏后重算
    # ------------------------------------------------------------------
    def structural_integrity(self, _visited: Optional[set] = None) -> Dict:
        if _visited is None:
            _visited = set()
        collapsed: List[str] = []
        peak_stresses: Dict[str, float] = {}
        # 迭代直到稳定：坍塌可能引发新的悬空/超载
        for _ in range(len(self.walls) + 1):
            self._compute_support()
            stresses = self._compute_stress()
            for wid, value in stresses.items():
                if value > peak_stresses.get(wid, 0.0):
                    peak_stresses[wid] = value
            # 悬空（无支撑且非落地）的墙坍塌
            falling = [w for w in self.walls.values()
                       if not w.destroyed and not w.supported]
            # 应力超限的墙中，应力比最高（最薄弱）的先断
            overloaded = [w for w in self.walls.values()
                          if not w.destroyed and w.supported
                          and w.stress > self.config["stress_limit"]]
            overloaded.sort(key=lambda w: w.stress, reverse=True)
            breaking = falling + overloaded[:1]
            if not breaking:
                break
            for wall in breaking:
                if wall.wall_id in _visited:
                    continue
                _visited.add(wall.wall_id)
                collapsed.append(wall.wall_id)
                self._collapse_wall(wall)
        return {"collapsed": collapsed,
                "stresses": {wid: w.stress for wid, w in self.walls.items()},
                "peak_stresses": peak_stresses,
                "supported": {wid: w.supported for wid, w in self.walls.items()}}

    def _compute_support(self):
        """支撑关系：落地或有完整支撑墙的墙才算有支撑"""
        ground_y = self.config["ground_y"]
        changed = True
        for w in self.walls.values():
            w.supported = False
        # 迭代传播支撑状态（支撑墙本身可能悬空）
        while changed:
            changed = False
            for w in self.walls.values():
                if w.destroyed or w.supported:
                    continue
                on_ground = w.position[1] <= ground_y
                held = any(
                    (s in self.walls and not self.walls[s].destroyed
                     and self.walls[s].supported)
                    for s in w.supported_by
                )
                if on_ground or held:
                    w.supported = True
                    changed = True

    def _compute_stress(self) -> Dict[str, float]:
        """应力 = (自重 + 上方传递的载荷) / 强度，载荷沿支撑链向下传递"""
        load = {wid: (0.0 if w.destroyed else w.weight)
                for wid, w in self.walls.items()}
        # 自顶向下传播载荷（按高度降序）
        ordered = sorted(self.walls.values(),
                         key=lambda w: w.position[1], reverse=True)
        for w in ordered:
            if w.destroyed:
                continue
            supporters = [self.walls[s] for s in w.supported_by
                          if s in self.walls and not self.walls[s].destroyed]
            if not supporters:
                continue
            share = load[w.wall_id] / len(supporters)
            for s in supporters:
                load[s.wall_id] += share
        for wid, w in self.walls.items():
            w.stress = 0.0 if (w.destroyed or w.strength <= 0) \
                else load[wid] / w.strength
        return {wid: w.stress for wid, w in self.walls.items()}

    def _collapse_wall(self, wall: Wall):
        """结构坍塌：墙体掉落，移除碰撞体并产生碎片"""
        wall.destroyed = True
        wall.visible = False
        wall.has_collider = False
        wall.health = 0.0
        wall.cracks = 1.0
        wall.deformation = 1.0
        self._spawn_debris(wall, wall.position)
        self.effects.append(self.destruction_effect(wall.wall_id,
                                                    damage_type="collapse"))

    # ------------------------------------------------------------------
    # 破坏效果：裂痕/变形 + 按伤害类型区分 + LOD
    # ------------------------------------------------------------------
    def destruction_effect(self, wall_id: str, damage_type: str = "explosive",
                           distance: float = 0.0) -> Dict:
        wall = self.walls.get(wall_id)
        if wall is None:
            raise KeyError(f"未知墙壁: {wall_id}")

        # 按伤害类型区分效果组成
        type_effects = {
            "explosive": ["shockwave", "flash", "smoke", "rubble"],
            "fire": ["flames", "embers", "smoke", "scorch"],
            "bullet": ["dust", "sparks"],
            "melee": ["dust", "chunks"],
            "collapse": ["dust", "rubble", "rumble"],
        }
        components = list(type_effects.get(damage_type, ["dust"]))

        # LOD：根据观察距离降低效果精度
        near, far = self.config["lod_distances"]
        if distance <= near:
            lod = "high"
            particle_count = 200
        elif distance <= far:
            lod = "medium"
            particle_count = 80
        else:
            lod = "low"
            particle_count = 20

        effect = {
            "wall_id": wall_id,
            "damage_type": damage_type,
            "cracks": wall.cracks,            # 裂痕
            "deformation": wall.deformation,  # 变形
            "components": components,
            "particles": particle_count,
            "lod": lod,
        }
        self.effects.append(effect)
        return effect

    # ------------------------------------------------------------------
    # 帧更新 / 重置
    # ------------------------------------------------------------------
    def update(self, dt: float):
        """每帧更新：推进碎片物理"""
        self.debris_physics(dt)

    def reset(self):
        """重置状态"""
        self.walls = {}
        self.debris = []
        self.effects = []
        self._debris_seq = 0
        self._state = {}
        self._history = []
