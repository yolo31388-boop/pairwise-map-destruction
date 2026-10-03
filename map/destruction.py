"""
地图破坏系统 - 核心模块

功能：
- apply_damage(): 按伤害类型加权扣血，血量低于破坏阈值时触发墙壁破坏
- destroy_wall(): 移除碰撞体、生成碎片、向相邻墙壁传播破坏
- debris_physics(): 碎片受力运动、与地面碰撞、数量上限控制
- structural_integrity(): 支撑关系检查、应力集中计算、破坏后重新计算
- destruction_effect(): 裂痕与变形效果、随伤害类型变化、按距离做 LOD
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import math


# 伤害类型 -> 伤害倍率
DAMAGE_TYPE_MULTIPLIERS = {
    "bullet": 1.0,
    "melee": 1.5,
    "explosion": 3.0,
    "fire": 0.5,
}

# 材质 -> (基础血量, 单块碎片尺寸)
MATERIALS = {
    "wood": (60.0, 0.3),
    "concrete": (100.0, 0.5),
    "steel": (200.0, 0.8),
}

GRAVITY = 9.8
DEFAULT_MAX_DEBRIS = 200
# 单次破坏生成的碎片数量上限
DEBRIS_PER_WALL = 12
# 破坏传播给相邻墙壁的伤害比例
PROPAGATION_RATIO = 0.4
# 应力集中阈值：超过则结构断裂
STRESS_THRESHOLD = 1.0

# LOD 距离阈值
LOD_DISTANCES = ((20.0, "high"), (50.0, "medium"), (math.inf, "low"))

# 每种伤害类型的效果特征
EFFECT_STYLES = {
    "bullet": {"crack_pattern": "radial", "deformation": "puncture", "particle_kind": "dust"},
    "melee": {"crack_pattern": "dent", "deformation": "dent", "particle_kind": "chips"},
    "explosion": {"crack_pattern": "shatter", "deformation": "blast", "particle_kind": "fireball"},
    "fire": {"crack_pattern": "scorch", "deformation": "melt", "particle_kind": "flame"},
}


@dataclass
class Wall:
    wall_id: str
    position: Tuple[float, float, float]  # 底面中心 (x, y, z)
    size: Tuple[float, float, float]      # (宽, 高, 厚)
    material: str = "concrete"
    health: float = 100.0
    max_health: float = 100.0
    neighbors: List[str] = field(default_factory=list)
    destroyed: bool = False
    has_collider: bool = True
    cracks: float = 0.0        # 裂痕程度 0~1
    deformation: float = 0.0   # 变形程度 0~1


@dataclass
class Debris:
    position: List[float]
    velocity: List[float]
    size: float
    grounded: bool = False


class MapDestruction:
    """地图破坏系统核心类"""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self.max_debris = int(self.config.get("max_debris", DEFAULT_MAX_DEBRIS))
        self.walls: Dict[str, Wall] = {}
        self.debris: List[Debris] = []
        self.effects: List[Dict] = []
        self._state = {}
        self._history = []

    # ------------------------------------------------------------------
    # 墙壁管理
    # ------------------------------------------------------------------
    def add_wall(self, wall_id: str,
                 position: Tuple[float, float, float] = (0.0, 0.0, 0.0),
                 size: Tuple[float, float, float] = (2.0, 3.0, 0.3),
                 material: str = "concrete",
                 health: Optional[float] = None,
                 neighbors: Optional[List[str]] = None) -> Wall:
        base_health, _ = MATERIALS.get(material, MATERIALS["concrete"])
        max_health = float(health) if health is not None else base_health
        wall = Wall(
            wall_id=wall_id,
            position=tuple(position),
            size=tuple(size),
            material=material,
            health=max_health,
            max_health=max_health,
            neighbors=list(neighbors or []),
        )
        self.walls[wall_id] = wall
        # 邻居关系双向化
        for other_id in wall.neighbors:
            other = self.walls.get(other_id)
            if other is not None and wall_id not in other.neighbors:
                other.neighbors.append(wall_id)
        return wall

    def can_pass(self, wall_id: str) -> bool:
        """玩家能否穿过该墙壁（碰撞体已移除即可通过）"""
        wall = self.walls.get(wall_id)
        if wall is None:
            return True
        return not wall.has_collider

    # ------------------------------------------------------------------
    # 伤害
    # ------------------------------------------------------------------
    def damage_multiplier(self, damage_type: str) -> float:
        return DAMAGE_TYPE_MULTIPLIERS.get(damage_type, 1.0)

    def apply_damage(self, wall_id: str, damage: float,
                     damage_type: str = "bullet") -> Dict:
        """按伤害类型加权扣血；血量降到 0 及以下时触发破坏。"""
        wall = self.walls.get(wall_id)
        if wall is None or wall.destroyed:
            return {"wall_id": wall_id, "applied": 0.0, "destroyed": False,
                    "remaining_health": 0.0}

        effective = float(damage) * self.damage_multiplier(damage_type)
        wall.health = max(0.0, wall.health - effective)
        # 伤害留下裂痕与变形
        damage_ratio = effective / wall.max_health if wall.max_health > 0 else 1.0
        wall.cracks = min(1.0, wall.cracks + damage_ratio)
        wall.deformation = min(1.0, wall.deformation + damage_ratio * 0.5)

        destroyed = wall.health <= 0.0
        if destroyed:
            self.destroy_wall(wall_id, damage_type=damage_type)

        self._history.append({
            "event": "damage", "wall_id": wall_id,
            "type": damage_type, "applied": effective,
        })
        return {"wall_id": wall_id, "applied": effective,
                "destroyed": destroyed, "remaining_health": wall.health}

    # ------------------------------------------------------------------
    # 破坏
    # ------------------------------------------------------------------
    def destroy_wall(self, wall_id: str, damage_type: str = "explosion") -> Dict:
        """破坏墙壁：移除碰撞体、生成碎片、传播破坏、重算结构完整性。"""
        wall = self.walls.get(wall_id)
        if wall is None or wall.destroyed:
            return {"wall_id": wall_id, "destroyed": False, "debris": 0,
                    "propagated": []}

        wall.destroyed = True
        wall.health = 0.0
        wall.has_collider = False  # 移除碰撞体，玩家可以通过
        wall.cracks = 1.0
        wall.deformation = 1.0

        spawned = self._spawn_debris(wall)
        self.destruction_effect(wall_id, damage_type=damage_type)

        # 破坏传播：相邻墙壁受到溅射伤害
        propagated = []
        if damage_type == "explosion":
            splash = wall.max_health * PROPAGATION_RATIO
            for neighbor_id in list(wall.neighbors):
                neighbor = self.walls.get(neighbor_id)
                if neighbor is None or neighbor.destroyed:
                    continue
                result = self.apply_damage(neighbor_id, splash, damage_type)
                propagated.append(neighbor_id)
                if result["destroyed"]:
                    continue

        # 破坏后重新计算支撑，悬空结构会坍塌
        collapsed = self.structural_integrity()

        self._history.append({"event": "destroy", "wall_id": wall_id,
                              "type": damage_type})
        return {"wall_id": wall_id, "destroyed": True, "debris": spawned,
                "propagated": propagated, "collapsed": collapsed}

    def _spawn_debris(self, wall: Wall) -> int:
        """为被破坏的墙壁生成碎片，受全局数量上限约束。"""
        _, debris_size = MATERIALS.get(wall.material, MATERIALS["concrete"])
        room = max(0, self.max_debris - len(self.debris))
        count = min(DEBRIS_PER_WALL, room)
        cx, cy, cz = wall.position
        height = wall.size[1]
        for i in range(count):
            angle = (2.0 * math.pi * i) / max(count, 1)
            speed = 3.0 + (i % 3)
            self.debris.append(Debris(
                position=[cx, cy + height * 0.5, cz],
                velocity=[speed * math.cos(angle),
                          4.0 + (i % 4),
                          speed * math.sin(angle)],
                size=debris_size,
            ))
        return count

    # ------------------------------------------------------------------
    # 碎片物理
    # ------------------------------------------------------------------
    def debris_physics(self, dt: float) -> int:
        """更新碎片：施加重力、按速度运动、与地面碰撞。返回活动碎片数。"""
        active = 0
        for piece in self.debris:
            if piece.grounded:
                continue
            piece.velocity[1] -= GRAVITY * dt
            for axis in range(3):
                piece.position[axis] += piece.velocity[axis] * dt
            # 与地面碰撞：不穿地板，落地弹跳直至停稳
            if piece.position[1] <= 0.0:
                piece.position[1] = 0.0
                if abs(piece.velocity[1]) > 1.0:
                    piece.velocity[1] = -piece.velocity[1] * 0.3
                    piece.velocity[0] *= 0.6
                    piece.velocity[2] *= 0.6
                else:
                    piece.velocity = [0.0, 0.0, 0.0]
                    piece.grounded = True
            active += 1
        return active

    # ------------------------------------------------------------------
    # 结构完整性
    # ------------------------------------------------------------------
    def _is_grounded(self, wall: Wall) -> bool:
        return wall.position[1] <= 0.0

    def _supported_ids(self) -> set:
        """从地面墙壁出发沿邻居关系 BFS，得到所有被支撑的墙壁。"""
        supported = {wid for wid, w in self.walls.items()
                     if not w.destroyed and self._is_grounded(w)}
        queue = list(supported)
        while queue:
            current = queue.pop()
            for neighbor_id in self.walls[current].neighbors:
                neighbor = self.walls.get(neighbor_id)
                if (neighbor is not None and not neighbor.destroyed
                        and neighbor_id not in supported):
                    supported.add(neighbor_id)
                    queue.append(neighbor_id)
        return supported

    def compute_stress(self) -> Dict[str, float]:
        """应力 = 该墙承载的上方墙数 / 剩余血量比例。薄弱环节应力更高。"""
        stress = {wid: 0.0 for wid, w in self.walls.items() if not w.destroyed}
        supported = self._supported_ids()
        # 每面非地面墙的支撑者：y 更低且相邻的未毁墙
        for wid in supported:
            wall = self.walls[wid]
            if self._is_grounded(wall):
                continue
            load = 1.0 + wall.size[1] * 0.1
            for neighbor_id in wall.neighbors:
                neighbor = self.walls.get(neighbor_id)
                if (neighbor is not None and not neighbor.destroyed
                        and neighbor.position[1] < wall.position[1]):
                    health_ratio = (neighbor.health / neighbor.max_health
                                    if neighbor.max_health > 0 else 0.0)
                    stress[neighbor_id] = stress.get(neighbor_id, 0.0) + (
                        load / max(health_ratio, 0.05))
        return stress

    def structural_integrity(self) -> List[str]:
        """检查支撑关系与应力集中；悬空墙坍塌、应力超限的薄弱环节断裂。

        返回本次坍塌/断裂的墙壁 id 列表。
        """
        collapsed = []
        # 1) 支撑检查：不在支撑集合内的未毁墙坍塌
        supported = self._supported_ids()
        for wid, wall in list(self.walls.items()):
            if wall.destroyed or wid in supported:
                continue
            wall.destroyed = True
            wall.has_collider = False
            self._spawn_debris(wall)
            collapsed.append(wid)

        # 2) 应力集中：超过阈值的薄弱环节先断
        stress = self.compute_stress()
        for wid, value in stress.items():
            wall = self.walls.get(wid)
            if wall is None or wall.destroyed:
                continue
            if value > STRESS_THRESHOLD * 10.0:
                wall.destroyed = True
                wall.has_collider = False
                self._spawn_debris(wall)
                collapsed.append(wid)

        if collapsed:
            self._history.append({"event": "collapse", "walls": collapsed})
        return collapsed

    # ------------------------------------------------------------------
    # 破坏效果
    # ------------------------------------------------------------------
    def destruction_effect(self, wall_id: str, damage_type: str = "explosion",
                           distance: float = 0.0) -> Dict:
        """生成破坏效果：裂痕 + 变形 + 粒子，随伤害类型变化，按距离 LOD。"""
        wall = self.walls.get(wall_id)
        lod = "low"
        for threshold, level in LOD_DISTANCES:
            if distance < threshold:
                lod = level
                break

        lod_scale = {"high": 1.0, "medium": 0.5, "low": 0.1}[lod]
        style = EFFECT_STYLES.get(damage_type, EFFECT_STYLES["explosion"])
        cracks = wall.cracks if wall is not None else 1.0
        deformation = wall.deformation if wall is not None else 1.0

        effect = {
            "wall_id": wall_id,
            "damage_type": damage_type,
            "lod": lod,
            "cracks": {
                "pattern": style["crack_pattern"],
                "intensity": cracks,
                "count": max(1, int(16 * cracks * lod_scale)),
            },
            "deformation": {
                "kind": style["deformation"],
                "amount": deformation,
            },
            "particles": {
                "kind": style["particle_kind"],
                "count": max(1, int(100 * lod_scale)),
            },
        }
        self.effects.append(effect)
        return effect

    # ------------------------------------------------------------------
    # 帧更新与重置
    # ------------------------------------------------------------------
    def update(self, dt: float):
        """每帧更新：推进碎片物理。"""
        self.debris_physics(dt)

    def reset(self):
        """重置状态"""
        self.walls = {}
        self.debris = []
        self.effects = []
        self._state = {}
        self._history = []
